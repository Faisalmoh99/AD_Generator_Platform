"""Budget ledger (data only). The reserve/commit logic lives in ``agent/budget.py``."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from adsmith.domain.base import DomainModel, utcnow

CostCategory = Literal["llm", "image", "video"]
USD_DECIMALS = 8  # same precision as LLMCallRecord.cost_usd, so ledger and trace always agree


class BudgetEntry(DomainModel):
    at: datetime = Field(default_factory=utcnow)
    category: CostCategory
    scene_id: str | None = None
    est_usd: float = Field(ge=0)
    actual_usd: float | None = Field(default=None, ge=0)
    ref: str = Field(description="attempt id / llm call id")


class BudgetRefusedError(ValueError):
    """A reservation would exceed the run's money or attempt limits (no call is made)."""


class BudgetLedger(DomainModel):
    limit_usd: float = Field(gt=0)
    # Separate cap for paid media (image + video) only, e.g. the prepaid fal wallet's headroom.
    # LLM spend is billed elsewhere (Anthropic) and must not starve the media budget (STOP 1:
    # a $0.60 fal cap with ~$0.5 of LLM calls). None = only ``limit_usd`` applies.
    media_limit_usd: float | None = Field(default=None, ge=0)
    max_attempts_per_scene: int = Field(ge=1)
    max_total_attempts: int = Field(ge=1)
    spent_usd: float = Field(default=0.0, ge=0)
    reserved_usd: float = Field(default=0.0, ge=0)
    attempts_total: int = Field(default=0, ge=0)
    by_category: dict[CostCategory, float] = Field(default_factory=dict)
    entries: list[BudgetEntry] = Field(default_factory=list)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd - self.reserved_usd)

    @property
    def media_committed_usd(self) -> float:
        """Settled + open reservations for image and video."""
        return round(
            sum(
                e.actual_usd if e.actual_usd is not None else e.est_usd
                for e in self.entries
                if e.category != "llm"
            ),
            USD_DECIMALS,
        )

    @property
    def media_remaining_usd(self) -> float | None:
        if self.media_limit_usd is None:
            return None
        return max(0.0, self.media_limit_usd - self.media_committed_usd)

    def record_actual(
        self,
        category: CostCategory,
        usd: float,
        ref: str,
        scene_id: str | None = None,
        est_usd: float | None = None,
    ) -> None:
        """Book an already-incurred cost (e.g. a finished LLM call). Idempotent per ``ref``."""
        if any(e.ref == ref for e in self.entries):
            return
        self.entries.append(
            BudgetEntry(
                category=category,
                scene_id=scene_id,
                est_usd=usd if est_usd is None else est_usd,
                actual_usd=usd,
                ref=ref,
            )
        )
        self.spent_usd = round(self.spent_usd + usd, USD_DECIMALS)
        self.by_category[category] = round(self.by_category.get(category, 0.0) + usd, USD_DECIMALS)

    # ------------------------------------------------------------------ reserve → settle/release
    # Reservations are made inside the atomic state update that also starts the attempt, so
    # concurrent scenes can never jointly overshoot the limit (ADR-0011 single-writer).
    def reserve(
        self, ref: str, category: CostCategory, est_usd: float, scene_id: str | None
    ) -> None:
        if self.attempts_total >= self.max_total_attempts:
            raise BudgetRefusedError(
                f"run attempt limit reached ({self.attempts_total}/{self.max_total_attempts})"
            )
        if est_usd > self.remaining_usd + 1e-9:
            raise BudgetRefusedError(
                f"estimated ${est_usd:.3f} exceeds the remaining ${self.remaining_usd:.3f} "
                f"of the ${self.limit_usd:g} budget"
            )
        media_left = self.media_remaining_usd
        if category != "llm" and media_left is not None and est_usd > media_left + 1e-9:
            raise BudgetRefusedError(
                f"estimated ${est_usd:.3f} exceeds the remaining ${media_left:.3f} "
                f"of the ${self.media_limit_usd:g} media cap"
            )
        self.entries.append(
            BudgetEntry(category=category, scene_id=scene_id, est_usd=est_usd, ref=ref)
        )
        self.reserved_usd = round(self.reserved_usd + est_usd, USD_DECIMALS)
        self.attempts_total += 1

    def _open(self, ref: str) -> BudgetEntry:
        for e in self.entries:
            if e.ref == ref and e.actual_usd is None:
                return e
        raise KeyError(f"no open reservation {ref!r}")

    def settle(self, ref: str, actual_usd: float) -> None:
        entry = self._open(ref)
        entry.actual_usd = actual_usd
        self.reserved_usd = max(0.0, round(self.reserved_usd - entry.est_usd, USD_DECIMALS))
        self.spent_usd = round(self.spent_usd + actual_usd, USD_DECIMALS)
        self.by_category[entry.category] = round(
            self.by_category.get(entry.category, 0.0) + actual_usd, USD_DECIMALS
        )

    def release(self, ref: str) -> None:
        """The call failed before incurring cost: free the reservation (actual = 0)."""
        self.settle(ref, 0.0)

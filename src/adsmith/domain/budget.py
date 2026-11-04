"""Budget ledger (data only). The reserve/commit logic lives in ``agent/budget.py``."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from adsmith.domain.base import DomainModel, utcnow

CostCategory = Literal["llm", "image", "video"]


class BudgetEntry(DomainModel):
    at: datetime = Field(default_factory=utcnow)
    category: CostCategory
    scene_id: str | None = None
    est_usd: float = Field(ge=0)
    actual_usd: float | None = Field(default=None, ge=0)
    ref: str = Field(description="attempt id / llm call id")


class BudgetLedger(DomainModel):
    limit_usd: float = Field(gt=0)
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
        self.spent_usd = round(self.spent_usd + usd, 6)
        self.by_category[category] = round(self.by_category.get(category, 0.0) + usd, 6)

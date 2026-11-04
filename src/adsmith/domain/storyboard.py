"""The storyboard: ordered scenes whose durations add up to the target.

Where the duration invariant lives (ADR-0002 addendum): the storyboard **owns** its
``target_duration_s``. It is copied from the Brief when the storyboard is built, so the invariant
"durations sum to target" is self-contained and holds on every load, without hidden validation
context. That the target agrees with ``brief.duration_s`` is a *cross-aggregate* rule, so
``RunState`` checks it.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel
from adsmith.domain.scene import Scene, SceneDraft

DURATION_TOLERANCE_S = 0.25
MIN_SCENES = 1  # 1 = single-take hero format (one continuous shot)
MAX_SCENES = 6


class StoryboardDraft(DomainModel):
    """``storyboard`` LLM output. Durations are checked when it is turned into a ``Storyboard``."""

    scenes: list[SceneDraft] = Field(min_length=MIN_SCENES, max_length=MAX_SCENES)


def check_duration_sum(durations: list[float], target: float) -> None:
    total = round(sum(durations), 3)
    if abs(total - target) > DURATION_TOLERANCE_S:
        listed = " + ".join(f"{d:g}" for d in durations)
        raise ValueError(
            f"scene durations sum to {total:g}s ({listed}) but the target is {target:g}s "
            f"(tolerance ±{DURATION_TOLERANCE_S}s). Adjust durations so they add up."
        )


class Storyboard(DomainModel):
    concept_id: str
    target_duration_s: float = Field(gt=0, le=60)
    scenes: list[Scene] = Field(min_length=MIN_SCENES, max_length=MAX_SCENES)

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        self.check_invariants()
        return self

    def check_invariants(self) -> None:
        orders = [s.order for s in self.scenes]
        if orders != list(range(1, len(self.scenes) + 1)):
            raise ValueError(f"scene orders must be 1..n in sequence, got {orders}")
        ids = [s.id for s in self.scenes]
        if len(set(ids)) != len(ids):
            raise ValueError("scene ids must be unique")
        check_duration_sum([s.duration_s for s in self.scenes], self.target_duration_s)

    @classmethod
    def from_draft(
        cls, draft: StoryboardDraft, *, concept_id: str, target_duration_s: float
    ) -> Storyboard:
        """Build from LLM output. Raises ``ValidationError`` with a repair-friendly message."""
        scenes = [Scene(**sd.model_dump(), order=i) for i, sd in enumerate(draft.scenes, start=1)]
        return cls(concept_id=concept_id, target_duration_s=target_duration_s, scenes=scenes)

    @property
    def total_duration_s(self) -> float:
        return round(sum(s.duration_s for s in self.scenes), 3)

    def by_id(self, scene_id: str) -> Scene:
        for s in self.scenes:
            if s.id == scene_id:
                return s
        raise KeyError(scene_id)

    def by_order(self, order: int) -> Scene:
        if not 1 <= order <= len(self.scenes):
            raise KeyError(order)
        return self.scenes[order - 1]

    def neighbors(self, scene_id: str) -> tuple[Scene | None, Scene | None]:
        scene = self.by_id(scene_id)
        i = scene.order - 1
        prev = self.scenes[i - 1] if i > 0 else None
        nxt = self.scenes[i + 1] if i + 1 < len(self.scenes) else None
        return prev, nxt

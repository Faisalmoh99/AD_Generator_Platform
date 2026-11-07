"""The creative rubric (ADR-0024): data in ``config/rubric.toml``. The same criteria score the
concepts (``select``) and the finished ad (the whole-ad critic); the LLM fills scores and code
computes every weighted total, so a judgement is always reproducible from its parts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel

RUBRIC_KEYS: tuple[str, ...] = (
    "insight",
    "hook",
    "story_turn",
    "originality",
    "cultural_meaning",
    "product_role",
)


class RubricCriterion(DomainModel):
    weight: float = Field(gt=0, le=1)
    label: str = Field(min_length=1, max_length=60)
    definition: str = Field(min_length=1, max_length=400)
    concept_question: str = Field(min_length=1, max_length=300)
    video_question: str = Field(min_length=1, max_length=300)


class Rubric(DomainModel):
    cliche_penalty: float = Field(ge=0, le=5)
    novelty_penalty: float = Field(default=0.0, ge=0, le=5)
    criteria: dict[str, RubricCriterion]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if tuple(self.criteria) != RUBRIC_KEYS:
            raise ValueError(f"rubric criteria must be exactly {RUBRIC_KEYS} in that order")
        total = sum(c.weight for c in self.criteria.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"rubric weights must sum to 1, got {total:g}")
        return self

    @property
    def weights(self) -> dict[str, float]:
        return {k: c.weight for k, c in self.criteria.items()}

    def weighted(self, scores: Mapping[str, float], *, cliche: bool = False) -> float:
        """Weighted total on the 1–10 scale, minus the cliché penalty when flagged."""
        total = sum(float(scores[k]) * c.weight for k, c in self.criteria.items())
        return round(total - (self.cliche_penalty if cliche else 0.0), 4)

    def prompt_block(self, kind: Literal["concept", "video"]) -> str:
        lines = []
        for key, c in self.criteria.items():
            q = c.concept_question if kind == "concept" else c.video_question
            lines.append(f"- {key} ({c.label}): {c.definition} Ask: {q}")
        return "\n".join(lines)

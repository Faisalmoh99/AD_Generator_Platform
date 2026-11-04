"""QA critic verdicts. The pass/fail decision is computed by code, never trusted from the LLM."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from adsmith.domain.base import DomainModel

IssueCategory = Literal[
    "product", "character", "lighting", "composition", "motion", "continuity", "artifact", "safety"
]
# Issues in these categories mean the *look* is wrong, so the keyframe must be regenerated too.
KEYFRAME_ISSUE_CATEGORIES: frozenset[str] = frozenset({"product", "character", "composition"})
# The critic sees the keyframe as image 0 and the clip frames as images 1..n.
KEYFRAME_IMAGE_INDEX = 0


class QAChecks(DomainModel):
    product_visible: bool
    matches_shot_spec: bool
    lighting_matches: bool
    continuity_ok: bool
    artifacts_free: bool
    brand_safe: bool


class QAIssue(DomainModel):
    category: IssueCategory
    severity: Literal["minor", "major", "blocker"]
    observation: Annotated[str, Field(min_length=1, max_length=400)]
    fix: Annotated[str, Field(min_length=1, max_length=400)] = Field(
        description="Actionable change to the generation prompt."
    )
    frame_index: int | None = Field(default=None, ge=0)


class QAVerdictDraft(DomainModel):
    """``qa_critic`` output schema."""

    score: float = Field(ge=0, le=1)
    checks: QAChecks
    issues: list[QAIssue] = Field(default_factory=list, max_length=10)
    revised_prompt_hint: str = Field(default="", max_length=800)
    frames_inspected: int = Field(ge=0)


class QAVerdict(QAVerdictDraft):
    threshold: float = Field(ge=0, le=1)

    @classmethod
    def from_draft(cls, draft: QAVerdictDraft, *, threshold: float) -> QAVerdict:
        return cls(**draft.model_dump(), threshold=threshold)

    @property
    def passed(self) -> bool:
        """Deterministic: score at or above threshold, no blocker, and safety must hold."""
        no_blocker = all(i.severity != "blocker" for i in self.issues)
        return self.score >= self.threshold and no_blocker and self.checks.brand_safe

    @property
    def needs_new_keyframe(self) -> bool:
        """The still itself is wrong: a non-minor look issue (product/character/composition), or
        any non-minor non-motion issue the critic located in image 0, which is the keyframe
        (e.g. key light from the wrong side is a "lighting" issue, but it lives in the still)."""
        return any(
            i.severity != "minor"
            and (
                i.category in KEYFRAME_ISSUE_CATEGORIES
                or (i.frame_index == KEYFRAME_IMAGE_INDEX and i.category != "motion")
            )
            for i in self.issues
        )

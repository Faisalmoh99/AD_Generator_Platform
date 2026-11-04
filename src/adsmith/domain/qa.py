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
# Two or more major issues fail a clip even when the score clears the threshold.
MAX_MAJOR_ISSUES = 2


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
    def fail_reasons(self) -> list[str]:
        """Why this verdict fails, decided by code (ADR-0016). Empty ⇒ passed."""
        reasons: list[str] = []
        if self.score < self.threshold:
            reasons.append(f"score {self.score:.2f} < threshold {self.threshold:.2f}")
        if not self.checks.product_visible:
            reasons.append("product not clearly readable in the frames the viewer sees")
        if not self.checks.continuity_ok:
            reasons.append("continuity broken (character or product differs from the bible)")
        if not self.checks.brand_safe:
            reasons.append("not brand-safe")
        majors = sum(1 for i in self.issues if i.severity == "major")
        if majors >= MAX_MAJOR_ISSUES:
            reasons.append(f"{majors} major issues (limit {MAX_MAJOR_ISSUES - 1})")
        if any(i.severity == "blocker" for i in self.issues):
            reasons.append("blocker issue")
        return reasons

    @property
    def passed(self) -> bool:
        return not self.fail_reasons

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

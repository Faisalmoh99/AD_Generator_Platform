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
# Severity calibration (ADR-0016 addendum 3). HARD checks can fail a shot on their own; SOFT
# issue categories (light side, background objects, placement, exact composition, negative-space
# precision) only lower the score: code caps their severity at "minor", whatever the LLM said.
HARD_CHECKS: tuple[str, ...] = ("product_visible", "continuity_ok", "text_free", "brand_safe")
SOFT_CATEGORIES: frozenset[str] = frozenset({"lighting", "composition"})


class QAChecks(DomainModel):
    product_visible: bool
    matches_shot_spec: bool
    lighting_matches: bool
    continuity_ok: bool
    artifacts_free: bool
    brand_safe: bool
    # No letters, words, logos or garbled text anywhere in frame (beyond the bible's branding
    # text). Defaults to True so verdicts stored before this check existed still load.
    text_free: bool = True


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
    # True when the clip was animated from a keyframe the gate already approved: clip problems
    # then regenerate the keyframe only if the critic located them in the keyframe itself.
    keyframe_approved: bool = False

    @classmethod
    def from_draft(
        cls, draft: QAVerdictDraft, *, threshold: float, keyframe_approved: bool = False
    ) -> QAVerdict:
        """Applies the severity calibration: soft-category issues are capped at minor."""
        data = draft.model_dump()
        for issue in data["issues"]:
            if issue["category"] in SOFT_CATEGORIES and issue["severity"] != "minor":
                issue["severity"] = "minor"
        return cls(**data, threshold=threshold, keyframe_approved=keyframe_approved)

    @property
    def fail_reasons(self) -> list[str]:
        """Why this verdict fails, decided by code (ADR-0016 addendum 3): any HARD check false, any
        blocker (soft categories are capped at minor, so never blockers), or score < threshold."""
        reasons: list[str] = []
        if self.score < self.threshold:
            reasons.append(f"score {self.score:.2f} < threshold {self.threshold:.2f}")
        hard = {
            "product_visible": "product not clearly readable",
            "continuity_ok": "product or character identity differs from the bible",
            "text_free": "text, logo or garbled letters in frame",
            "brand_safe": "not brand-safe",
        }
        reasons += [msg for check, msg in hard.items() if not getattr(self.checks, check)]
        if any(i.severity == "blocker" for i in self.issues):  # soft issues are never blockers
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
        in_keyframe = [
            i
            for i in self.issues
            if i.severity != "minor"
            and i.frame_index == KEYFRAME_IMAGE_INDEX
            and i.category != "motion"
        ]
        if self.keyframe_approved:
            return bool(in_keyframe)
        return bool(in_keyframe) or any(
            i.severity != "minor" and i.category in KEYFRAME_ISSUE_CATEGORIES for i in self.issues
        )

"""Scenes: shot spec, prompt, the per-scene sub-state machine, versioning and content hashing.

Scene sub-state machine (ADR-0001). Transitions happen only through the methods below, which
check legality against ``SCENE_TRANSITIONS`` and re-validate invariants once at the end:

    pending ──start──▶ generating ──complete_keyframe──▶ pending (phase=clip)
                         │  └──complete_clip──▶ qa_review ──approve──▶ approved
                         │                         └──reject──▶ pending (phase clip|keyframe)
                         └──release_lease──▶ pending        (crash recovery)
    any active ──fail──▶ failed        any ──mark_stale──▶ stale ──requeue──▶ pending
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel, new_id, stable_hash, utcnow
from adsmith.domain.enums import (
    GLOBAL_BIBLE_SECTIONS,
    BibleSection,
    CameraMove,
    FailureKind,
    ScenePhase,
    ScenePurpose,
    SceneStatus,
    ShotType,
    Speed,
    StaleFrom,
    TimeOfDay,
)
from adsmith.domain.generation import Artifact, GenerationAttempt
from adsmith.domain.qa import QAVerdict

# 500, not 300: real runs at DP-level detail (Kelvin, direction, practicals) hit 300 and
# cost a repair each time.
SpecText = Annotated[str, Field(min_length=1, max_length=500)]
MIN_SCENE_S = 1.5
# Absolute bound for any scene: a single-take ad is one scene up to the model's single-shot
# limit (Kling: 15 s). In multi-scene ads each scene stays at most MULTI_SCENE_MAX_S
# (enforced by the storyboard node, where the scene count is known).
MAX_SCENE_S = 15.0
MULTI_SCENE_MAX_S = 8.0


class IllegalTransitionError(ValueError):
    """A scene was asked to move along an edge the state machine does not have."""


class ShotSpec(DomainModel):
    """Cinematography for one scene. Free-text fields are English (generation-facing)."""

    shot_type: ShotType
    camera_movement: CameraMove
    lens: SpecText = Field(description="e.g. '85mm, f/1.8, shallow depth of field'")
    lighting: SpecText
    time_of_day: TimeOfDay
    composition: SpecText
    mood: SpecText
    subject_action: SpecText
    setting: SpecText
    color_notes: str | None = Field(default=None, max_length=300)
    speed: Speed = Speed.NORMAL


class ScenePromptDraft(DomainModel):
    """``prompt_writer`` output. English only. Adapters map it onto each video model's payload."""

    keyframe_prompt: Annotated[str, Field(min_length=40, max_length=2500)] = Field(
        description="One still frame: subject, frozen action, setting, framing, light, grade, mood."
    )
    motion_prompt: Annotated[str, Field(min_length=10, max_length=600)] = Field(
        description="Only what moves: camera move, subject motion, atmosphere; ≤ 50 words."
    )
    negative_prompt: str = Field(default="", max_length=800)


class ScenePrompt(DomainModel):
    """Provider-agnostic generation prompt. Adapters map it onto each model's payload."""

    keyframe_prompt: Annotated[str, Field(min_length=1, max_length=2500)]
    motion_prompt: Annotated[str, Field(min_length=1, max_length=1200)]
    negative_prompt: str = Field(default="", max_length=800)
    template_version: str
    bible_version: int = Field(ge=1)


class SceneDraft(DomainModel):
    """One scene as produced by the ``storyboard`` node."""

    purpose: ScenePurpose
    description: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        description="What happens, in the user's language (shown to the user)."
    )
    duration_s: float = Field(ge=MIN_SCENE_S, le=MAX_SCENE_S)
    shot: ShotSpec
    # Fail-safe defaults (ADR-0010): a false negative silently breaks continuity after a bible
    # edit, a false positive only costs one regeneration. Code upgrades them further
    # (domain/scene_flags.py) but never downgrades.
    shows_character: bool = Field(
        default=True,
        description="True if the character (or any part: hands, face, silhouette) is on screen. "
        "When in doubt, mark true.",
    )
    shows_product: bool = Field(
        default=True,
        description="True if the product (bottle, cap, label, packaging) is on screen. "
        "When in doubt, mark true.",
    )


class SceneVersion(DomainModel):
    """Immutable snapshot of a scene's spec + prompt before a change (edit or regeneration)."""

    version: int = Field(ge=1)
    purpose: ScenePurpose
    description: str
    duration_s: float
    shot: ShotSpec
    shows_character: bool
    shows_product: bool
    prompt: ScenePrompt | None
    reason: str = Field(max_length=500)
    patch_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class SceneFailure(DomainModel):
    kind: FailureKind
    reason_user: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        description="Plain-language reason in the user's language."
    )
    reason_debug: str = Field(default="", max_length=4000)
    at: datetime = Field(default_factory=utcnow)


# ------------------------------------------------------------------------------ hashing scheme
# content_hash = the *creative spec*: what this scene should be. It is the input to everything
# downstream, so "spec unchanged" ⇔ "no regeneration needed" (up to bible changes, which the blast
# radius handles explicitly, and prompt-template changes, which the request cache key handles).
SPEC_FIELDS: frozenset[str] = frozenset(
    {"purpose", "description", "duration_s", "shot", "shows_character", "shows_product"}
)
# Fields that only affect *motion*. Changing only these keeps the keyframe (StaleFrom.CLIP).
# Shared with the blast-radius algorithm so the two can never disagree.
MOTION_SCENE_FIELDS: frozenset[str] = frozenset({"duration_s"})
MOTION_SHOT_FIELDS: frozenset[str] = frozenset({"camera_movement", "speed"})

SCENE_TRANSITIONS: dict[SceneStatus, frozenset[SceneStatus]] = {
    SceneStatus.PENDING: frozenset({SceneStatus.GENERATING, SceneStatus.FAILED, SceneStatus.STALE}),
    SceneStatus.GENERATING: frozenset(
        {SceneStatus.PENDING, SceneStatus.QA_REVIEW, SceneStatus.FAILED, SceneStatus.STALE}
    ),
    SceneStatus.QA_REVIEW: frozenset(
        {SceneStatus.APPROVED, SceneStatus.PENDING, SceneStatus.FAILED, SceneStatus.STALE}
    ),
    SceneStatus.APPROVED: frozenset({SceneStatus.STALE}),
    # failed → approved only through ``accept_by_human`` (a logged human decision).
    SceneStatus.FAILED: frozenset({SceneStatus.STALE, SceneStatus.APPROVED}),
    SceneStatus.STALE: frozenset({SceneStatus.PENDING, SceneStatus.STALE}),
}


class HumanOverride(DomainModel):
    """A human accepted a clip the critic rejected. Logged in the trace; the critic's failing
    verdict stays on the scene (``qa``) as the honest record."""

    decision: Literal["accept_clip"] = "accept_clip"
    reason: Annotated[str, Field(min_length=3, max_length=500)]
    critic_score: float = Field(ge=0, le=1)
    at: datetime = Field(default_factory=utcnow)


class Scene(SceneDraft):
    id: str = Field(default_factory=lambda: new_id("scn"))
    order: int = Field(ge=1)
    prompt: ScenePrompt | None = None
    status: SceneStatus = SceneStatus.PENDING
    phase: ScenePhase = ScenePhase.KEYFRAME
    stale_from: StaleFrom | None = None
    keyframe: Artifact | None = None
    clip: Artifact | None = None
    qa: QAVerdict | None = None
    # Keyframe QA gate (ADR-0003 addendum): the verdict on the keyframe alone, and the sha256 of
    # the keyframe it approved. Video is generated only from an approved keyframe.
    keyframe_qa: QAVerdict | None = None
    keyframe_approved_sha: str | None = None
    version: int = Field(default=1, ge=1)
    history: list[SceneVersion] = Field(default_factory=list)
    attempts: list[GenerationAttempt] = Field(default_factory=list)
    failure: SceneFailure | None = None
    human_override: HumanOverride | None = None

    # ----------------------------------------------------------------------- invariants
    @model_validator(mode="after")
    def _invariants(self) -> Self:
        self.check_invariants()
        return self

    def check_invariants(self) -> None:
        s = self.status
        if (s is SceneStatus.FAILED) != (self.failure is not None):
            raise ValueError("status 'failed' and a failure record must go together")
        if (s is SceneStatus.STALE) != (self.stale_from is not None):
            raise ValueError("status 'stale' and stale_from must go together")
        if s is SceneStatus.QA_REVIEW and self.clip is None:
            raise ValueError("qa_review requires a clip")
        # The pass rule is applied at decision time (approve()), not on every load: a scene approved
        # under an older rule must stay loadable when the rule tightens (history is not re-judged).
        if s is SceneStatus.APPROVED and (self.clip is None or self.qa is None):
            raise ValueError("approved requires a clip and a QA verdict")
        active = s in {SceneStatus.PENDING, SceneStatus.GENERATING}
        if self.phase is ScenePhase.CLIP and active and self.keyframe is None:
            raise ValueError("clip phase requires a keyframe")
        if self.keyframe is not None and self.prompt is None:
            raise ValueError("a keyframe cannot exist without the prompt that produced it")
        versions = [v.version for v in self.history]
        if versions != sorted(set(versions)) or (versions and versions[-1] >= self.version):
            raise ValueError("history versions must be strictly increasing and below version")

    # ----------------------------------------------------------------------- hashing
    def content_hash(self) -> str:
        """Hash of the creative spec (SPEC_FIELDS). Excludes identity, status and derived data."""
        return stable_hash(
            "scene.content.v1", self.model_dump(mode="json", include=set(SPEC_FIELDS))
        )

    def keyframe_spec_hash(self) -> str:
        """Hash of the spec minus motion-only fields: unchanged ⇒ the keyframe is still valid."""
        spec: dict[str, Any] = self.model_dump(
            mode="json", include=set(SPEC_FIELDS - MOTION_SCENE_FIELDS)
        )
        spec["shot"] = {k: v for k, v in spec["shot"].items() if k not in MOTION_SHOT_FIELDS}
        return stable_hash("scene.keyframe_spec.v1", spec)

    def uses_bible_sections(self) -> frozenset[BibleSection]:
        used = set(GLOBAL_BIBLE_SECTIONS)
        if self.shows_character:
            used.add(BibleSection.CHARACTER)
        if self.shows_product:
            used.add(BibleSection.PRODUCT)
        return frozenset(used)

    # ----------------------------------------------------------------------- transitions
    def _move(self, to: SceneStatus) -> None:
        if to not in SCENE_TRANSITIONS[self.status]:
            raise IllegalTransitionError(f"scene {self.order}: {self.status} → {to} is not allowed")
        self.status = to

    def start(self) -> None:
        """pending → generating for the current phase."""
        if self.prompt is None:
            raise IllegalTransitionError(f"scene {self.order}: cannot generate without a prompt")
        self._move(SceneStatus.GENERATING)
        self.check_invariants()

    def complete_keyframe(self, artifact: Artifact) -> None:
        if self.phase is not ScenePhase.KEYFRAME or artifact.kind != "image":
            raise IllegalTransitionError("complete_keyframe needs keyframe phase and an image")
        self._move(SceneStatus.PENDING)
        self.keyframe, self.phase = artifact, ScenePhase.CLIP
        self.check_invariants()

    def complete_clip(self, artifact: Artifact) -> None:
        if self.phase is not ScenePhase.CLIP or artifact.kind != "video":
            raise IllegalTransitionError("complete_clip needs clip phase and a video")
        self._move(SceneStatus.QA_REVIEW)
        self.clip = artifact
        self.check_invariants()

    def approve(self, verdict: QAVerdict) -> None:
        if not verdict.passed:
            raise IllegalTransitionError("cannot approve with a failing verdict")
        self._move(SceneStatus.APPROVED)
        self.qa = verdict
        self.check_invariants()

    def reject(self, verdict: QAVerdict) -> None:
        """QA rejected: requeue. Look problems also discard the keyframe."""
        if verdict.passed:
            raise IllegalTransitionError("cannot reject with a passing verdict")
        self._move(SceneStatus.PENDING)
        self.qa, self.clip = verdict, None
        if verdict.needs_new_keyframe:
            self.keyframe, self.phase = None, ScenePhase.KEYFRAME
        self.check_invariants()

    @property
    def keyframe_approved(self) -> bool:
        return self.keyframe is not None and self.keyframe_approved_sha == self.keyframe.sha256

    def approve_keyframe(self, verdict: QAVerdict) -> None:
        if not verdict.passed or self.keyframe is None or self.phase is not ScenePhase.CLIP:
            raise IllegalTransitionError("approve_keyframe needs a passing verdict and a keyframe")
        self.keyframe_qa, self.keyframe_approved_sha = verdict, self.keyframe.sha256
        self.check_invariants()

    def reject_keyframe(self, verdict: QAVerdict) -> None:
        """Keyframe gate rejected the still: discard it before any video is paid for."""
        if verdict.passed:
            raise IllegalTransitionError("cannot reject a keyframe with a passing verdict")
        if self.status is not SceneStatus.PENDING or self.phase is not ScenePhase.CLIP:
            raise IllegalTransitionError("reject_keyframe needs a pending scene with a keyframe")
        if self.clip is not None:
            raise IllegalTransitionError("the keyframe gate runs before any clip exists")
        self.keyframe_qa, self.keyframe_approved_sha = verdict, None
        self.keyframe, self.phase = None, ScenePhase.KEYFRAME
        self.check_invariants()

    def release_lease(self) -> None:
        """generating → pending, used on resume after a crash (the cache prevents re-spend)."""
        self._move(SceneStatus.PENDING)
        self.check_invariants()

    def accept_by_human(self, clip: Artifact, verdict: QAVerdict, reason: str) -> None:
        """Human override: accept a QA-rejected clip. Only from failed(quality_rejected)."""
        if self.status is not SceneStatus.FAILED or self.failure is None:
            raise IllegalTransitionError("only a failed scene can be accepted by a human")
        if self.failure.kind is not FailureKind.QUALITY_REJECTED or clip.kind != "video":
            raise IllegalTransitionError("only a QA-rejected clip can be accepted by a human")
        self._move(SceneStatus.APPROVED)
        self.clip, self.qa, self.failure = clip, verdict, None
        self.human_override = HumanOverride(reason=reason, critic_score=verdict.score)
        self.check_invariants()

    def fail(self, failure: SceneFailure) -> None:
        self._move(SceneStatus.FAILED)
        self.failure = failure
        self.check_invariants()

    def mark_stale(self, depth: StaleFrom) -> None:
        """Invalidate derived artifacts down to ``depth``. Deeper staleness always wins."""
        self._move(SceneStatus.STALE)
        self.stale_from = StaleFrom.deepest(self.stale_from, depth)
        self.failure, self.qa, self.clip = None, None, None
        if self.stale_from.depth >= StaleFrom.KEYFRAME.depth:
            self.keyframe = None
        if self.stale_from is StaleFrom.PROMPT:
            self.prompt = None
        self.check_invariants()

    def set_prompt(self, prompt: ScenePrompt) -> None:
        """Allowed for fresh scenes, prompt-stale scenes, and QA-driven revisions while pending."""
        if self.status not in {SceneStatus.PENDING, SceneStatus.STALE}:
            raise IllegalTransitionError(f"cannot set prompt while {self.status}")
        old = self.prompt
        self.prompt = prompt
        # The keyframe is a function of keyframe_prompt only: a motion-only revision keeps it.
        if old is None or old.keyframe_prompt != prompt.keyframe_prompt:
            self.keyframe = None
            if self.status is SceneStatus.PENDING:
                self.phase = ScenePhase.KEYFRAME
        self.check_invariants()

    def requeue(self) -> None:
        """stale → pending, resuming at the phase implied by the invalidation depth."""
        if self.stale_from is None:
            raise IllegalTransitionError("requeue requires a stale scene")
        if self.prompt is None:
            raise IllegalTransitionError("prompt-stale scenes need a new prompt before requeue")
        needs_keyframe = self.keyframe is None
        self._move(SceneStatus.PENDING)
        self.stale_from = None
        self.phase = ScenePhase.KEYFRAME if needs_keyframe else ScenePhase.CLIP
        self.check_invariants()

    # ----------------------------------------------------------------------- versioning
    def snapshot(self, reason: str, patch_id: str | None = None) -> SceneVersion:
        return SceneVersion(
            version=self.version,
            purpose=self.purpose,
            description=self.description,
            duration_s=self.duration_s,
            shot=self.shot.model_copy(deep=True),
            shows_character=self.shows_character,
            shows_product=self.shows_product,
            prompt=self.prompt.model_copy(deep=True) if self.prompt else None,
            reason=reason,
            patch_id=patch_id,
        )

    def bump_version(self, reason: str, patch_id: str | None = None) -> None:
        """Archive the current spec+prompt in history before it changes."""
        self.history.append(self.snapshot(reason, patch_id))
        self.version += 1
        self.check_invariants()

    def last_attempt(self, phase: ScenePhase | None = None) -> GenerationAttempt | None:
        for att in reversed(self.attempts):
            if phase is None or att.phase is phase:
                return att
        return None

    def attempts_for_current_version(self) -> int:
        return sum(1 for a in self.attempts if a.scene_version == self.version)

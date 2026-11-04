"""Closed vocabularies. Using enums (not free text) lets prompts, adapters, validators and the
blast-radius algorithm all reason over the same finite set of values."""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    UNDERSTAND = "understand"
    IDEATE = "ideate"
    SELECT = "select"
    AWAITING_CONCEPT_CHOICE = "awaiting_concept_choice"
    STORYBOARD = "storyboard"
    CONTINUITY = "continuity"
    PROMPT = "prompt"
    KEYFRAME = "keyframe"
    AWAITING_STORYBOARD_APPROVAL = "awaiting_storyboard_approval"
    GENERATE = "generate"
    QA = "qa"
    ASSEMBLE = "assemble"
    DONE = "done"
    DONE_PARTIAL = "done_partial"
    ERRORED = "errored"


AWAITING_STAGES: frozenset[Stage] = frozenset(
    {Stage.AWAITING_CONCEPT_CHOICE, Stage.AWAITING_STORYBOARD_APPROVAL}
)
TERMINAL_STAGES: frozenset[Stage] = frozenset({Stage.DONE, Stage.DONE_PARTIAL, Stage.ERRORED})


class SceneStatus(StrEnum):
    PENDING = "pending"
    GENERATING = "generating"
    QA_REVIEW = "qa_review"
    APPROVED = "approved"
    FAILED = "failed"
    STALE = "stale"


class ScenePhase(StrEnum):
    """Which artifact the scene is currently working towards."""

    KEYFRAME = "keyframe"
    CLIP = "clip"


class StaleFrom(StrEnum):
    """Invalidation depth: how far back a stale scene must restart (ADR-0005)."""

    PROMPT = "prompt"  # re-write prompt, new keyframe, new clip
    KEYFRAME = "keyframe"  # keep prompt, new keyframe, new clip
    CLIP = "clip"  # keep prompt + keyframe, new clip only

    @property
    def depth(self) -> int:
        return _STALE_DEPTH[self]

    @staticmethod
    def deepest(a: StaleFrom | None, b: StaleFrom) -> StaleFrom:
        return b if a is None or b.depth > a.depth else a


_STALE_DEPTH: dict[StaleFrom, int] = {StaleFrom.CLIP: 1, StaleFrom.KEYFRAME: 2, StaleFrom.PROMPT: 3}


class ShotType(StrEnum):
    EXTREME_WIDE = "extreme_wide"
    WIDE = "wide"
    MEDIUM_WIDE = "medium_wide"
    MEDIUM = "medium"
    MEDIUM_CLOSE_UP = "medium_close_up"
    CLOSE_UP = "close_up"
    EXTREME_CLOSE_UP = "extreme_close_up"
    INSERT = "insert"
    OVER_THE_SHOULDER = "over_the_shoulder"
    POV = "pov"


class CameraMove(StrEnum):
    STATIC = "static"
    PAN = "pan"
    TILT = "tilt"
    DOLLY_IN = "dolly_in"
    DOLLY_OUT = "dolly_out"
    TRUCK = "truck"
    PEDESTAL = "pedestal"
    CRANE = "crane"
    ORBIT = "orbit"
    HANDHELD = "handheld"
    GIMBAL_FOLLOW = "gimbal_follow"
    PUSH_IN = "push_in"
    PULL_OUT = "pull_out"
    RACK_FOCUS = "rack_focus"


class TimeOfDay(StrEnum):
    DAWN = "dawn"
    DAY = "day"
    GOLDEN_HOUR = "golden_hour"
    DUSK = "dusk"
    NIGHT = "night"
    INTERIOR = "interior"


class Speed(StrEnum):
    NORMAL = "normal"
    SLOW_MOTION = "slow_motion"
    SPEED_RAMP = "speed_ramp"


class ScenePurpose(StrEnum):
    HOOK = "hook"
    BUILD = "build"
    REVEAL = "reveal"
    PRODUCT = "product"
    ENDCARD = "endcard"


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    CONTENT_POLICY = "content_policy"
    QUALITY_REJECTED = "quality_rejected"
    INVALID_OUTPUT = "invalid_output"
    BUDGET_EXCEEDED = "budget_exceeded"
    PERMANENT = "permanent"
    EXHAUSTED = "exhausted"


class RecoveryAction(StrEnum):
    RETRY_BACKOFF = "retry_backoff"
    POLICY_REWRITE = "policy_rewrite"
    QA_REGENERATE = "qa_regenerate"
    SCHEMA_REPAIR = "schema_repair"
    GIVE_UP = "give_up"


class Language(StrEnum):
    EN = "en"
    AR = "ar"


class Dialect(StrEnum):
    NONE = "none"  # English requests
    MSA = "msa"
    SAUDI_NAJDI = "saudi_najdi"
    SAUDI_HIJAZI = "saudi_hijazi"
    GULF = "gulf"
    OTHER = "other"


class BibleSection(StrEnum):
    """Top-level sections of the continuity bible. Scenes declare which ones they depend on."""

    CHARACTER = "character"
    PRODUCT = "product"
    COLOR_GRADE = "color_grade"
    LOCATION = "location"
    MOOD = "mood"
    LIGHTING = "lighting"


GLOBAL_BIBLE_SECTIONS: frozenset[BibleSection] = frozenset(
    {BibleSection.COLOR_GRADE, BibleSection.LOCATION, BibleSection.MOOD, BibleSection.LIGHTING}
)

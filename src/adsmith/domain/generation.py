"""Generated artifacts, generation requests (the cache key) and the attempt log."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from adsmith.domain.base import DomainModel, new_id, stable_hash, utcnow
from adsmith.domain.enums import FailureKind, RecoveryAction, ScenePhase

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Artifact(DomainModel):
    kind: Literal["image", "video", "audio"]
    path: str = Field(min_length=1, description="Relative to the data dir.")
    sha256: Sha256
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_s: float | None = Field(default=None, gt=0)
    provider: str
    remote_url: str | None = None


class GenerationRequest(DomainModel):
    """Everything that determines a generation result. Its hash is the idempotency/cache key.

    Includes provider + model so switching models never returns another model's output, and the
    input keyframe's sha256 so a regenerated keyframe invalidates the clip automatically.
    """

    provider: str
    model: str
    phase: ScenePhase
    prompt: str = Field(min_length=1)
    negative_prompt: str = ""
    params: dict[str, JsonValue] = Field(default_factory=dict)
    input_image_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _clip_needs_keyframe(self) -> Self:
        if self.phase is ScenePhase.CLIP and self.input_image_sha256 is None:
            raise ValueError("image-to-video requests need input_image_sha256")
        return self

    def cache_key(self) -> str:
        return stable_hash("generation.request.v1", self.model_dump(mode="json"))


class GenerationAttempt(DomainModel):
    id: str = Field(default_factory=lambda: new_id("att"))
    scene_id: str
    scene_version: int = Field(ge=1)
    phase: ScenePhase
    attempt_no: int = Field(ge=1)
    provider: str
    model: str
    request_hash: Sha256
    prompt_used: str
    params: dict[str, JsonValue] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    outcome: Literal["success", "cache_hit", "failed"] | None = None
    failure_kind: FailureKind | None = None
    error_message: str | None = Field(default=None, max_length=2000)
    recovery_action: RecoveryAction | None = None
    backoff_s: float | None = Field(default=None, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    artifact: Artifact | None = None
    qa_score: float | None = Field(default=None, ge=0, le=1)
    qa_passed: bool | None = None  # the code-decided verdict (a high score can still fail)
    chaos_injected: bool = False
    injected_fault: FailureKind | None = Field(
        default=None, description="Chaos Mode fault injected into this attempt, if any."
    )

    @model_validator(mode="after")
    def _outcome_consistent(self) -> Self:
        if self.outcome == "failed" and self.failure_kind is None:
            raise ValueError("failed attempts need failure_kind")
        if self.outcome in {"success", "cache_hit"} and self.artifact is None:
            raise ValueError("successful attempts need an artifact")
        if self.outcome is not None and self.finished_at is None:
            raise ValueError("finished attempts need finished_at")
        return self

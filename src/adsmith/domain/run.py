"""``RunState``: the aggregate root that is snapshotted after every transition.

Validation layering (answers "where do cross-object rules live?"):
- **Object-local** invariants live on the object (``Scene`` status/artifact consistency,
  ``Storyboard`` durations vs. its own ``target_duration_s``, ``ConceptSet`` distinctness).
- **Cross-aggregate** invariants live here, on the root that owns all the pieces: the storyboard
  target equals the brief duration, the storyboard belongs to the selected concept, prompts never
  reference a future bible version, and each stage has the data it needs.
Because the root validates on every load, a corrupted or hand-edited snapshot fails loudly at the
boundary instead of deep inside a node.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel, new_id, utcnow
from adsmith.domain.brief import AspectRatio, Brief
from adsmith.domain.budget import BudgetLedger
from adsmith.domain.concept import ConceptSet
from adsmith.domain.continuity import ContinuityBible
from adsmith.domain.edit import ScenePatch
from adsmith.domain.endcard import EndCardCopy
from adsmith.domain.enums import FailureKind, Stage
from adsmith.domain.generation import Artifact
from adsmith.domain.qa import QAVerdict
from adsmith.domain.similarity import detect_language
from adsmith.domain.storyboard import Storyboard

_DURATION_EPS = 1e-6


class RunConfig(DomainModel):
    """Frozen copy of the settings that shaped this run, for reproducibility and resume."""

    interactive: bool = True
    chaos: bool = False
    seed: int = 7
    llm_provider: Literal["scripted", "anthropic", "openai"] = "scripted"
    media_provider: Literal["mock", "fal"] = "mock"
    model_profile: Literal["fast", "balanced", "quality"] = "balanced"
    qa_mode: Literal["mock", "llm"] = "mock"
    qa_threshold: float = Field(default=0.7, ge=0, le=1)
    aspect_ratio: AspectRatio = "9:16"
    target_duration_s: float = Field(default=15.0, gt=0, le=60)
    music_path: str | None = None
    endcard: bool = True
    endcard_s: float = Field(default=2.0, ge=0, le=6)
    partial_mode: Literal["skip", "slate"] = "skip"  # failed scenes in done_partial
    clip_candidates: int = Field(default=1, ge=1, le=4)  # best-of-N clips per scene
    product_reference: bool = False  # locked product still conditions product keyframes
    scripted_scenario: str | None = None  # which canned fixtures the scripted LLM replays
    # Catalog keys (config/models.toml) of the media models: resume never switches model.
    image_model: str | None = None
    video_model: str | None = None
    media_long_side: int | None = Field(default=None, ge=256, le=4096)


class RunError(DomainModel):
    stage: Stage
    kind: FailureKind
    reason_user: str = Field(min_length=1, max_length=500)
    reason_debug: str = Field(default="", max_length=4000)
    at: datetime = Field(default_factory=utcnow)


# Data each stage needs before it can run. Ordered pipeline → cumulative requirements.
_NEEDS_BRIEF = frozenset(set(Stage) - {Stage.UNDERSTAND, Stage.ERRORED})
_NEEDS_CONCEPTS = _NEEDS_BRIEF - {Stage.IDEATE}
_NEEDS_SELECTION = _NEEDS_CONCEPTS - {Stage.SELECT, Stage.AWAITING_CONCEPT_CHOICE}
_NEEDS_STORYBOARD = _NEEDS_SELECTION - {Stage.STORYBOARD}
_NEEDS_BIBLE = _NEEDS_STORYBOARD - {Stage.CONTINUITY}


class ProductReference(DomainModel):
    """The run's locked product still (ADR-0003 addendum 3): one packshot of the bible's product,
    passed by the keyframe gate, that conditions every keyframe showing the product so the
    product is identical everywhere. Tied to a bible version: a product edit makes it stale."""

    bible_version: int = Field(ge=1)
    prompt: str = Field(min_length=1)
    tries: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    artifact: Artifact | None = None  # the approved still (None until one passes the gate)
    rejected: list[Artifact] = Field(default_factory=list)  # kept in the CAS for the record
    verdict: QAVerdict | None = None  # the gate verdict of the last reviewed still

    @property
    def approved(self) -> bool:
        return self.artifact is not None


class RunState(DomainModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    state_version: int = Field(default=0, ge=0, description="Optimistic-lock counter.")
    stage: Stage = Stage.UNDERSTAND
    config: RunConfig
    request: str = Field(min_length=1, max_length=4000)
    brief: Brief | None = None
    concepts: ConceptSet | None = None
    storyboard: Storyboard | None = None
    bible: ContinuityBible | None = None
    bible_history: list[ContinuityBible] = Field(default_factory=list)
    patches: list[ScenePatch] = Field(default_factory=list)
    final: Artifact | None = None
    endcard: EndCardCopy | None = None
    product_reference: ProductReference | None = None
    budget: BudgetLedger
    error: RunError | None = None

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        self.check_invariants()
        return self

    def check_invariants(self) -> None:
        self._check_stage_prerequisites()
        self._check_cross_aggregate()

    def _check_stage_prerequisites(self) -> None:
        s = self.stage
        missing: list[str] = []
        if s in _NEEDS_BRIEF and self.brief is None:
            missing.append("brief")
        if s in _NEEDS_CONCEPTS and self.concepts is None:
            missing.append("concepts")
        if s in _NEEDS_SELECTION and (self.concepts is None or self.concepts.selected_id is None):
            missing.append("selected concept")
        if s in _NEEDS_STORYBOARD and self.storyboard is None:
            missing.append("storyboard")
        if s in _NEEDS_BIBLE and self.bible is None:
            missing.append("continuity bible")
        if s is Stage.DONE and self.final is None:
            missing.append("final video")
        if s is Stage.ERRORED and self.error is None:
            missing.append("error record")
        if missing:
            raise ValueError(f"stage {s} requires: {', '.join(missing)}")

    def _check_cross_aggregate(self) -> None:
        sb, brief = self.storyboard, self.brief
        if (
            sb is not None
            and brief is not None
            and abs(sb.target_duration_s - brief.duration_s) > _DURATION_EPS
        ):
            raise ValueError(
                f"storyboard target {sb.target_duration_s}s ≠ brief {brief.duration_s}s"
            )
        if (
            sb is not None
            and self.concepts is not None
            and sb.concept_id != self.concepts.selected_id
        ):
            raise ValueError("storyboard does not belong to the selected concept")
        if self.bible is not None:
            hist = [b.version for b in self.bible_history]
            if hist != sorted(set(hist)) or (hist and hist[-1] >= self.bible.version):
                raise ValueError("bible_history versions must increase and precede bible.version")
            if sb is not None:
                for scene in sb.scenes:
                    if scene.prompt and scene.prompt.bible_version > self.bible.version:
                        raise ValueError(f"scene {scene.order} prompt references a future bible")

    @property
    def language(self) -> str:
        """User-facing language: the brief's, or the request's script before a brief exists."""
        return self.brief.language.value if self.brief else detect_language(self.request)

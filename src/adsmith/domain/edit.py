"""Natural-language edits as typed patches, conflict reports and the computed blast radius."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from adsmith.domain.base import DomainModel, new_id, utcnow
from adsmith.domain.enums import (
    BibleSection,
    CameraMove,
    Language,
    ShotType,
    Speed,
    StaleFrom,
    TimeOfDay,
)
from adsmith.domain.scene import MAX_SCENE_S, MIN_SCENE_S, SpecText


class ShotSpecPatch(DomainModel):
    """Every ShotSpec field, optional. ``None`` = unchanged."""

    shot_type: ShotType | None = None
    camera_movement: CameraMove | None = None
    lens: SpecText | None = None
    lighting: SpecText | None = None
    time_of_day: TimeOfDay | None = None
    composition: SpecText | None = None
    mood: SpecText | None = None
    subject_action: SpecText | None = None
    setting: SpecText | None = None
    color_notes: SpecText | None = None
    speed: Speed | None = None

    def changed_fields(self) -> frozenset[str]:
        return frozenset(k for k, v in self.model_dump().items() if v is not None)


class SceneRef(DomainModel):
    """How the user referred to a scene. Resolved to ids deterministically (edit/parser.py)."""

    kind: Literal["ordinal", "id", "relative", "all"]
    value: Annotated[str, Field(max_length=200)] = Field(
        description="ordinal: '2' or 'last'; id: scene id; relative: the user's words; all: ''"
    )


class ScenePatchDraft(DomainModel):
    """``edit_parser`` LLM output."""

    language: Language
    targets: list[SceneRef] = Field(default_factory=list, max_length=6)
    shot: ShotSpecPatch = Field(default_factory=ShotSpecPatch)
    duration_s: float | None = Field(default=None, ge=MIN_SCENE_S, le=MAX_SCENE_S)
    description_change: str | None = Field(default=None, max_length=500)
    bible_changes: dict[str, Annotated[str, Field(max_length=300)]] | None = Field(
        default=None, description="Dotted paths into the bible, e.g. 'character.wardrobe'."
    )
    confidence: float = Field(ge=0, le=1)
    clarification_needed: str | None = Field(
        default=None, max_length=300, description="A question in the user's language, or null."
    )

    @field_validator("bible_changes")
    @classmethod
    def _known_sections(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v:
            valid = {s.value for s in BibleSection}
            bad = [k for k in v if k.split(".", 1)[0] not in valid]
            if bad:
                raise ValueError(f"unknown bible sections {bad}; valid: {sorted(valid)}")
        return v

    @model_validator(mode="after")
    def _something_to_do(self) -> Self:
        if self.clarification_needed:
            return self
        has_change = bool(
            self.shot.changed_fields()
            or self.duration_s is not None
            or self.description_change
            or self.bible_changes
        )
        if not has_change:
            raise ValueError("patch changes nothing; set fields or ask a clarification question")
        if not self.targets and not self.bible_changes:
            raise ValueError("scene-level changes need at least one target scene")
        return self

    def changed_bible_sections(self) -> frozenset[BibleSection]:
        return frozenset(BibleSection(k.split(".", 1)[0]) for k in (self.bible_changes or {}))


class ScenePatch(ScenePatchDraft):
    id: str = Field(default_factory=lambda: new_id("pch"))
    raw_instruction: Annotated[str, Field(min_length=1, max_length=2000)]
    resolved_scene_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class Conflict(DomainModel):
    against: Literal["bible", "prev_scene", "next_scene", "duration_budget"]
    scene_id: str | None = None
    field: Annotated[str, Field(max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=400)]
    severity: Literal["info", "warning", "blocking"]


Resolution = Literal["apply_local", "propagate_neighbors", "update_bible", "reject"]


class ConflictReport(DomainModel):
    conflicts: list[Conflict] = Field(default_factory=list, max_length=12)
    suggested_resolution: Resolution = "apply_local"
    explanation: str = Field(default="", max_length=800)


class BlastRadius(DomainModel):
    patch_id: str
    affected: dict[str, StaleFrom] = Field(description="scene_id → invalidation depth")
    bible_bump: bool = False
    duration_rebalance: dict[str, float] = Field(default_factory=dict)
    explanation: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list)

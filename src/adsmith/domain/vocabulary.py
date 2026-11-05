"""Shot vocabulary (PLAN_V2 item 2, ADR-0018): the closed set of shots a montage is built from.

Data lives in ``config/shot_vocabulary.toml``. A scene that names an entry (``Scene.vocab``) takes
its ``shows_character`` / ``shows_product`` flags from the entry (the vocabulary is the source of
truth, so a product-free "spray burst" is never upgraded to a product shot by text matching), and
its keyframe identity mode (item 3).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel
from adsmith.domain.enums import CameraMove, ShotType, Speed
from adsmith.domain.formats import Framing

# crop: a macro cut from the locked packshot in code, then relit onto black by the reference model
# (the product pixels are the packshot's own: identity exact by construction).
IdentityMode = Literal["none", "direct", "composite", "crop"]
CropRegion = Literal["cap", "edge"]


class ShotVocab(DomainModel):
    label: str = Field(min_length=1, max_length=80)
    framing: Framing
    needs_product: bool
    shows_person: bool
    identity_mode: IdentityMode
    key_shot: bool = False
    # The composition IS the spec (the event is the point of the shot): a missing or different
    # event fails the keyframe gate and clip QA instead of only lowering the score (STOP 1: a
    # missing water ribbon passed at 0.72 as a "minor" composition issue).
    composition_hard: bool = False
    shot_type: ShotType
    camera: CameraMove
    speed: Speed
    lighting: str = Field(min_length=1, max_length=300)
    motion: str = Field(min_length=1, max_length=300)
    keyframe_hint: str = Field(min_length=1, max_length=400)
    # Shot-specific change to the locked product, told to the reference model and the gate
    # (e.g. "the cap is off": otherwise a missing cap is a hard identity mismatch).
    reference_note: str = Field(default="", max_length=300)
    # identity_mode = "crop": which part of the packshot the macro is cut from.
    crop: CropRegion | None = None
    fallback: str
    avoid_on: list[str] = Field(default_factory=list)

    @property
    def composition_is_spec(self) -> bool:
        return self.composition_hard or self.key_shot

    @model_validator(mode="after")
    def _identity_matches_product(self) -> Self:
        if (self.identity_mode == "none") == self.needs_product:
            raise ValueError("identity_mode is 'none' exactly when the shot has no product")
        if (self.identity_mode == "crop") != (self.crop is not None):
            raise ValueError("crop (cap|edge) is set exactly when identity_mode is 'crop'")
        return self


class VocabLibrary(DomainModel):
    shots: dict[str, ShotVocab] = Field(min_length=1)

    @model_validator(mode="after")
    def _fallbacks_exist(self) -> Self:
        missing = {k: v.fallback for k, v in self.shots.items() if v.fallback not in self.shots}
        if missing:
            raise ValueError(f"unknown fallback entries: {missing}")
        return self

    def get(self, name: str | None) -> ShotVocab | None:
        return None if name is None else self.shots.get(name)

    def prompt_block(self) -> str:
        """The whole vocabulary as prompt text for the storyboard."""
        lines = []
        for name, v in self.shots.items():
            shows = ("product" if v.needs_product else "no product") + (
                ", a person (" + v.framing + ")" if v.shows_person else ""
            )
            key = "; KEY SHOT" if v.key_shot else ""
            lines.append(
                f"- {name}: {v.label} [{shows}; {v.shot_type.value}, camera {v.camera.value}, "
                f"{v.speed.value}{key}]. Motion: {v.motion}."
            )
        return "\n".join(lines)


def entry_block(name: str | None, v: ShotVocab | None) -> str:
    """One entry as prompt text for the prompt writer and the rewrite."""
    if name is None or v is None:
        return "(no vocabulary entry: follow the scene specification)"
    who = "a person in detail framing only" if v.shows_person else "no person"
    what = "the product" if v.needs_product else "NO product: no bottle, no hand"
    return "\n".join(
        [
            f"Entry: {name} ({v.label}); shows {what}; {who}",
            f"Keyframe: {v.keyframe_hint}",
            f"Lighting: {v.lighting}",
            f"Motion: {v.motion}",
            f"Camera: {v.camera.value}; speed: {v.speed.value}",
        ]
    )

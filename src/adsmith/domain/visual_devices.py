"""Visual devices (ADR-0029), data in ``config/visual_devices.toml``.

The culture bank gives an idea its meaning; a visual device gives it the one image only a moving
picture can show (make the invisible visible, a transformation, a trail to follow, ...). Ideate
picks one device per concept from the devices that fit the product's category; code checks the
id, that the candidates use different devices, and that no concept copies a worked exemplar.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel
from adsmith.domain.concept import motif_overlap

Text = Annotated[str, Field(min_length=1, max_length=400)]
Renderability = Literal["proven", "likely", "unproven"]
# A concept sharing this many motifs with a worked exemplar is a copy (M4a: a concept copied the
# illustrative example in the ideate prompt).
EXEMPLAR_COPY_MOTIFS = 2


class VisualDevice(DomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    name: Annotated[str, Field(min_length=1, max_length=60)]
    how_it_works: Text
    why_video_only: Text
    categories: list[str] = Field(min_length=1, max_length=8)
    seedance: Text
    renderability: Renderability
    proven_by: Annotated[str, Field(max_length=200)] | None = None
    min_s: float = Field(ge=1.0, le=6.0)

    def fits(self, categories: Sequence[str]) -> bool:
        return "any" in self.categories or bool(set(categories) & set(self.categories))


class Exemplar(DomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    devices: list[str] = Field(min_length=1, max_length=3)
    idea: Text
    why: Text
    motifs: list[Annotated[str, Field(min_length=1, max_length=30)]] = Field(
        min_length=2, max_length=6
    )


class DeviceLibrary(DomainModel):
    devices: list[VisualDevice] = Field(min_length=1)
    exemplars: list[Exemplar] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [d.id for d in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("visual device ids must be unique")
        unknown = {x for e in self.exemplars for x in e.devices} - set(ids)
        if unknown:
            raise ValueError(f"exemplars name unknown devices: {sorted(unknown)}")
        return self

    def get(self, device_id: str) -> VisualDevice | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def for_categories(self, categories: Sequence[str]) -> list[VisualDevice]:
        """The devices that fit the brief's categories (every device marked "any" fits)."""
        return [d for d in self.devices if d.fits(categories)]

    def prompt_block(self, devices: Sequence[VisualDevice]) -> str:
        lines = []
        for d in devices:
            proof = f" ({d.proven_by})" if d.proven_by else ""
            lines.append(
                f"- {d.id} ({d.name}): {d.how_it_works} Only video: {d.why_video_only} "
                f"Renders: {d.renderability}{proof}; the key shot needs at least {d.min_s:g} s. "
                f"Wording that renders: {d.seedance}"
            )
        return "\n".join(lines) or "(none)"

    def exemplars_block(self) -> str:
        if not self.exemplars:
            return "(none)"
        return "\n".join(
            f"- {e.idea} Device: {', '.join(e.devices)}. Why it works: {e.why}"
            for e in self.exemplars
        )

    def copied_exemplar(self, motifs: Sequence[str]) -> Exemplar | None:
        """The exemplar a concept's motifs copy (2+ shared motifs), if any."""
        return next(
            (e for e in self.exemplars if motif_overlap(motifs, e.motifs) >= EXEMPLAR_COPY_MOTIFS),
            None,
        )

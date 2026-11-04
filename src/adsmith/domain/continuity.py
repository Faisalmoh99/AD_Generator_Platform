"""The continuity bible: the single source of visual truth injected into every scene prompt."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from adsmith.domain.base import DomainModel, stable_hash
from adsmith.domain.enums import BibleSection

Concrete = Annotated[str, Field(min_length=1, max_length=300)]
HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]


class CharacterSpec(DomainModel):
    description: Concrete
    age_range: Annotated[str, Field(max_length=40)]
    appearance: Concrete
    hair: Concrete
    grooming: Concrete
    build: Concrete
    wardrobe: list[Concrete] = Field(min_length=1, max_length=6)


class ProductSpec(DomainModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    bottle_shape: Concrete
    bottle_material: Concrete
    liquid_color: Concrete
    cap: Concrete
    label_text: Annotated[str, Field(max_length=80)]
    label_style: Concrete
    distinctive_features: list[Concrete] = Field(default_factory=list, max_length=6)
    hero_angle: Concrete


class ColorGrade(DomainModel):
    palette: list[HexColor] = Field(min_length=2, max_length=6)
    contrast: Concrete
    saturation: Concrete
    film_emulation: str | None = Field(default=None, max_length=120)


class LocationSpec(DomainModel):
    primary: Concrete
    secondary: list[Concrete] = Field(default_factory=list, max_length=4)
    era: Annotated[str, Field(max_length=60)] = "contemporary"


class ContinuityBibleDraft(DomainModel):
    """``continuity`` LLM output. Visual and concrete only, in English. ``character`` is null for
    product-only ads."""

    character: CharacterSpec | None
    product: ProductSpec
    color_grade: ColorGrade
    location: LocationSpec
    mood: Concrete
    lighting: Concrete = Field(description="Lighting baseline that scenes deviate from explicitly.")
    audio_mood: Concrete
    forbidden: list[Concrete] = Field(default_factory=list, max_length=12)


class ContinuityBible(ContinuityBibleDraft):
    version: int = Field(default=1, ge=1)

    def section_hash(self, section: BibleSection) -> str:
        return stable_hash(
            "bible.section.v1", self.model_dump(mode="json", include={section.value})
        )

    def changed_sections(self, other: ContinuityBible) -> frozenset[BibleSection]:
        """Sections that differ between two bibles (drives the blast radius of bible edits)."""
        return frozenset(s for s in BibleSection if self.section_hash(s) != other.section_hash(s))

    def prompt_block(self, sections: frozenset[BibleSection] | None = None) -> str:
        """Canonical English text injected into prompts. Only sections the scene uses are
        included, so a product-only insert is not polluted by character details."""
        use = sections if sections is not None else frozenset(BibleSection)
        lines: list[str] = []
        if BibleSection.CHARACTER in use and self.character is not None:
            c = self.character
            lines.append(
                f"CHARACTER: {c.description}; age {c.age_range}; {c.appearance}; hair {c.hair}; "
                f"{c.grooming}; build {c.build}; wardrobe: {', '.join(c.wardrobe)}."
            )
        if BibleSection.PRODUCT in use:
            p = self.product
            feats = f"; {', '.join(p.distinctive_features)}" if p.distinctive_features else ""
            lines.append(
                f"PRODUCT: {p.name}: {p.bottle_shape} {p.bottle_material} bottle, "
                f"{p.liquid_color} liquid, {p.cap} cap, label '{p.label_text}' ({p.label_style})"
                f"{feats}; hero angle {p.hero_angle}."
            )
        if BibleSection.COLOR_GRADE in use:
            g = self.color_grade
            film = f", {g.film_emulation}" if g.film_emulation else ""
            lines.append(
                f"COLOR GRADE: palette {' '.join(g.palette)}; {g.contrast} contrast; "
                f"{g.saturation} saturation{film}."
            )
        if BibleSection.LOCATION in use:
            loc = self.location
            sec = f" (also: {', '.join(loc.secondary)})" if loc.secondary else ""
            lines.append(f"LOCATION: {loc.primary}{sec}; era {loc.era}.")
        if BibleSection.LIGHTING in use:
            lines.append(f"LIGHTING BASELINE: {self.lighting}.")
        if BibleSection.MOOD in use:
            lines.append(f"MOOD: {self.mood}.")
        if self.forbidden:
            lines.append(f"AVOID: {'; '.join(self.forbidden)}.")
        return "\n".join(lines)

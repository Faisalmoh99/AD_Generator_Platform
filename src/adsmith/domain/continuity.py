"""The continuity bible: the single source of visual truth injected into every scene prompt."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field

from adsmith.domain.base import DomainModel, stable_hash
from adsmith.domain.enums import BibleSection

Concrete = Annotated[str, Field(min_length=1, max_length=500)]
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
    """Category-neutral on purpose: works for a bottle, a cup of coffee or a phone app.
    (The first version had bottle/cap/liquid fields; real runs for a café and a delivery app
    produced "bottle_shape: Not a bottle: a modern smartphone".)"""

    name: Annotated[str, Field(min_length=1, max_length=80)]
    form: Concrete = Field(description="Shape and form factor of what the camera sees.")
    materials: Concrete = Field(description="Materials and surface finish.")
    colors: Concrete
    branding_text: Annotated[str, Field(max_length=80)] = Field(
        description="Exact wording on the product, or empty for wordless branding."
    )
    branding_style: Concrete = Field(description="How the label/logo/screen branding looks.")
    distinctive_features: list[Concrete] = Field(default_factory=list, max_length=6)
    hero_angle: Concrete

    def checklist(self) -> list[str]:
        """Item-by-item identity checks for the QA critic, derived only from this spec. Any
        mismatch is a major product issue (ADR-0016). Wordless branding (no branding_text) means
        no lettering of any kind is allowed on the product."""
        items = [
            *(f"Shape: {p}" for p in atomic_parts(self.form)),
            *(f"Material: {p}" for p in atomic_parts(self.materials)),
            *(f"Colour: {p}" for p in atomic_parts(self.colors)),
            *(f"Detail: {f}" for f in self.distinctive_features),
        ]
        if self.branding_text:
            items.append(
                f"Branding: only the text '{self.branding_text}', styled as: "
                f"{self.branding_style}. Any other lettering, logo or engraving is a mismatch."
            )
        else:
            items.append(
                f"Branding: {self.branding_style}. The product is WORDLESS: any letters, words, "
                "logos, monograms, engraving or printed marks anywhere on it are a mismatch."
            )
        return items


# A blank label, plate or plaque on a wordless product is a surface the video model fills with
# invented lettering (hero round: "blank label plate" → "RLYCHADA" in the close-up).
_LABEL_SURFACE = re.compile(r"\b(label|plate|plaque|placard|tag|sticker)s?\b", re.IGNORECASE)
# "no label, logo or printed text" is the wordless rule itself, not a surface: drop negated clauses.
_NEGATED_CLAUSE = re.compile(r"\b(no|not|never|without|free of)\b[^.;:]*", re.IGNORECASE)


def wordless_label_problems(product: ProductSpec) -> list[str]:
    """Wordless products must not describe any label surface (checked by the continuity node,
    not the model, so bibles stored before this rule still load)."""
    if product.branding_text:
        return []
    fields = {
        "form": product.form,
        "materials": product.materials,
        "branding_style": product.branding_style,
        **{f"distinctive_features[{i}]": f for i, f in enumerate(product.distinctive_features)},
        "hero_angle": product.hero_angle,
    }
    return [
        f"product.{name} mentions a {m.group(1).lower()} on a wordless product; describe bare, "
        "unlabelled material instead (no label, plate, plaque or tag of any kind)"
        for name, text in fields.items()
        if (m := _LABEL_SURFACE.search(_NEGATED_CLAUSE.sub(" ", text)))
    ]


class ColorGrade(DomainModel):
    palette: list[HexColor] = Field(min_length=2, max_length=6)
    contrast: Concrete
    saturation: Concrete
    film_emulation: str | None = Field(default=None, max_length=200)


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
        included, so a product-only insert is not polluted by character details.

        Labelled ``key: value`` lines, not sentence templates: LLM-written values often already
        contain the noun ("brushed gold cap"), and wrapping them ("… cap cap") reads badly in
        every prompt that embeds the bible.
        """
        use = sections if sections is not None else frozenset(BibleSection)
        lines: list[str] = []
        if BibleSection.CHARACTER in use and self.character is not None:
            c = self.character
            lines.append(
                f"CHARACTER: {c.description}; age: {c.age_range}; appearance: {c.appearance}; "
                f"hair: {c.hair}; grooming: {c.grooming}; build: {c.build}; "
                f"wardrobe: {', '.join(c.wardrobe)}."
            )
        if BibleSection.PRODUCT in use:
            p = self.product
            brand = (
                f"'{p.branding_text}', {p.branding_style}" if p.branding_text else p.branding_style
            )
            feats = (
                f"; details: {', '.join(p.distinctive_features)}" if p.distinctive_features else ""
            )
            lines.append(
                f"PRODUCT: {p.name}; form: {p.form}; materials: {p.materials}; "
                f"colors: {p.colors}; branding: {brand}{feats}; hero angle: {p.hero_angle}."
            )
        if BibleSection.COLOR_GRADE in use:
            g = self.color_grade
            film = f"; film: {g.film_emulation}" if g.film_emulation else ""
            lines.append(
                f"COLOR GRADE: palette {' '.join(g.palette)}; contrast: {g.contrast}; "
                f"saturation: {g.saturation}{film}."
            )
        if BibleSection.LOCATION in use:
            loc = self.location
            sec = f"; also: {', '.join(loc.secondary)}" if loc.secondary else ""
            lines.append(f"LOCATION: {loc.primary}{sec}; era: {loc.era}.")
        if BibleSection.LIGHTING in use:
            lines.append(f"LIGHTING BASELINE: {self.lighting}.")
        if BibleSection.MOOD in use:
            lines.append(f"MOOD: {self.mood}.")
        if self.forbidden:
            lines.append(f"AVOID: {'; '.join(self.forbidden)}.")
        return "\n".join(lines)


_ATOM_SPLIT = re.compile(r"\s*(?:[,;]|\band\b|\bwith\b)\s*")


def atomic_parts(text: str) -> list[str]:
    """Split a composite bible field into atomic checks ("bottle with beveled base, chamfered
    shoulders and a heavy squared cap" → bottle / beveled base / chamfered shoulders / heavy
    squared cap). One composite item lets a critic tick it while missing one part (seen live:
    a round cap passed under a single "Form and shape" item)."""
    parts = [p.strip(" .") for p in _ATOM_SPLIT.split(text)]
    return [p for p in parts if len(p) > 2] or [text]

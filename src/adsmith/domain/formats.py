"""Ad formats (PLAN_V2 item 1, ADR-0018): each format's grammar is data (``config/formats.toml``).

One format per run, resolved by code from the brief (``resolve_format``), never chosen by the judge:
the request names one → that; a requested shot count too small for a montage → narrative (or a
single take for one shot); a physical product → montage; anything else → narrative.

``grammar_problems`` is the deterministic validator for a storyboard draft in a grammar with a
shot vocabulary (montage). Its messages feed the schema-repair loop, like the duration check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel
from adsmith.domain.enums import CameraMove, ScenePurpose, Speed

if TYPE_CHECKING:
    from adsmith.domain.scene import SceneDraft
    from adsmith.domain.vocabulary import ShotVocab


class AdFormat(StrEnum):
    MONTAGE = "montage"
    SINGLE_TAKE = "single_take"
    NARRATIVE = "narrative"


Framing = Literal["macro", "product", "event", "detail", "silhouette", "full"]


class FormatGrammar(DomainModel):
    label: str = Field(min_length=1, max_length=80)
    min_shots: int = Field(ge=1, le=12)
    max_shots: int = Field(ge=1, le=12)
    kept_s: tuple[float, float]
    hero_last: bool = True
    max_person_shots: int | None = Field(default=None, ge=0)
    person_framing: list[Framing] = Field(default_factory=list)
    person_position: Literal["any", "last_two"] = "any"
    min_product_share: float = Field(default=0.0, ge=0, le=1)
    allowed_camera: list[CameraMove] = Field(default_factory=list)  # empty = any
    speed_default: Speed = Speed.NORMAL
    max_settings: int | None = Field(default=None, ge=1)
    dialogue: bool = True
    # Montage v2 (after run 1 had no event at all): at least this many physical-event shots
    # (vocabulary `physical_event`), and the last shot is this vocabulary entry.
    min_event_shots: int = Field(default=0, ge=0, le=12)
    last_vocab: str | None = None
    identity: Literal["reference", "optional"] = "optional"
    look: str = Field(min_length=1, max_length=1000)
    events: str = Field(min_length=1, max_length=1000)
    risk_notes: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _ranges(self) -> Self:
        if self.min_shots > self.max_shots:
            raise ValueError("min_shots > max_shots")
        if not 0 < self.kept_s[0] <= self.kept_s[1]:
            raise ValueError("kept_s must be (min, max) with 0 < min <= max")
        return self

    @property
    def kept_min(self) -> float:
        return self.kept_s[0]

    @property
    def kept_max(self) -> float:
        return self.kept_s[1]


class FormatLibrary(DomainModel):
    default_physical: AdFormat
    default_other: AdFormat
    formats: dict[AdFormat, FormatGrammar]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        missing = set(AdFormat) - set(self.formats)
        if missing:
            raise ValueError(f"formats.toml is missing formats: {sorted(missing)}")
        return self

    def grammar(self, fmt: AdFormat) -> FormatGrammar:
        return self.formats[fmt]


def resolve_format(
    lib: FormatLibrary,
    *,
    requested: AdFormat | None,
    requested_shots: int | None,
    physical_product: bool,
) -> tuple[AdFormat, str]:
    """(format, why). Pure: the same brief always resolves to the same format."""
    if requested is not None:
        return requested, f"the request asks for a {requested.value} format"
    if requested_shots == 1:
        return AdFormat.SINGLE_TAKE, "the request asks for one continuous shot"
    montage = lib.grammar(AdFormat.MONTAGE)
    if requested_shots is not None and requested_shots < montage.min_shots:
        return (
            AdFormat.NARRATIVE,
            f"the request asks for {requested_shots} shots, fewer than a montage's "
            f"{montage.min_shots}",
        )
    if physical_product:
        return lib.default_physical, "physical product and no format in the request"
    return lib.default_other, "not a physical product and no format in the request"


def format_block(lib: FormatLibrary, fmt: AdFormat) -> str:
    """The format as prompt text (ideate, storyboard)."""
    g = lib.grammar(fmt)
    lines = [
        f"Format: {fmt.value} ({g.label})",
        f"Shots: {g.min_shots}-{g.max_shots}; each shot lasts {g.kept_min:g}-{g.kept_max:g} s "
        "in the finished ad",
        f"Look: {g.look}",
        f"Events: {g.events}",
    ]
    if g.max_person_shots is not None:
        where = " (placed last or second to last)" if g.person_position == "last_two" else ""
        framing = " or ".join(g.person_framing) or "any"
        lines.append(
            f"People: at most {g.max_person_shots} shot(s) with a person, framed as "
            f"{framing} only{where}; never a full figure"
        )
    if g.allowed_camera:
        lines.append("Camera moves allowed: " + ", ".join(c.value for c in g.allowed_camera))
    if g.max_settings is not None:
        lines.append(f"At most {g.max_settings} distinct setting families across the ad")
    if g.hero_last:
        lines.append("The final shot is the product hero (purpose 'product').")
    if g.last_vocab:
        lines.append(f"The final shot uses the vocabulary entry {g.last_vocab}.")
    if g.min_event_shots:
        lines.append(
            f"At least {g.min_event_shots} shots are physical events (entries marked EVENT in "
            "the vocabulary); prefer the ones marked PROVEN (they passed on real media)."
        )
    lines += [f"Risk: {r}" for r in g.risk_notes]
    return "\n".join(lines)


def grammar_problems(
    scenes: Sequence[SceneDraft],
    g: FormatGrammar,
    vocab: Mapping[str, ShotVocab] | None = None,
) -> list[str]:
    """Deterministic checks of a storyboard draft against a format grammar (→ schema repair)."""
    n = len(scenes)
    problems: list[str] = []
    if not g.min_shots <= n <= g.max_shots:
        problems.append(f"this format needs {g.min_shots}-{g.max_shots} shots, not {n}")
    off = [
        f"{i}: {s.duration_s:g}s"
        for i, s in enumerate(scenes, 1)
        if not g.kept_min - 1e-9 <= s.duration_s <= g.kept_max + 1e-9
    ]
    if off:
        problems.append(
            f"each shot lasts {g.kept_min:g}-{g.kept_max:g}s in this format: {', '.join(off)}; "
            "redistribute the time (add or remove shots)"
        )
    if g.hero_last and n and (scenes[-1].purpose is not ScenePurpose.PRODUCT):
        problems.append("the final shot must be the product hero (purpose 'product')")
    if g.allowed_camera:
        bad = [
            f"{i}: {s.shot.camera_movement.value}"
            for i, s in enumerate(scenes, 1)
            if s.shot.camera_movement not in g.allowed_camera
        ]
        if bad:
            allowed = ", ".join(c.value for c in g.allowed_camera)
            problems.append(
                f"camera moves not allowed in this format ({', '.join(bad)}); use {allowed}"
            )
    people = [i for i, s in enumerate(scenes, 1) if s.shows_character]
    if g.max_person_shots is not None and len(people) > g.max_person_shots:
        problems.append(
            f"{len(people)} shots show a person ({people}); this format allows at most "
            f"{g.max_person_shots}: make the others product or physical-event shots"
        )
    if g.person_position == "last_two" and any(i < n - 1 for i in people):
        problems.append(f"a person shot must be last or second to last, not shot {people[0]}")
    if n and g.min_product_share:
        share = sum(1 for s in scenes if not s.shows_character) / n
        if share + 1e-9 < g.min_product_share:
            problems.append(
                f"only {share:.0%} of shots are product or physics shots; need at least "
                f"{g.min_product_share:.0%}"
            )
    if vocab is not None:
        problems += _vocab_problems(scenes, g, vocab)
    return problems


def _vocab_problems(
    scenes: Sequence[SceneDraft], g: FormatGrammar, vocab: Mapping[str, ShotVocab]
) -> list[str]:
    problems: list[str] = []
    names = ", ".join(sorted(vocab))
    for i, s in enumerate(scenes, 1):
        if s.vocab is None:
            problems.append(f"scene {i} has no vocab: pick one entry from the shot vocabulary")
            continue
        entry = vocab.get(s.vocab)
        if entry is None:
            problems.append(f"scene {i} vocab {s.vocab!r} is unknown; use one of: {names}")
            continue
        if entry.shows_person and g.person_framing and entry.framing not in g.person_framing:
            problems.append(f"scene {i} vocab {s.vocab!r} frames a person as {entry.framing}")
    last = vocab.get(scenes[-1].vocab or "") if scenes else None
    if g.hero_last and last is not None and not last.needs_product:
        problems.append("the final shot must use a vocabulary entry that shows the product")
    if g.last_vocab and scenes and scenes[-1].vocab != g.last_vocab:
        problems.append(f"the final shot must use the vocabulary entry {g.last_vocab}")
    events = [s.vocab for s in scenes if (e := vocab.get(s.vocab or "")) and e.physical_event]
    if len(events) < g.min_event_shots:
        names = ", ".join(n for n, e in vocab.items() if e.physical_event)
        problems.append(
            f"only {len(events)} physical-event shot(s); this format needs at least "
            f"{g.min_event_shots}: use entries from {names} (prefer PROVEN ones)"
        )
    return problems

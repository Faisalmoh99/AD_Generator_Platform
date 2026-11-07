"""Creative rules for the whole-ad prompt (ADR-0026), data in ``config/creative_rules.toml``.

Two kinds of rule:
- **checked by code** (``params``, ``banned``): every ad prompt the writer or the director
  produces goes through :func:`creative_rule_problems`; a failure goes back to the model through
  the schema-repair loop, with the replacement named, so it is fixed rather than refused.
- **judged** (``judge``): rules that need judgement (is the turn withheld?). The creative
  director rules on each one by id; code checks it answered every id.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.ad import AdPromptDraft
from adsmith.domain.base import DomainModel

HERO_SIZES = {"half", "fills"}


class CreativeParams(DomainModel):
    max_shots: int = Field(ge=1, le=12)
    min_shot_s: float = Field(gt=0, le=10)
    key_min_s: float = Field(gt=0, le=10)
    hero_min_s: float = Field(gt=0, le=10)
    product_min_shots: int = Field(ge=0, le=12)
    product_self_min_shots: int = Field(ge=0, le=12)
    product_min_share: float = Field(ge=0, le=1)
    handled_min_shots: int = Field(ge=0, le=12)
    max_locations: int = Field(ge=1, le=4)
    max_references: int = Field(ge=0, le=8)


class BannedWord(DomainModel):
    word: Annotated[str, Field(min_length=1, max_length=40)]
    pattern: str | None = None
    why: Annotated[str, Field(min_length=1, max_length=200)]
    use: Annotated[str, Field(min_length=1, max_length=200)]
    fields: Literal["all", "camera"] = "all"
    except_hero: bool = False

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        re.compile(self.regex, re.IGNORECASE)
        return self

    @property
    def regex(self) -> str:
        return self.pattern or rf"\b{re.escape(self.word)}\b"

    def hits(self, text: str) -> bool:
        return re.search(self.regex, text, re.IGNORECASE) is not None


class JudgeRule(DomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    text: Annotated[str, Field(min_length=1, max_length=400)]


class CreativeRules(DomainModel):
    params: CreativeParams
    banned: list[BannedWord] = Field(default_factory=list)
    judge: list[JudgeRule] = Field(min_length=1)
    lessons: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [r.id for r in self.judge]
        if len(ids) != len(set(ids)):
            raise ValueError("judge rule ids must be unique")
        return self

    @property
    def judge_ids(self) -> list[str]:
        return [r.id for r in self.judge]

    def params_block(self) -> str:
        p = self.params
        return "\n".join(
            [
                f"- At most {p.max_shots} shots; every shot lasts at least {p.min_shot_s:g} s.",
                f"- The key shot (where the turn happens) lasts at least {p.key_min_s:g} s; the "
                f"last shot is the product hero, at least {p.hero_min_s:g} s, the product filling "
                "at least half the frame.",
                f"- The product, or a prop that transforms into it, is visible in at least "
                f"{p.product_min_shots} shots and {p.product_min_share:.0%} of the runtime; the "
                f"product itself in at least {p.product_self_min_shots} shots.",
                f"- A person's hands hold or use the product in at least {p.handled_min_shots} "
                "shot(s). What is withheld until the key shot is what the product DOES, never the "
                "product.",
                f"- {p.max_locations} location for the whole film; at most {p.max_references} "
                "references besides the product photo.",
            ]
        )

    def banned_block(self) -> str:
        lines = []
        for b in self.banned:
            where = " (in the camera text" + (", except the last shot)" if b.except_hero else ")")
            lines.append(
                f'- "{b.word}"{where if b.fields == "camera" else ""}: {b.why}. Write: {b.use}'
            )
        return "\n".join(lines)

    def judge_block(self) -> str:
        return "\n".join(f"- {r.id}: {r.text}" for r in self.judge)

    def lessons_block(self) -> str:
        return "\n".join(f"- {k}: {v}" for k, v in self.lessons.items())

    def prompt_block(self) -> str:
        """The rules as prompt text (writer and director)."""
        return (
            "Checked by code (a prompt that breaks one is sent back):\n"
            f"{self.params_block()}\n"
            "Words the video model takes literally or ignores (never use them):\n"
            f"{self.banned_block()}\n"
            "Judged by the creative director:\n"
            f"{self.judge_block()}"
        )


def _texts(d: AdPromptDraft) -> list[tuple[str, str, bool]]:
    """(where, text, is_hero_shot_camera) for every writable text of a prompt."""
    out: list[tuple[str, str, bool]] = [
        ("location", d.location, False),
        ("look", d.look, False),
        ("lighting", d.lighting, False),
        ("audio", d.audio, False),
    ]
    out += [(f"reference '{r.name}'", r.description, False) for r in d.references]
    last = len(d.shots)
    for s in d.shots:
        for field in ("action", "event", "sound"):
            out.append((f"shot {s.number} {field}", getattr(s, field), False))
        out.append((f"shot {s.number} camera", s.camera, s.number == last))
    return out


def banned_problems(d: AdPromptDraft, banned: Sequence[BannedWord]) -> list[str]:
    problems: list[str] = []
    for b in banned:
        for where, text, hero_camera in _texts(d):
            if b.fields == "camera" and not where.endswith("camera"):
                continue
            if b.except_hero and hero_camera:
                continue
            if b.hits(text):
                problems.append(f'{where} uses "{b.word}" ({b.why}); write instead: {b.use}')
    return problems


# One source of size (2026-11-07): the Framing line rendered by code from product_in_frame is the
# only place the product's size is stated; run_01M6SSVMPBEAY1WW1FWF4DGWRZ said "a third" in the
# action and "half" in the framing.
_SIZE_IN_TEXT = re.compile(
    r"\b(fills?|filling|half|third|quarter|two[- ]thirds|small|tiny|large|big)\b[^.;]{0,40}"
    r"\bframe\b|\bframe\b[^.;]{0,15}\b(half|third|quarter)\b",
    re.IGNORECASE,
)
_PERIPHERAL = re.compile(r"\b(edge|corner|soft focus|out of focus|blurred)\b", re.IGNORECASE)
_TEMPLATE_RESIDUE = re.compile(r"[{}<>]")
_PAIRS = (("(", ")"), ("[", "]"))


def size_problems(d: AdPromptDraft) -> list[str]:
    problems: list[str] = []
    for s in d.shots:
        for field in ("action", "event"):
            m = _SIZE_IN_TEXT.search(getattr(s, field))
            if m:
                problems.append(
                    f'shot {s.number} {field} states a size ("{m.group(0)}"); remove it: the '
                    "Framing line, written by code from product_in_frame, is the only place for "
                    "the product's size"
                )
        if s.shows_product and s.product_in_frame in HERO_SIZES:
            m = _PERIPHERAL.search(s.action)
            if m:
                problems.append(
                    f'shot {s.number} puts the product "{m.group(0)}" but product_in_frame is '
                    f"'{s.product_in_frame}': a product at the edge or out of focus cannot fill "
                    "half the frame; choose one (product_in_frame 'small', or bring it forward)"
                )
    return problems


def residue_problems(d: AdPromptDraft) -> list[str]:
    """Stray characters (template residue, unbalanced brackets) before anything is rendered:
    the writer of run_01M6SSVMPBEAY1WW1FWF4DGWRZ left a '}' in the audio line."""
    problems: list[str] = []
    for where, text, _ in _texts(d):
        if m := _TEMPLATE_RESIDUE.search(text):
            problems.append(f'{where} contains a stray "{m.group(0)}": remove it')
        for a, b in _PAIRS:
            if text.count(a) != text.count(b):
                problems.append(f'{where} has unbalanced "{a}{b}": close or remove it')
    return problems


def product_share(d: AdPromptDraft) -> tuple[list[int], float]:
    """Shots (and the share of the runtime) where the product, or a prop that transforms into
    it, is on screen (the user's rule, 2026-11-07: a transforming prop counts)."""
    stand_in = {n for r in d.references if r.transforms_into_product for n in r.shots}
    shots = [s for s in d.shots if s.shows_product or s.number in stand_in]
    total = d.shots[-1].end_s - d.shots[0].start_s if d.shots else 0.0
    seconds = sum(s.end_s - s.start_s for s in shots)
    return [s.number for s in shots], (seconds / total if total > 0 else 0.0)


def creative_rule_problems(  # noqa: PLR0912 - one flat list of independent rules
    d: AdPromptDraft, rules: CreativeRules
) -> list[str]:
    """Every rule code can check, as repair messages."""
    p = rules.params
    problems: list[str] = []
    n = len(d.shots)
    if n > p.max_shots:
        problems.append(f"at most {p.max_shots} shots, not {n}")
    lengths = {s.number: s.end_s - s.start_s for s in d.shots}
    short = [f"{k} ({v:g} s)" for k, v in lengths.items() if v + 1e-9 < p.min_shot_s]
    if short:
        problems.append(
            f"every shot lasts at least {p.min_shot_s:g} s: {', '.join(short)} (the model cuts "
            "shorter shots further)"
        )
    if d.key_shot in lengths and lengths[d.key_shot] + 1e-9 < p.key_min_s:
        problems.append(
            f"the key shot {d.key_shot} lasts {lengths[d.key_shot]:g} s; give the turn at least "
            f"{p.key_min_s:g} s"
        )
    if d.shots:
        hero = d.shots[-1]
        if not hero.shows_product or hero.product_in_frame not in HERO_SIZES:
            problems.append(
                f"the last shot {hero.number} is the product hero: shows_product true and "
                "product_in_frame 'half' or 'fills'"
            )
        if lengths[hero.number] + 1e-9 < p.hero_min_s:
            problems.append(
                f"the hero shot lasts {lengths[hero.number]:g} s; it needs at least "
                f"{p.hero_min_s:g} s"
            )
    for s in d.shots:
        if s.shows_product == (s.product_in_frame == "absent"):
            problems.append(
                f"shot {s.number}: product_in_frame must be 'absent' exactly when shows_product "
                "is false"
            )
        if s.handled and not s.shows_product:
            problems.append(f"shot {s.number} is handled but the product is not visible")
        if re.search(r"\bimage\s*\d", f"{s.action} {s.event} {s.camera}", re.IGNORECASE):
            problems.append(
                f"shot {s.number} names an image number; name the object (the References line "
                "is written by code)"
            )
    seen, share = product_share(d)
    need = min(p.product_min_shots, n)
    if len(seen) < need or share + 1e-9 < p.product_min_share:
        problems.append(
            f"the product (or a prop that transforms into it) is on screen in shots {seen} = "
            f"{share:.0%} of the runtime; it needs at least {need} shots and "
            f"{p.product_min_share:.0%}: show the product earlier, held or in the background"
        )
    own = sum(1 for s in d.shots if s.shows_product)
    if own < min(p.product_self_min_shots, n):
        problems.append(
            f"the product itself is visible in {own} shot(s); it needs at least "
            f"{p.product_self_min_shots}"
        )
    handled = sum(1 for s in d.shots if s.handled)
    if handled < p.handled_min_shots:
        problems.append(
            f"a person's hands hold or use the product in {handled} shot(s); at least "
            f"{p.handled_min_shots} (lift it, open it, spray it, pass it)"
        )
    if len(d.references) > p.max_references:
        problems.append(f"at most {p.max_references} references, not {len(d.references)}")
    names = [r.name.lower() for r in d.references]
    if len(names) != len(set(names)):
        problems.append("reference names must be unique")
    locations = [r for r in d.references if r.role == "location"]
    if len(locations) > p.max_locations:
        problems.append(f"at most {p.max_locations} location reference(s)")
    for r in d.references:
        bad = [k for k in r.shots if k > n]
        if bad:
            problems.append(f"reference '{r.name}' lists shots {bad}; there are {n} shots")
        if r.transforms_into_product and r.role != "prop":
            problems.append(f"reference '{r.name}': only a prop can transform into the product")
    problems += banned_problems(d, rules.banned)
    problems += size_problems(d)
    problems += residue_problems(d)
    return problems

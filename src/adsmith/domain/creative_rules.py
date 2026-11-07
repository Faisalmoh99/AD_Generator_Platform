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

from adsmith.domain.ad import AdPromptDraft, writer_words
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
    # Word budget (ADR-0028). Defaults keep an older rules file loadable.
    max_words: int = Field(default=180, ge=40, le=600)
    key_within_words: int = Field(default=30, ge=10, le=80)
    max_words_look: int = Field(default=35, ge=5, le=200)
    max_words_shot: int = Field(default=45, ge=10, le=200)
    max_words_audio: int = Field(default=25, ge=5, le=200)
    # The key shot's beats (ADR-0029: the coffee video's key shot had four in 2.5 s).
    key_max_beats: int = Field(default=2, ge=1, le=6)
    # A word-budget overage up to this share is a logged warning, not a failure (user,
    # 2026-11-07: the perfume writer failed its last repair 38 words against 35).
    word_tolerance: float = Field(default=0.10, ge=0, le=0.5)


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


class PhysicsRule(DomainModel):
    """A Seedance failure and the wording that prevents it (ADR-0028), checked by code."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    failure: Annotated[str, Field(min_length=1, max_length=200)]
    seen: Annotated[str, Field(min_length=1, max_length=200)]
    trigger: str
    required: str
    use: Annotated[str, Field(min_length=1, max_length=300)]
    key_only: bool = False

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        re.compile(self.trigger, re.IGNORECASE)
        re.compile(self.required, re.IGNORECASE)
        return self


class ServingRule(DomainModel):
    """How Saudi qahwa is served (checked by code): pouring into a finjan names the dallah."""

    cup: str
    pour: str
    vessel: str
    wrong: str
    why: Annotated[str, Field(min_length=1, max_length=300)]
    use: Annotated[str, Field(min_length=1, max_length=300)]

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        for x in (self.cup, self.pour, self.vessel, self.wrong):
            re.compile(x, re.IGNORECASE)
        return self


class SupportRule(DomainModel):
    """What carries an unheld product (checked by code, 2026-11-07): the perfume run's shot 2
    started "from the open bottle" with no hand and no surface, and Seedance hung the bottle in
    mid-air beside the mother."""

    surface: str  # regex: a named support (table, tray, sand, a palm, ...)
    why: Annotated[str, Field(min_length=1, max_length=300)]
    use: Annotated[str, Field(min_length=1, max_length=300)]

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        re.compile(self.surface, re.IGNORECASE)
        return self

    def supported(self, text: str) -> bool:
        return re.search(self.surface, text, re.IGNORECASE) is not None


class ReferenceTemplate(DomainModel):
    """A reference ad's measured execution (ADR-0028): targets, not a format to copy."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    source: Annotated[str, Field(min_length=1, max_length=200)]
    numbers: Annotated[str, Field(min_length=1, max_length=400)]
    borrow: Annotated[str, Field(min_length=1, max_length=300)]


class JudgeRule(DomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    text: Annotated[str, Field(min_length=1, max_length=600)]


class CreativeRules(DomainModel):
    params: CreativeParams
    banned: list[BannedWord] = Field(default_factory=list)
    physics: list[PhysicsRule] = Field(default_factory=list)
    templates: list[ReferenceTemplate] = Field(default_factory=list)
    serving: ServingRule | None = None
    support: SupportRule | None = None
    camera_moves: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list
    )
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
                f"- Your words (look, every shot's action, camera and sound, audio) are at most "
                f"{p.max_words}; the look at most {p.max_words_look}, each shot at most "
                f"{p.max_words_shot}, the audio at most {p.max_words_audio}. Code writes the "
                "product's identity and the "
                "key shot's event as the first two lines: they must end within "
                f"{p.key_within_words} words, so the key shot's event is the one visible motion "
                "in a few words.",
                f"- The key shot's action has at most {p.key_max_beats} beats (clauses split by "
                "';' or a full stop): the one event that is the turn, and the reaction to it.",
            ]
        )

    def physics_block(self) -> str:
        return "\n".join(f"- {r.id}: {r.failure} ({r.seen}). Write: {r.use}" for r in self.physics)

    def templates_block(self) -> str:
        return "\n".join(f"- {t.source}: {t.numbers}. Borrow: {t.borrow}" for t in self.templates)

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
            "Seedance failures we saw and the wording that prevents them (checked by code: a shot "
            "whose motion matches one must use its wording):\n"
            f"{self.physics_block()}\n"
            "Camera moves that read in one shot (one per shot): "
            f"{', '.join(self.camera_moves)}\n"
            + (
                f"Serving Saudi qahwa (checked by code): {self.serving.why}. "
                f"Write: {self.serving.use}\n"
                if self.serving
                else ""
            )
            + "Execution targets from reference ads we measured:\n"
            f"{self.templates_block()}\n"
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


def physics_problems(d: AdPromptDraft, physics: Sequence[PhysicsRule]) -> list[str]:
    """The Seedance failure library (ADR-0028): a shot whose motion triggers a rule must use its
    wording in the same shot (action or event)."""
    problems: list[str] = []
    for s in d.shots:
        text = f"{s.action} {s.event}"
        for r in physics:
            if r.key_only and s.number != d.key_shot:
                continue
            if re.search(r.trigger, text, re.IGNORECASE) and not re.search(
                r.required, text, re.IGNORECASE
            ):
                problems.append(f"shot {s.number} ({r.id}): {r.failure} ({r.seen}). {r.use}")
    return problems


def serving_problems(d: AdPromptDraft, rule: ServingRule | None) -> list[str]:
    """Saudi qahwa (user rule, 2026-11-07): a shot that pours or serves into a finjan names the
    dallah and no other vessel; a prompt with a finjan has no brewer, kettle or teapot prop."""
    if rule is None:
        return []

    def has(pattern: str, text: str) -> re.Match[str] | None:
        return re.search(pattern, text, re.IGNORECASE)

    problems: list[str] = []
    any_cup = False
    for s in d.shots:
        text = f"{s.action} {s.event}"
        if not has(rule.cup, text):
            continue
        any_cup = True
        if not has(rule.pour, text):
            continue
        if bad := has(rule.wrong, text):
            problems.append(
                f'shot {s.number} serves into a finjan from a "{bad.group(0)}": {rule.why}. '
                f"Write: {rule.use}"
            )
        elif not has(rule.vessel, text):
            problems.append(
                f"shot {s.number} pours into a finjan without naming the dallah: {rule.why}. "
                f"Write: {rule.use}"
            )
    if any_cup or any(has(rule.cup, f"{r.name} {r.description}") for r in d.references):
        for r in d.references:
            if bad := has(rule.wrong, r.name):
                problems.append(
                    f'reference "{r.name}" is a "{bad.group(0)}" next to a finjan: {rule.why}. '
                    "Use the dallah"
                )
    return problems


def over_budget(n: int, limit: int, tolerance: float) -> bool:
    """A failure only above the tolerance (10 %: 38 words against 35 is a warning)."""
    return n > limit * (1 + tolerance) + 1e-9


def _field_counts(d: AdPromptDraft, p: CreativeParams) -> list[tuple[str, int, int]]:
    out = [("location + look + lighting", len(f"{d.location} {d.look} {d.lighting}".split()),
            p.max_words_look)]  # fmt: skip
    out += [
        (f"shot {s.number}", len(f"{s.action} {s.camera} {s.sound}".split()), p.max_words_shot)
        for s in d.shots
    ]
    out.append(("the audio line", len(d.audio.split()), p.max_words_audio))
    return out


def word_budget_problems(d: AdPromptDraft, p: CreativeParams) -> list[str]:
    """Per-field word budgets (ADR-0028); the whole-prompt budget is checked on the rendered
    text with its References line (``domain.ad.word_problems``). An overage within
    ``word_tolerance`` is a warning (``word_budget_warnings``), not a failure."""
    problems: list[str] = []
    for where, n, limit in _field_counts(d, p):
        if not over_budget(n, limit, p.word_tolerance):
            continue
        if where.startswith("location"):
            problems.append(
                f"location + look + lighting take {n} words; at most {limit}: one short "
                "sentence each, no props (the shots name them)"
            )
        elif where.startswith("shot"):
            problems.append(
                f"{where} takes {n} words (action + camera + sound); at most {limit}: one beat, "
                "one move, one sound"
            )
        else:
            problems.append(f"the audio line takes {n} words; at most {limit}")
    return problems


def word_budget_warnings(d: AdPromptDraft, p: CreativeParams) -> list[str]:
    """Overages within the tolerance (per field and the writer's total): logged, never sent
    back to the model."""
    counts = [*_field_counts(d, p), ("the writer's words", writer_words(d), p.max_words)]
    return [
        f"{where} {n} words (budget {limit}, within {p.word_tolerance:.0%})"
        for where, n, limit in counts
        if n > limit and not over_budget(n, limit, p.word_tolerance)
    ]


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


def unsupported_shots(d: AdPromptDraft, rule: SupportRule | None) -> list[int]:
    """Shots that show the product with no hand on it and no named surface under it."""
    if rule is None:
        return []
    return [
        s.number
        for s in d.shots
        if s.shows_product and not s.handled and not rule.supported(f"{s.action} {s.event}")
    ]


def product_share(d: AdPromptDraft, support: SupportRule | None = None) -> tuple[list[int], float]:
    """Shots (and the share of the runtime) where the product, or a prop that transforms into
    it, is on screen (the user's rule, 2026-11-07: a transforming prop counts). With a
    ``support`` rule, an unheld product with no named surface does not count (it floats)."""
    stand_in = {n for r in d.references if r.transforms_into_product for n in r.shots}
    floating = set(unsupported_shots(d, support))
    shots = [
        s for s in d.shots if (s.shows_product and s.number not in floating) or s.number in stand_in
    ]
    total = d.shots[-1].end_s - d.shots[0].start_s if d.shots else 0.0
    seconds = sum(s.end_s - s.start_s for s in shots)
    return [s.number for s in shots], (seconds / total if total > 0 else 0.0)


_BEAT_SPLIT = re.compile(r";|\.\s+(?=[A-Z])")


def key_beat_problems(d: AdPromptDraft, max_beats: int) -> list[str]:
    """The key shot carries at most ``max_beats`` beats (clauses split by ';' or a full stop):
    the coffee video's key shot had four in 2.5 s (tilt; fill; offer; smile) and the turn
    landed softly. Steps of ONE event ("spirals tighter, thickens, then condenses") are fine."""
    shot = next((s for s in d.shots if s.number == d.key_shot), None)
    if shot is None:
        return []
    beats = [b for b in _BEAT_SPLIT.split(shot.action.strip().rstrip(".")) if b.strip()]
    if len(beats) <= max_beats:
        return []
    return [
        f"the key shot {shot.number} has {len(beats)} beats ({'; '.join(b.strip() for b in beats)}"
        f"); at most {max_beats}: keep the one event that IS the turn and the reaction to it, "
        "move the rest to another shot or cut it"
    ]


def device_length_problems(d: AdPromptDraft, device: str, min_s: float) -> list[str]:
    """The concept's visual device happens in the key shot, which needs the device's minimum
    length (ADR-0029: try 2's 2.5 s morph was compressed to 0.5 s)."""
    shot = next((s for s in d.shots if s.number == d.key_shot), None)
    if shot is None or shot.end_s - shot.start_s + 1e-9 >= min_s:
        return []
    return [
        f"the key shot {shot.number} carries the visual device '{device}', which needs at least "
        f"{min_s:g} s; it lasts {shot.end_s - shot.start_s:g} s (take time from another shot)"
    ]


def creative_rule_problems(  # noqa: PLR0912, PLR0915 - one flat list of independent rules
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
    seen, share = product_share(d, rules.support)
    if rules.support is not None:
        for n_ in unsupported_shots(d, rules.support):
            problems.append(
                f"shot {n_} shows the product with no hand on it and no surface under it "
                f"({rules.support.why}); {rules.support.use}"
            )
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
    problems += physics_problems(d, rules.physics)
    problems += serving_problems(d, rules.serving)
    problems += word_budget_problems(d, p)
    problems += key_beat_problems(d, p.key_max_beats)
    for s in d.shots:
        if s.handled and s.product_in_frame == "small":
            problems.append(
                f"shot {s.number}: a hand uses the product but it is small in frame; a product "
                "that acts is at least half the frame (try 2: the spray was invisible because the "
                "bottle was tiny)"
            )
    return problems

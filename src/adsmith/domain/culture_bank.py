"""The Saudi culture bank (ADR-0026, phase 4), data in ``config/culture_bank.toml``.

Rituals, proverbs, occasions, gestures, objects and phrases with their meaning, how an ad can
stand on them, the product categories they fit and their pitfalls. The insight stage receives the
entries that fit the brief's category and names, for each insight, the entry it stands on
(``culture_anchor``), so culture carries meaning instead of decorating the frame.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.ad import AdPromptDraft, ref_key
from adsmith.domain.base import DomainModel
from adsmith.domain.brief import Brief

NO_ANCHOR = "none"
# An Arabic word may carry a proclitic (و، ف، ب، ل، ك and the article ال), so "لعطر" and "والقهوة"
# match; the alias must start a word, so "عود" (oud) never matches inside "سعودية" (Saudi).
_ARABIC_PREFIX = "(?:^|(?<=[^\u0621-\u064a]))(?:[وفبلك]?(?:ال|لل)?)"


def _arabic_word(w: str) -> str:
    return _ARABIC_PREFIX + re.escape(w)


Kind = Literal["proverb", "ritual", "occasion", "gesture", "object", "phrase"]
Text = Annotated[str, Field(min_length=1, max_length=400)]


class GulfObject(DomainModel):
    """The canonical Gulf version of a physical object a prop may be (a dallah, a finjan)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    names: list[Annotated[str, Field(min_length=2, max_length=40)]] = Field(min_length=1)
    description: Annotated[str, Field(min_length=1, max_length=200)]
    # 2-4 words for a compact References line, when the image carries the look.
    gloss: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    not_like: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list)
    # A verified reference photo of the Gulf version (path relative to the project root): used
    # at $0 instead of a generated prop image (user, 2026-11-07).
    ref_image: str | None = Field(default=None, pattern=r"\.(png|jpe?g|webp)$")
    # Object fidelity (ADR-0029, the coffee video: "handleless" said once, the shots said "cups",
    # the finjans got handles; the dallah image came back as set dressing too).
    # always_say: the defining word code glues to every mention in the shots ("handleless").
    always_say: Annotated[str, Field(min_length=2, max_length=30)] | None = None
    # wrong_words: regexes for the generic word a model would draw instead ("cups?", "mugs?"),
    # refused in the shots when this object is in the prompt (its own names are not).
    wrong_words: list[Annotated[str, Field(min_length=2, max_length=60)]] = Field(
        default_factory=list, max_length=8
    )
    # max_count 1: exactly one in the film, said in the References line.
    max_count: int | None = Field(default=None, ge=1, le=6)

    def name_regex(self) -> str:
        """The object's English names (longest first, plural allowed) as one regex."""
        names = sorted((n for n in self.names if n.isascii()), key=len, reverse=True)
        return r"\b(" + "|".join(re.escape(n) for n in names) + r")(s|es)?\b"

    def matches(self, prop_name: str) -> bool:
        """A prop named 'brass dallah' or 'the coffee pot' is this object (whole-word match)."""
        key = " ".join(prop_name.lower().replace("_", " ").replace("-", " ").split())
        for n in self.names:
            w = n.lower()
            if w.isascii() and re.search(rf"\b{re.escape(w)}\b", key):
                return True
            if not w.isascii() and re.search(_arabic_word(w), key):
                return True
        return False


Theme = Literal["hospitality", "family", "self", "occasion", "travel", "craft", "season"]


class CultureEntry(DomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    theme: Theme
    kind: Kind
    text_ar: Annotated[str, Field(min_length=1, max_length=120)]
    meaning_en: Text
    use: Text
    categories: list[str] = Field(min_length=1, max_length=8)
    objects: list[str] = Field(default_factory=list, max_length=4)  # GulfObject ids
    pitfalls: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list)


class CultureBank(DomainModel):
    aliases: dict[str, list[str]]
    general_pitfalls: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list)
    objects: list[GulfObject] = Field(default_factory=list)
    entries: list[CultureEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [e.id for e in self.entries]
        if len(ids) != len(set(ids)) or NO_ANCHOR in ids:
            raise ValueError("culture bank ids must be unique and must not be 'none'")
        known = set(self.aliases) | {"any"}
        bad = {c for e in self.entries for c in e.categories} - known
        if bad:
            raise ValueError(f"unknown categories (add them to [aliases]): {sorted(bad)}")
        known_objects = {o.id for o in self.objects}
        missing = {o for e in self.entries for o in e.objects} - known_objects
        if missing or len(known_objects) != len(self.objects):
            raise ValueError(f"object ids must be unique and defined: unknown {sorted(missing)}")
        return self

    def object_for(self, prop_name: str) -> GulfObject | None:
        """The canonical object a prop's name refers to; the most specific name wins (a 'coffee
        cup' is a finjan, not a dallah)."""
        best: tuple[int, GulfObject] | None = None
        for o in self.objects:
            for n in o.names:
                if o.model_copy(update={"names": [n]}).matches(prop_name) and (
                    best is None or len(n) > best[0]
                ):
                    best = (len(n), o)
        return best[1] if best else None

    def glosses(self, names: Sequence[str]) -> dict[str, str]:
        """Compact glosses for the props named (by ``ref_key``), from the Gulf objects."""
        out: dict[str, str] = {}
        for n in names:
            obj = self.object_for(n)
            if obj is not None and obj.gloss:
                out[ref_key(n)] = obj.gloss + (", only one" if obj.max_count == 1 else "")
        return out

    def objects_block(self) -> str:
        if not self.objects:
            return "(none)"
        return "\n".join(
            f"- {o.id} ({', '.join(o.names[:3])}): {o.description}"
            + (f". Never: {'; '.join(o.not_like)}" if o.not_like else "")
            for o in self.objects
        )

    @property
    def ids(self) -> list[str]:
        return [e.id for e in self.entries]

    def get(self, entry_id: str) -> CultureEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def categories_for(self, brief: Brief) -> list[str]:
        """The bank's categories the brief belongs to (English category and product, and the raw
        request, which may be Arabic)."""
        text = f"{brief.product_category} {brief.product} {brief.raw_request}".lower()
        found = []
        for cat, words in self.aliases.items():
            for word in words:
                w = word.lower()
                hit = re.search(rf"\b{re.escape(w)}" if w.isascii() else _arabic_word(w), text)
                if hit:
                    found.append(cat)
                    break
        return found

    def for_brief(self, brief: Brief, limit: int = 12, per_theme: int = 3) -> list[CultureEntry]:
        """Entries that fit the brief: its own categories first, then the ones for any product;
        balanced across themes (ADR-0028): taken round-robin by theme, at most ``per_theme`` of
        one theme while other themes still have entries."""
        cats = set(self.categories_for(brief))
        own = [e for e in self.entries if cats & set(e.categories)]
        general = [e for e in self.entries if "any" in e.categories and e not in own]
        out = _round_robin(own, limit, per_theme)
        if len(out) < limit:
            out += _round_robin(general, limit - len(out), per_theme)
        return out

    def prompt_block(self, entries: list[CultureEntry]) -> str:
        general = (
            "Never, in any ad: " + "; ".join(self.general_pitfalls) + "."
            if self.general_pitfalls
            else ""
        )
        if not entries:
            return "(no entry fits this product: anchor every insight to 'none')" + (
                f"\n{general}" if general else ""
            )
        lines = [general] if general else []
        for e in entries:
            pit = f" Never: {'; '.join(e.pitfalls)}." if e.pitfalls else ""
            lines.append(
                f"- {e.id} ({e.kind}) «{e.text_ar}»: {e.meaning_en} An ad can use it as: "
                f"{e.use}.{pit}"
            )
        return "\n".join(lines)


def _round_robin(entries: list[CultureEntry], limit: int, per_theme: int) -> list[CultureEntry]:
    """Up to ``limit`` entries, one theme at a time in order of first appearance; a theme beyond
    ``per_theme`` entries only once every other theme is exhausted."""
    by_theme: dict[str, list[CultureEntry]] = {}
    for e in entries:
        by_theme.setdefault(e.theme, []).append(e)
    out: list[CultureEntry] = []
    for cap in (per_theme, len(entries)):
        rnd = 0
        while len(out) < limit and any(len(v) > rnd for v in by_theme.values()) and rnd < cap:
            for v in by_theme.values():
                if rnd < len(v) and v[rnd] not in out and len(out) < limit:
                    out.append(v[rnd])
            rnd += 1
    return out


_ONLY_ONE = "; exactly one in the whole film"


def _glue(text: str, obj: GulfObject) -> str:
    """Put the object's defining word before every mention that lacks it nearby
    ("two finjans" → "two handleless finjans")."""
    word = obj.always_say
    if not word:
        return text

    def add(m: re.Match[str]) -> str:
        before = text[max(0, m.start() - 30) : m.start()].lower()
        return m.group(0) if word.lower() in before else f"{word} {m.group(0)}"

    return re.sub(obj.name_regex(), add, text, flags=re.IGNORECASE)


def objects_in(d: AdPromptDraft, bank: CultureBank) -> list[GulfObject]:
    """The Gulf objects the prompt names, as a reference or in any shot."""
    text = " ".join(f"{s.action} {s.event}" for s in d.shots)
    named = {o.id for r in d.references if (o := bank.object_for(r.name)) is not None}
    return [
        o
        for o in bank.objects
        if o.id in named or re.search(o.name_regex(), text, re.IGNORECASE) is not None
    ]


def with_gulf_objects(d: AdPromptDraft, bank: CultureBank) -> AdPromptDraft:
    """A prop that names a Gulf object (a dallah, a finjan) gets the bank's canonical description,
    so the prompt, the prop image and its check all describe the Gulf version, never a generic
    one; an object that must be one of a kind says so; its defining word is glued to every
    mention in the shots. Code, not the writer, makes these substitutions."""
    refs = []
    for r in d.references:
        obj = bank.object_for(r.name) if r.role == "prop" else None
        if obj is None:
            refs.append(r)
            continue
        desc = obj.description
        if obj.max_count == 1 and len(desc) + len(_ONLY_ONE) <= 200:
            desc += _ONLY_ONE
        refs.append(r.model_copy(update={"description": desc}))
    present = objects_in(d, bank)
    shots = []
    for s in d.shots:
        action, event = s.action, s.event
        for o in present:
            action, event = _glue(action, o), _glue(event, o)
        shots.append(s.model_copy(update={"action": action, "event": event}))
    return d.model_copy(update={"references": refs, "shots": shots})


def object_problems(d: AdPromptDraft, bank: CultureBank) -> list[str]:
    """A shot that calls a Gulf object by a generic word ("cups" for finjans) is sent back: the
    video model draws the generic word (the coffee video's teacups with handles)."""
    problems: list[str] = []
    for o in objects_in(d, bank):
        for s in d.shots:
            for field in ("action", "event"):
                text = re.sub(o.name_regex(), " ", getattr(s, field), flags=re.IGNORECASE)
                for w in o.wrong_words:
                    m = re.search(rf"\b(?:{w})\b", text, re.IGNORECASE)
                    if m:
                        problems.append(
                            f'shot {s.number} {field} says "{m.group(0)}" while the prompt has '
                            f"the {o.id}: call it the {o.always_say + ' ' if o.always_say else ''}"
                            f"{o.id} every time (the model draws the generic word)"
                        )
    return problems

"""The Saudi culture bank (ADR-0026, phase 4), data in ``config/culture_bank.toml``.

Rituals, proverbs, occasions, gestures, objects and phrases with their meaning, how an ad can
stand on them, the product categories they fit and their pitfalls. The insight stage receives the
entries that fit the brief's category and names, for each insight, the entry it stands on
(``culture_anchor``), so culture carries meaning instead of decorating the frame.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.ad import AdPromptDraft
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
    not_like: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list)

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


class CultureEntry(DomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    kind: Kind
    text_ar: Annotated[str, Field(min_length=1, max_length=120)]
    meaning_en: Text
    use: Text
    categories: list[str] = Field(min_length=1, max_length=8)
    objects: list[str] = Field(default_factory=list, max_length=4)  # GulfObject ids
    pitfalls: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list)


class CultureBank(DomainModel):
    aliases: dict[str, list[str]]
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

    def for_brief(self, brief: Brief, limit: int = 12) -> list[CultureEntry]:
        """Entries that fit the brief: its own categories first, then the ones for any product."""
        cats = set(self.categories_for(brief))
        own = [e for e in self.entries if cats & set(e.categories)]
        general = [e for e in self.entries if "any" in e.categories and e not in own]
        return (own + general)[:limit]

    @staticmethod
    def prompt_block(entries: list[CultureEntry]) -> str:
        if not entries:
            return "(no entry fits this product: anchor every insight to 'none')"
        lines = []
        for e in entries:
            pit = f" Never: {'; '.join(e.pitfalls)}." if e.pitfalls else ""
            lines.append(
                f"- {e.id} ({e.kind}) «{e.text_ar}»: {e.meaning_en} An ad can use it as: "
                f"{e.use}.{pit}"
            )
        return "\n".join(lines)


def with_gulf_objects(d: AdPromptDraft, bank: CultureBank) -> AdPromptDraft:
    """A prop that names a Gulf object (a dallah, a finjan) gets the bank's canonical description,
    so the prompt, the prop image and its check all describe the Gulf version, never a generic
    one. Code, not the writer, makes this substitution."""
    refs = []
    for r in d.references:
        obj = bank.object_for(r.name) if r.role == "prop" else None
        refs.append(r.model_copy(update={"description": obj.description}) if obj else r)
    return d.model_copy(update={"references": refs})

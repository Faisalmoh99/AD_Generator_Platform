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


class CultureEntry(DomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    kind: Kind
    text_ar: Annotated[str, Field(min_length=1, max_length=120)]
    meaning_en: Text
    use: Text
    categories: list[str] = Field(min_length=1, max_length=8)
    pitfalls: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list)


class CultureBank(DomainModel):
    aliases: dict[str, list[str]]
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
        return self

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

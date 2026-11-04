"""End-card copy: brand name and tagline shown after the footage, never inside it."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated

from pydantic import Field

from adsmith.domain.base import DomainModel
from adsmith.domain.enums import Language

# Real brands the agent must never put on an end card when it invents placeholder copy
# (fragrance houses common in the Gulf market plus very famous consumer brands). Deterministic
# guard behind the prompt's instruction; a hit goes through the schema-repair loop.
REAL_BRANDS: frozenset[str] = frozenset(
    """chanel dior gucci armani versace prada hermes hermès creed amouage ajmal rasasi lattafa
    givenchy burberry bvlgari bulgari guerlain lancome lancôme montblanc kilian byredo
    nike adidas apple samsung starbucks rolex cartier""".split()
) | frozenset(
    {
        "tom ford", "yves saint laurent", "ysl", "calvin klein", "hugo boss", "paco rabanne",
        "jo malone", "dolce & gabbana", "dolce and gabbana", "arabian oud", "swiss arabian",
        "abdul samad al qurashi", "jo loves", "louis vuitton",
        "شانيل", "ديور", "غوتشي", "أرماني", "فيرساتشي", "العربية للعود", "أجمل", "رصاصي",
        "لطافة", "عبد الصمد القرشي", "أمواج", "كريد", "توم فورد", "إيف سان لوران",
    }
)  # fmt: skip


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).lower()).strip()


def real_brand_in(text: str) -> str | None:
    t = f" {_norm(text)} "
    for brand in REAL_BRANDS:
        b = _norm(brand)
        if f" {b} " in t or (len(b) > 4 and b in t):
            return brand
    return None


class EndCardCopyDraft(DomainModel):
    """``endcard_copy`` LLM output: an INVENTED brand and tagline in the request language."""

    brand_name: Annotated[str, Field(min_length=1, max_length=40)]
    tagline: Annotated[str, Field(min_length=1, max_length=90)]


class EndCardCopy(DomainModel):
    brand_name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(min_length=1, max_length=120)
    language: Language
    # True when the agent invented it (no brand/tagline in the brief): shown as a PLACEHOLDER in
    # the trace and the Production Pack, never presented as the client's real brand.
    brand_placeholder: bool = False
    tagline_placeholder: bool = False

    @property
    def is_placeholder(self) -> bool:
        return self.brand_placeholder or self.tagline_placeholder

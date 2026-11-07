"""End-card copy written by the system (ADR-0028 part 2): five Arabic (or English) taglines
tied to the insight, three fictional brand names when the brief has none, scored by a judge on
``config/copy_rubric.toml``; code checks the lines, weights the scores and picks. A human's
brand or tagline (``--endcard-brand`` / ``--endcard-tagline``) always wins."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel
from adsmith.domain.endcard import real_brand_in

COPY_KEYS: tuple[str, ...] = (
    "insight_tie",
    "memorability",
    "dialect_fit",
    "sayability",
    "brand_fit",
)
TAGLINE_COUNT = 5
BRAND_COUNT = 3
Device = Literal["proverb_echo", "contrast", "pun", "rhyme", "plain"]
Note = Annotated[str, Field(min_length=1, max_length=240)]


class BrandDraft(DomainModel):
    name: Annotated[str, Field(min_length=1, max_length=30)] = Field(
        description="A fictional brand name, 1-2 words, in the request's script."
    )
    rationale: Note = Field(description="Why it fits, in one sentence (the user's language).")


class TaglineDraft(DomainModel):
    text: Annotated[str, Field(min_length=1, max_length=60)] = Field(
        description="The tagline as it appears on the end card, at most five words."
    )
    device: Device = Field(
        description="proverb_echo: echoes a known saying with a twist; contrast: two halves that "
        "answer each other; pun: a word with two meanings; rhyme: rhyme or rhythm (saj'); plain."
    )
    echoes: Annotated[str, Field(max_length=120)] = Field(
        description="The saying, phrase or ritual it plays on (in Arabic for Arabic), or empty."
    )
    gloss_en: Annotated[str, Field(min_length=1, max_length=160)] = Field(
        description="A literal English gloss, so a non-Arabic reader can follow."
    )
    ties_to: Note = Field(description="Which part of the film's truth or turn it says.")


class CopySetDraft(DomainModel):
    """``copywriter`` output: exactly five taglines; brands only when the brief has none."""

    brands: list[BrandDraft] = Field(max_length=BRAND_COUNT)
    taglines: list[TaglineDraft] = Field(min_length=TAGLINE_COUNT, max_length=TAGLINE_COUNT)


class TaglineScore(DomainModel):
    number: int = Field(ge=1, le=TAGLINE_COUNT)
    insight_tie: int = Field(ge=1, le=10)
    memorability: int = Field(ge=1, le=10)
    dialect_fit: int = Field(ge=1, le=10)
    sayability: int = Field(ge=1, le=10)
    brand_fit: int = Field(ge=1, le=10)
    generic: bool = Field(description="True if any brand in the category could sign this line.")
    note: Note = Field(description="Its biggest strength and weakness (the user's language).")

    def scores(self) -> dict[str, float]:
        return {k: float(getattr(self, k)) for k in COPY_KEYS}


class BrandScore(DomainModel):
    number: int = Field(ge=1, le=BRAND_COUNT)
    score: int = Field(ge=1, le=10)
    note: Note


class CopyJudgementDraft(DomainModel):
    """``copy_judge`` output: one score per tagline (and per brand when there are brands)."""

    taglines: list[TaglineScore] = Field(min_length=TAGLINE_COUNT, max_length=TAGLINE_COUNT)
    brands: list[BrandScore] = Field(max_length=BRAND_COUNT)

    @model_validator(mode="after")
    def _one_each(self) -> Self:
        if sorted(t.number for t in self.taglines) != list(range(1, TAGLINE_COUNT + 1)):
            raise ValueError("give exactly one score per tagline number 1..5")
        nums = [b.number for b in self.brands]
        if len(nums) != len(set(nums)):
            raise ValueError("give at most one score per brand number")
        return self


class CopyCriterion(DomainModel):
    weight: float = Field(gt=0, le=1)
    label: str = Field(min_length=1, max_length=60)
    question: str = Field(min_length=1, max_length=300)


class CopyBanned(DomainModel):
    empty: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    religious: list[str] = Field(default_factory=list)


class CopyRubric(DomainModel):
    generic_penalty: float = Field(ge=0, le=5)
    max_words: int = Field(ge=2, le=12)
    brand_max_words: int = Field(ge=1, le=4)
    criteria: dict[str, CopyCriterion]
    banned: CopyBanned = Field(default_factory=CopyBanned)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if tuple(self.criteria) != COPY_KEYS:
            raise ValueError(f"copy criteria must be exactly {COPY_KEYS} in that order")
        if abs(sum(c.weight for c in self.criteria.values()) - 1.0) > 1e-6:
            raise ValueError("copy rubric weights must sum to 1")
        return self

    def weighted(self, s: TaglineScore) -> float:
        total = sum(float(getattr(s, k)) * c.weight for k, c in self.criteria.items())
        return round(total - (self.generic_penalty if s.generic else 0.0), 4)

    def prompt_block(self) -> str:
        return "\n".join(
            f"- {k} ({c.label}, weight {c.weight:g}): {c.question}"
            for k, c in self.criteria.items()
        )

    def banned_block(self) -> str:
        b = self.banned
        return (
            f"Empty luxury words (never): {', '.join(b.empty)}\n"
            f"Claims (never): {', '.join(b.claims)}\n"
            f"Religious words and formulae (never on an ad): {', '.join(b.religious)}"
        )


_AR_DIACRITICS = re.compile(r"[ً-ْـ]")  # harakat and tatweel
_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9٠-٩]")
_MARKS = re.compile(r"[#@\"«»“”]")


def normalise(text: str) -> str:
    """Comparable form: NFKC, no diacritics or tatweel, alef/ya/ta-marbuta unified, lower case."""
    t = unicodedata.normalize("NFKC", text)
    t = _AR_DIACRITICS.sub("", t)
    t = re.sub("[إأآ]", "ا", t).replace("ى", "ي").replace("ة", "ه")
    return " ".join(t.lower().split())


def _hits(text: str, words: list[str]) -> list[str]:
    t = f" {normalise(text)} "
    out = []
    for w in words:
        n = normalise(w)
        pattern = (
            rf"\b{re.escape(n)}\b"
            if n.isascii()
            else rf"(?<!\S)(?:[وفبلك]?(?:ال)?){re.escape(n)}(?!\S)"
        )
        if re.search(pattern, t):
            out.append(w)
    return out


def line_problems(
    text: str, *, what: str, rubric: CopyRubric, arabic: bool, max_words: int
) -> list[str]:
    problems: list[str] = []
    words = text.split()
    if len(words) > max_words:
        problems.append(f"{what} «{text}» has {len(words)} words; at most {max_words}")
    if arabic and _LATIN.search(text):
        problems.append(f"{what} «{text}» mixes Latin letters into Arabic: write it in Arabic only")
    if _DIGIT.search(text) or _MARKS.search(text) or any(ord(ch) > 0xFFFF for ch in text):
        problems.append(f"{what} «{text}» has digits, quotes, hashtags or emoji: plain words only")
    b = rubric.banned
    for label, words_ in (("an empty luxury word", b.empty), ("a claim", b.claims),
                          ("a religious word", b.religious)):  # fmt: skip
        if hit := _hits(text, words_):
            problems.append(
                f"{what} «{text}» uses {label} ({', '.join(hit)}): say the insight instead"
            )
    if real := real_brand_in(text):
        problems.append(f"{what} «{text}» uses the real brand {real!r}: invent a fictional one")
    return problems


def copy_set_problems(
    d: CopySetDraft, *, rubric: CopyRubric, arabic: bool, need_brands: bool
) -> list[str]:
    """Code checks on the copywriter's output (→ schema repair)."""
    problems: list[str] = []
    for i, t in enumerate(d.taglines, 1):
        problems += line_problems(
            t.text, what=f"tagline {i}", rubric=rubric, arabic=arabic, max_words=rubric.max_words
        )
    seen = [normalise(t.text) for t in d.taglines]
    if len(set(seen)) != len(seen):
        problems.append("the five taglines must all be different")
    if need_brands and len(d.brands) != BRAND_COUNT:
        problems.append(f"the brief has no brand: write exactly {BRAND_COUNT} brand names")
    if not need_brands and d.brands:
        problems.append("the brief already has a brand: write no brand names (brands: [])")
    for i, b in enumerate(d.brands, 1):
        problems += line_problems(
            b.name, what=f"brand {i}", rubric=rubric, arabic=arabic,
            max_words=rubric.brand_max_words,
        )  # fmt: skip
    return problems


class CopyPick(DomainModel):
    text: str
    device: str = ""
    echoes: str = ""
    gloss_en: str = ""
    ties_to: str = ""
    weighted: float | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    generic: bool = False
    note: str = ""


class CopySelection(DomainModel):
    """The stored copy decision: every candidate with its scores, and what code picked."""

    dialect: str = Field(min_length=1, max_length=80)
    taglines: list[CopyPick] = Field(min_length=1, max_length=TAGLINE_COUNT)
    brands: list[CopyPick] = Field(default_factory=list, max_length=BRAND_COUNT)
    tagline_index: int = Field(ge=0)
    brand_index: int | None = Field(default=None, ge=0)
    prompt_hash: str = ""
    # Code's own picks, kept when a human chooses another line (ADR-0029 part 3); None = the
    # current index is code's pick.
    code_tagline_pick: int | None = Field(default=None, ge=0)
    code_brand_pick: int | None = Field(default=None, ge=0)

    @property
    def code_tagline_index(self) -> int:
        return self.tagline_index if self.code_tagline_pick is None else self.code_tagline_pick

    @property
    def code_brand_index(self) -> int | None:
        return self.brand_index if self.code_brand_pick is None else self.code_brand_pick

    @property
    def tagline(self) -> str:
        return self.taglines[self.tagline_index].text

    @property
    def brand(self) -> str | None:
        return None if self.brand_index is None else self.brands[self.brand_index].text


def pick(
    d: CopySetDraft, j: CopyJudgementDraft, rubric: CopyRubric, *, dialect: str, prompt_hash: str
) -> CopySelection:
    """Code decides: the highest weighted tagline (ties: insight_tie, then the shorter line, then
    the lower number); the highest-scored brand (ties: the lower number)."""
    by_num = {s.number: s for s in j.taglines}
    lines = []
    for i, t in enumerate(d.taglines, 1):
        s = by_num[i]
        lines.append(
            CopyPick(
                text=t.text, device=t.device, echoes=t.echoes, gloss_en=t.gloss_en,
                ties_to=t.ties_to, weighted=rubric.weighted(s), scores=s.scores(),
                generic=s.generic, note=s.note,
            )
        )  # fmt: skip
    best = max(
        range(len(lines)),
        key=lambda i: (lines[i].weighted or 0.0, lines[i].scores["insight_tie"],
                       -len(lines[i].text.split()), -i),
    )  # fmt: skip
    b_scores = {b.number: b for b in j.brands}
    brands = [
        CopyPick(
            text=b.name, ties_to=b.rationale,
            weighted=float(b_scores[i].score) if i in b_scores else None,
            note=b_scores[i].note if i in b_scores else "",
        )
        for i, b in enumerate(d.brands, 1)
    ]  # fmt: skip
    brand_index = (
        max(range(len(brands)), key=lambda i: (brands[i].weighted or 0.0, -i)) if brands else None
    )
    return CopySelection(
        dialect=dialect, taglines=lines, brands=brands, tagline_index=best,
        brand_index=brand_index, prompt_hash=prompt_hash,
    )  # fmt: skip


_HIJAZ = re.compile(
    r"(جد[ةه]|مك[ةه]|المدين[ةه]|الحجاز|حجازي|jeddah|jiddah|jidda|makkah|mecca|madinah|medina|hija?z)",
    re.IGNORECASE,
)


def copy_dialect(raw_request: str, *, arabic: bool) -> str:
    """The register of the copy (the user's rule, 2026-11-07): white Saudi (Najdi-leaning,
    understood everywhere) by default; Hijazi when the request names Jeddah, Makkah, Madinah or
    the Hijaz; English for an English request."""
    if not arabic:
        return "English"
    if _HIJAZ.search(raw_request):
        return "Hijazi Saudi dialect (the request names the Hijaz)"
    return "white Saudi dialect, Najdi-leaning, understood across the Kingdom and the Gulf"

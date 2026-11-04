"""Deterministic, dependency-free text similarity for concept distinctness.

Algorithm (see ``docs/adr/0009-concept-distinctness-metric.md``):
1. Normalise: NFKC + casefold; for Arabic, strip diacritics and tatweel, unify alef/ya/ta-marbuta
   forms, map Arabic-Indic digits, and strip the definite-article prefix (ال / وال / بال …).
2. Tokenise on non-word characters and drop EN/AR stop words and very short tokens.
3. Shingle every token into word-bounded character trigrams (``#scent#`` → ``#sc, sce, cen, ent,
   nt#``). Character n-grams absorb inflection (``reflection``/``reflections``) and Arabic clitics
   without a stemmer.
4. Similarity = Jaccard index of the two shingle sets.

Pure stdlib, O(n) per text, identical results on every machine. Embeddings were rejected at this
stage: they need a model download or API call, they are not bit-for-bit reproducible across
versions, and they are overkill for catching near-duplicate rewrites.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from itertools import combinations

# Pairs at or above this similarity are "not distinct". Calibrated on the fixture set in
# tests/fixtures/concept_texts.py (ADR-0009): genuinely distinct concepts score 0.05-0.13, the
# hardest distinct pair that shares a lot of vocabulary scores 0.29, and same-idea rewrites the
# metric can see score 0.39-0.75. 0.35 sits between the hardest distinct pair and the weakest
# detectable duplicate.
DISTINCTNESS_THRESHOLD: float = 0.35

_AR_DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_AR_CHAR_MAP = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ؤ": "و",
        "ئ": "ي",
        **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic digits
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # Extended Arabic-Indic digits
    }
)
_AR_ARTICLE_PREFIXES = ("وال", "بال", "كال", "فال", "لل", "ال")
_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)

_STOPWORDS_EN = frozenset(
    """a an the and or but of in on at to for with by from as is are was were be been being it its
    this that these those his her their our your my he she they we you i him them us into onto over
    under then than so very just while when where who whom which what as up down out off again
    each every all any both one two three own same such no not only also through about against
    between during before after above below has have had do does did will would can could should
    may might must""".split()
)
# Post-normalisation spellings (ة→ه, ى→ي, أ→ا) so they match normalised tokens.
_STOPWORDS_AR = frozenset(
    """في من علي الي عن مع هذا هذه ذلك تلك هو هي هم هن انا نحن انت انتم كان كانت يكون تكون ثم او
    و ف ب ل ك ان اذا لا ما لم لن قد كل بعد قبل عند حتي بين فوق تحت حين كما ايضا التي الذي الذين
    زي مره بس اللي يعني""".split()
)
_STOPWORDS = _STOPWORDS_EN | _STOPWORDS_AR
_MIN_TOKEN_LEN = 2


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _AR_DIACRITICS.sub("", text).replace(_TATWEEL, "")
    return text.translate(_AR_CHAR_MAP)


def _strip_ar_article(token: str) -> str:
    for prefix in _AR_ARTICLE_PREFIXES:
        if token.startswith(prefix) and len(token) - len(prefix) >= 3:
            return token[len(prefix) :]
    return token


def raw_tokens(text: str) -> list[str]:
    """Normalised tokens with the Arabic article stripped, but **no** stop-word filtering
    (pronouns like "he"/"هو" are signals for scene-flag detection)."""
    return [_strip_ar_article(t) for t in _TOKEN_SPLIT.split(normalize_text(text)) if t]


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN_SPLIT.split(normalize_text(text)):
        if not raw or raw.isdigit():
            continue
        tok = _strip_ar_article(raw)
        if len(tok) < _MIN_TOKEN_LEN or tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


def shingles(text: str, n: int = 3) -> frozenset[str]:
    grams: set[str] = set()
    for tok in tokens(text):
        padded = f"#{tok}#"
        if len(padded) <= n:
            grams.add(padded)
            continue
        grams.update(padded[i : i + n] for i in range(len(padded) - n + 1))
    return frozenset(grams)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0  # two empty texts are identical, not distinct
    return len(a & b) / len(a | b)


def similarity(a: str, b: str) -> float:
    return jaccard(shingles(a), shingles(b))


def pairwise_similarities(texts: Sequence[str]) -> dict[tuple[int, int], float]:
    sets = [shingles(t) for t in texts]
    return {(i, j): jaccard(sets[i], sets[j]) for i, j in combinations(range(len(sets)), 2)}


def too_similar_pairs(
    texts: Sequence[str], threshold: float = DISTINCTNESS_THRESHOLD
) -> list[tuple[int, int, float]]:
    """Index pairs (0-based) whose similarity is >= threshold, most similar first."""
    hits = [(i, j, s) for (i, j), s in pairwise_similarities(texts).items() if s >= threshold]
    return sorted(hits, key=lambda h: -h[2])


def join_fields(parts: Iterable[str]) -> str:
    return " \n ".join(p for p in parts if p)


_ARABIC_LETTER = re.compile("[\u0621-\u064a\u0671-\u06d3]")
_LATIN_LETTER = re.compile("[A-Za-z]")


def detect_language(text: str) -> str:
    """Deterministic script-based language guess ("ar" | "en"). Arabic wins if at least 30% of the
    letters are Arabic: brand names and English loanwords are common inside Arabic requests."""
    ar = len(_ARABIC_LETTER.findall(text))
    latin = len(_LATIN_LETTER.findall(text))
    if ar + latin == 0:
        return "en"
    return "ar" if ar / (ar + latin) >= 0.3 else "en"

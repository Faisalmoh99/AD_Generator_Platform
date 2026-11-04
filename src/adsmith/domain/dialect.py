"""Deterministic Gulf/Saudi dialect cues, to correct the LLM's dialect label.

Real run: "أبي إعلان لتطبيق توصيل، 10 ثواني، حماسي" was labelled "other" at low effort, although
"أبي" (= I want) is a textbook Gulf/Najdi marker. Only overrides ``msa``/``other``; a specific
Saudi/Gulf label from the model is kept.
"""

from __future__ import annotations

from adsmith.domain.enums import Dialect
from adsmith.domain.similarity import normalize_text, raw_tokens

# Normalised spellings (أ→ا, ى→ي, ة→ه, diacritics removed).
_HIJAZI = frozenset("ايش كدا كده دحين دحينه ايوه مره بالمره".split())
_NAJDI = frozenset("ابي ابغي وش سو سوي سولي خل خلي الحين ودي يبي يبغي".split())
_GULF = frozenset("شلون وايد هالحين جي".split())


def dialect_cues(text: str) -> dict[Dialect, list[str]]:
    toks = [normalize_text(t) for t in raw_tokens(text)]
    found: dict[Dialect, list[str]] = {}
    for dialect, cues in (
        (Dialect.SAUDI_HIJAZI, _HIJAZI),
        (Dialect.SAUDI_NAJDI, _NAJDI),
        (Dialect.GULF, _GULF),
    ):
        hits = sorted({t for t in toks if t in cues})
        if hits:
            found[dialect] = hits
    return found


def corrected_dialect(text: str, llm_label: Dialect) -> tuple[Dialect, list[str]] | None:
    """A better label when the model said msa/other but the text has clear cues, else None."""
    if llm_label not in {Dialect.MSA, Dialect.OTHER}:
        return None
    cues = dialect_cues(text)
    if not cues:
        return None
    best = max(cues, key=lambda d: len(cues[d]))
    return best, cues[best]

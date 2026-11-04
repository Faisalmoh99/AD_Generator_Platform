"""Fail-safe enforcement of ``Scene.shows_character`` / ``shows_product``.

These flags decide which scenes a continuity-bible change invalidates (ADR-0005). They come from the
LLM, and the two possible errors have very different costs (ADR-0010):
- false positive (flag true, element absent): one unnecessary regeneration after a bible edit.
- false negative (flag false, element present): a **silent continuity bug**. The scene keeps the
  old wardrobe or bottle after the bible changes.

So we bias toward ``True``. The schema defaults to ``True``, the prompt says "when in doubt, mark
true", and this deterministic cross-check **upgrades** a flag to ``True`` whenever the scene text
mentions the character or product. It never downgrades. Every override is returned so the caller
can emit a ``decision`` event explaining it.

Runs twice: at the storyboard stage with generic EN/AR terms, and again once the bible exists,
with the bible's own names and terms added.
"""

from __future__ import annotations

from typing import Literal

from adsmith.domain.base import DomainModel
from adsmith.domain.continuity import ContinuityBible
from adsmith.domain.enums import Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.scene import Scene
from adsmith.domain.similarity import normalize_text, raw_tokens

Flag = Literal["shows_character", "shows_product"]

# Normalised spellings (ة→ه, أ→ا, article stripped) so they match normalised tokens.
CHARACTER_TERMS_EN: frozenset[str] = frozenset(
    """man men guy he his him himself boy gentleman person model protagonist hero character face
    hand hands eyes wrist neck jaw silhouette figure""".split()
)
CHARACTER_TERMS_AR: frozenset[str] = frozenset(
    """شاب شابا رجل الرجل هو عارض بطل شخص وجه وجهه يد يده يديه عين عينيه رقبه معصم""".split()
)
PRODUCT_TERMS_EN: frozenset[str] = frozenset(
    """perfume fragrance cologne scent bottle flacon spray atomizer product cap label
    packshot""".split()
)
PRODUCT_TERMS_AR: frozenset[str] = frozenset("""عطر زجاجه قاروره رشه بخاخ منتج غطاء ملصق""".split())
_AR_CLITICS = ("و", "ب", "ل", "ف", "ك")
_MIN_BIBLE_TERM_LEN = 4


class FlagOverride(DomainModel):
    scene_id: str
    scene_order: int
    flag: Flag
    matched_terms: list[str]

    def as_event(self, run_id: str, stage: Stage) -> AgentEvent:
        what = "character" if self.flag == "shows_character" else "product"
        terms = ", ".join(self.matched_terms[:5])
        return AgentEvent(
            run_id=run_id,
            stage=stage,
            scene_id=self.scene_id,
            type=EventType.DECISION,
            summary=(
                f"Scene {self.scene_order}: set {self.flag}=true because the scene text mentions "
                f"the {what} ({terms}). Bias toward true: a missed flag would break continuity."
            ),
            payload={
                "flag": self.flag,
                "matched_terms": list(self.matched_terms),
                "override": True,
            },
        )


def _matches(token: str, term: str) -> bool:
    if token == term:
        return True
    if term.isascii():
        return token in {term + "s", term + "es"}  # plural only; no substring matches in English
    # Arabic: attached conjunction/preposition, or short suffixes (pronouns, dual/plural).
    stem = token[1:] if token[:1] in _AR_CLITICS and len(token) > len(term) else token
    if len(term) < 3:  # very short words (هو) match exactly, or "هواء" (air) would count
        return stem == term
    return stem == term or (stem.startswith(term) and len(stem) - len(term) <= 2)


def _bible_terms(bible: ContinuityBible | None) -> tuple[frozenset[str], frozenset[str]]:
    if bible is None:
        return frozenset(), frozenset()

    def content(*texts: str) -> frozenset[str]:
        out: set[str] = set()
        for t in texts:
            out.update(tok for tok in raw_tokens(t) if len(tok) >= _MIN_BIBLE_TERM_LEN)
        return frozenset(out)

    char: frozenset[str] = frozenset()
    if bible.character is not None:
        # Wardrobe items are the strongest signal: "his black thobe" ⇒ the character is on screen.
        char = content(*bible.character.wardrobe)
    p = bible.product
    prod = content(p.name, p.label_text) | {normalize_text(p.name)}
    return char, prod


def _find(text_tokens: list[str], terms: frozenset[str]) -> list[str]:
    hits: list[str] = []
    for term in sorted(terms):
        if any(_matches(tok, term) for tok in text_tokens) and term not in hits:
            hits.append(term)
    return hits


def detect_mentions(scene: Scene, bible: ContinuityBible | None = None) -> dict[Flag, list[str]]:
    """Terms found per flag in the scene's description, action and setting."""
    text = " ".join([scene.description, scene.shot.subject_action, scene.shot.composition])
    toks = raw_tokens(text)
    bible_char, bible_prod = _bible_terms(bible)
    return {
        "shows_character": _find(toks, CHARACTER_TERMS_EN | CHARACTER_TERMS_AR | bible_char),
        "shows_product": _find(toks, PRODUCT_TERMS_EN | PRODUCT_TERMS_AR | bible_prod),
    }


def enforce_scene_flags(
    scenes: list[Scene], bible: ContinuityBible | None = None
) -> list[FlagOverride]:
    """Upgrade false flags to true where the text mentions the element. Never downgrades."""
    overrides: list[FlagOverride] = []
    for scene in scenes:
        for flag, hits in detect_mentions(scene, bible).items():
            if hits and not getattr(scene, flag):
                setattr(scene, flag, True)
                overrides.append(
                    FlagOverride(
                        scene_id=scene.id, scene_order=scene.order, flag=flag, matched_terms=hits
                    )
                )
    return overrides

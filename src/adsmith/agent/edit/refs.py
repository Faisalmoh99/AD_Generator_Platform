"""Deterministic scene-reference resolution (PLAN §6 step 1).

The LLM parser says HOW the user referred to a scene (``SceneRef``); this module maps that to
scene ids with a lookup table, so "the second scene" can never silently become scene 3. Arabic
ordinals (masculine/feminine, with or without the article), Arabic-Indic digits, English
ordinals and words like "last", "opening", "final" are covered. Anything else is unresolved and
the service asks the user instead of guessing.
"""

from __future__ import annotations

import re

from adsmith.domain.edit import SceneRef
from adsmith.domain.storyboard import Storyboard

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_ORDINALS: dict[str, int] = {
    # English
    "first": 1, "1st": 1, "opening": 1,  # not "one": "the second one" must stay scene 2
    "second": 2, "2nd": 2, "two": 2,
    "third": 3, "3rd": 3, "three": 3,
    "fourth": 4, "4th": 4, "four": 4,
    "fifth": 5, "5th": 5, "five": 5,
    "sixth": 6, "6th": 6, "six": 6,
    # Arabic (stored in logical order; with/without "ال", masculine and feminine)
    "الأول": 1, "الاول": 1, "الأولى": 1, "الاولى": 1, "أول": 1, "اول": 1, "الافتتاحي": 1,
    "الثاني": 2, "الثانية": 2, "ثاني": 2,
    "الثالث": 3, "الثالثة": 3, "ثالث": 3,
    "الرابع": 4, "الرابعة": 4, "رابع": 4,
    "الخامس": 5, "الخامسة": 5, "خامس": 5,
    "السادس": 6, "السادسة": 6, "سادس": 6,
}  # fmt: skip
_LAST = frozenset(
    {"last", "final", "ending", "closing", "الأخير", "الاخير", "الأخيرة", "الاخيرة", "آخر", "اخر"}
)
_NUMBER = re.compile(r"\d+")


class UnresolvedRefError(ValueError):
    """A reference that does not map to exactly one known scene."""


def resolve_ref(ref: SceneRef, storyboard: Storyboard) -> list[str]:
    scenes = storyboard.scenes
    if ref.kind == "all":
        return [s.id for s in scenes]
    if ref.kind == "id":
        if ref.value not in {s.id for s in scenes}:
            raise UnresolvedRefError(f"unknown scene id {ref.value!r}")
        return [ref.value]
    text = ref.value.strip().lower().translate(_AR_DIGITS)
    words = re.findall(r"[\w؀-ۿ]+", text)
    if any(w in _LAST for w in words):
        return [scenes[-1].id]
    numbers = [int(n) for n in _NUMBER.findall(text)]
    ordinals = [_ORDINALS[w] for w in words if w in _ORDINALS]
    candidates = numbers or ordinals
    if len(set(candidates)) == 1:
        n = candidates[0]
        if 1 <= n <= len(scenes):
            return [scenes[n - 1].id]
        raise UnresolvedRefError(f"there is no scene {n} (the storyboard has {len(scenes)})")
    raise UnresolvedRefError(f"could not tell which scene {ref.value!r} means")


def resolve_targets(refs: list[SceneRef], storyboard: Storyboard) -> list[str]:
    """All referenced scene ids, in storyboard order, without duplicates."""
    wanted = {sid for ref in refs for sid in resolve_ref(ref, storyboard)}
    return [s.id for s in storyboard.scenes if s.id in wanted]

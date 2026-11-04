"""Deterministic production lints for shots and prompts (M4c).

Both feed the schema-repair loop (via ``convert`` in the storyboard/prompt nodes), the same
pattern as the duration-sum check. They are heuristics, tuned to be **conservative**: they catch
the patterns seen in real runs and accept some misses rather than nag about good shots. Every
rule is pinned by tests built from real outputs.

1. ``multi_action_problem``: a shot must have ONE primary action (AI video garbles busy shots).
   Signals: sequence words ("then", "before", "finally"…) and too many simultaneous action
   clauses joined by "and / while / as / , <verb>ing".
2. ``negative_contradictions``: the negative prompt must not exclude what the shot itself
   requires (e.g. "additional people" while a courier must appear).
"""

from __future__ import annotations

import re

MAX_ACTION_JOINTS = 2  # ≤3 simultaneous clauses (e.g. subject + camera + atmosphere) is fine

# Only unambiguous sequence words. Not "before" ("stands before it"), "once" ("sprays once") or
# "next" ("next to"): real-run false positives. Busy shots are still caught by the clause count.
_SEQUENCE = re.compile(
    r"\b(then|afterwards?|after that|finally|followed by|until|eventually)\b", re.IGNORECASE
)
_DETERMINERS = r"(?:the|his|her|its|their|he|she|they|it|a|an)"
# A new clause: "and/while/as" + a subject, or "and" + a verb-like word ("and smiles").
_JOINTS = re.compile(
    rf"\b(?:while|whilst)\b"
    rf"|\bas\s+(?:the|his|her|its|their|he|she|they|it)\b"
    rf"|\band\s+{_DETERMINERS}\b"
    rf"|\band\s+[a-z]+(?:s|ing)\b"
    rf"|,\s*[a-z]+ing\b",
    re.IGNORECASE,
)
_NOT_VERBS = frozenset(  # plural nouns that look like verbs after "and"
    "glass dates drinks lights shadows reflections sparks lines beams flowers stairs things".split()
)


def _joints(text: str) -> list[str]:
    hits = []
    for m in _JOINTS.finditer(text):
        word = m.group(0).split()[-1].lower().strip(",")
        if m.group(0).lower().startswith("and") and word in _NOT_VERBS:
            continue
        hits.append(m.group(0).strip(", "))
    return hits


def multi_action_problem(subject_action: str) -> str | None:
    """None if the shot has one primary action, else a repair-friendly explanation."""
    seq = sorted({m.group(0).lower() for m in _SEQUENCE.finditer(subject_action)})
    if seq:
        return (
            f"describes a sequence ({', '.join(repr(s) for s in seq)}): one shot must be one "
            "continuous action; move later beats to the next scene"
        )
    joints = _joints(subject_action)
    if len(joints) > MAX_ACTION_JOINTS:
        return (
            f"has {len(joints) + 1} simultaneous actions (joined by "
            f"{', '.join(repr(j) for j in joints)}): keep one primary action, plus at most the "
            "camera move and one atmospheric motion"
        )
    return None


# ------------------------------------------------------------------------------ negatives
_PERSON_ROLES = frozenset(
    """man men woman women boy boys girl girls person persons people courier couriers driver
    drivers barista baristas chef chefs waiter waiters friend friends family child children guest
    guests customer customers crowd father mother son daughter master tailor rider riders worker
    workers grandfather grandmother elder elders player players""".split()
)
_PLURAL_PEOPLE = frozenset(
    "men women boys girls people persons friends family children guests customers crowd".split()
)
_PRONOUN_ACTORS = frozenset("he she his her him".split())
_GENERIC_PEOPLE = frozenset("people person persons humans human figures characters".split())
_QUALIFIERS = frozenset(
    "additional extra other more second multiple duplicate deformed distorted visible background "
    "random".split()
)
_NEGATED = re.compile(r"\b(?:no|without|never|not)\b[^,.;]*", re.IGNORECASE)
_WORD = re.compile(r"[a-z]+")


_IRREGULAR = {"men": "man", "women": "woman", "children": "child", "people": "person"}


def _stem(word: str) -> str:
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _required_raw(*texts: str) -> set[str]:
    """Words the shot requires (negated mentions such as "no text in frame" removed)."""
    words: set[str] = set()
    for t in texts:
        words |= set(_WORD.findall(_NEGATED.sub(" ", t.lower())))
    return words


def _people_required(raw: set[str]) -> int:
    """How many people the shot needs. Plurality is judged on raw words, before stemming."""
    if raw & _PLURAL_PEOPLE:
        return 2
    roles = {_stem(w) for w in raw if w in _PERSON_ROLES}
    if not roles and raw & _PRONOUN_ACTORS:
        return 1
    return len(roles)


def negative_contradictions(subject_action: str, composition: str, negative: str) -> list[str]:
    """Negative-prompt terms that exclude something this shot requires."""
    raw_required = _required_raw(subject_action, composition)
    required = {_stem(w) for w in raw_required}
    people = _people_required(raw_required)
    problems = []
    for raw in negative.split(","):
        term = raw.strip().lower()
        words = [_stem(w) for w in _WORD.findall(term)]
        if not words:
            continue
        qualified = bool(set(_WORD.findall(term)) & _QUALIFIERS)
        term_raw = set(_WORD.findall(term))
        if term_raw & (_GENERIC_PEOPLE | _PERSON_ROLES):
            generic = bool(term_raw & _GENERIC_PEOPLE)
            if not qualified and people >= 1 and (set(words) & required or generic):
                problems.append(f"'{term}' excludes a person this shot requires")
            elif (
                qualified
                and people >= 2
                and term.split()[0] in {"additional", "extra", "other", "more"}
            ):
                problems.append(f"'{term}' excludes the second person this shot requires")
            continue
        if not qualified and len(words) == 1 and words[0] in required:
            problems.append(f"'{term}' excludes '{words[0]}', which this shot requires")
    return problems

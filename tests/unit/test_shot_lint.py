"""M4c lints, pinned by real-run outputs (quoted verbatim where noted)."""

from __future__ import annotations

import pytest

from adsmith.domain.dialect import corrected_dialect
from adsmith.domain.enums import Dialect
from adsmith.domain.shot_lint import multi_action_problem, negative_contradictions

BUSY = [
    # real run 3, scene 2 (delivery app)
    "She taps the phone screen with her thumb and smiles as the focus shifts from her phone to the "
    "faceless courier silhouette setting a large bag on the sand and waving.",
    # real run 1, scene 3 (perfume scale reveal)
    "The camera pulls back slowly so the perspective settles and the monument is revealed as a "
    "bottle on a table, while the young man stands before it and gives a faint, confident nod.",
    # real run 3 (first pass), scene 1: a sequence
    "Steam curls upward from the pot, then a pulsing golden light thread ignites inside it.",
]
FINE = [
    "The camera glides slowly sideways along the table while condensation beads slide down the "
    "glass and the ice shifts slightly.",
    "The camera tilts slowly upward along the amber glass monument as the young man beside it "
    "raises his head and looks up at it.",
    "A dune buggy races over a dune crest, kicking up sand and dust, with its headlight beams "
    "slicing through the blue dusk toward the distant campfire.",
    "Coffee drips steadily from the paper filter into the glass server, ripples spreading across "
    "the surface as the lattice light shifts left to right.",
    "The master tailor adjusts the young man's collar.",
    "His hand lifts the bottle and sprays once.",  # "once" is not a sequence
    "The man stands before the window.",  # spatial "before" is not a sequence
]


@pytest.mark.parametrize("text", BUSY, ids=["two-actors", "busy-reveal", "then-sequence"])
def test_busy_shots_are_flagged(text: str) -> None:
    assert multi_action_problem(text) is not None


@pytest.mark.parametrize("text", FINE, ids=range(len(FINE)))
def test_single_action_shots_pass(text: str) -> None:
    assert multi_action_problem(text) is None


def test_message_is_repair_friendly() -> None:
    msg = multi_action_problem(BUSY[0])
    assert msg is not None and "keep one primary action" in msg


@pytest.mark.parametrize(
    ("action", "composition", "negative", "expected"),
    [
        # real run 3 scene 2: courier must appear, negative excluded "additional people"
        (
            BUSY[0],
            "Young woman left third, courier silhouette upper right",
            "text, additional people, visible courier face",
            ["additional people"],
        ),
        ("She walks to the door", "woman in the doorway", "text, women", ["women"]),
        (
            "His hand lifts the bottle",
            "hand and bottle in foreground",
            "bottles, text",
            ["bottles"],
        ),
    ],
)
def test_contradictions_detected(
    action: str, composition: str, negative: str, expected: list[str]
) -> None:
    found = negative_contradictions(action, composition, negative)
    assert [f.split("'")[1] for f in found] == expected


@pytest.mark.parametrize(
    ("action", "composition", "negative"),
    [
        ("The man walks left to right", "man crossing", "additional people, women, extra fingers"),
        (
            "The camera pushes in on the lone bottle",
            "bottle centered",
            "people, man, duplicate bottle",
        ),
        (
            "Coffee drips into the server",
            "No text on any surface",
            "text, letters, cold white light",
        ),
        ("Two friends laugh by the fire", "friends left", "crowds, visible faces of bystanders"),
    ],
)
def test_legitimate_negatives_pass(action: str, composition: str, negative: str) -> None:
    assert negative_contradictions(action, composition, negative) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("أبي إعلان لتطبيق توصيل، 10 ثواني، حماسي", Dialect.SAUDI_NAJDI),  # real run 3
        ("ابغى اعلان كدا حلو دحين", Dialect.SAUDI_HIJAZI),
        ("وش رايك نسوي اعلان", Dialect.SAUDI_NAJDI),
    ],
)
def test_gulf_cues_correct_other_label(text: str, expected: Dialect) -> None:
    fix = corrected_dialect(text, Dialect.OTHER)
    assert fix is not None and fix[0] is expected


def test_dialect_left_alone_when_msa_or_already_specific() -> None:
    assert corrected_dialect("أريد إعلاناً لعطر فاخر", Dialect.MSA) is None
    assert corrected_dialect("أبي إعلان", Dialect.GULF) is None

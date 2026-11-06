"""PLAN_V2 item 2: the shot vocabulary, flags from data, and the person rule in a montage."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adsmith.config import Settings
from adsmith.domain.formats import AdFormat, grammar_problems
from adsmith.domain.vocabulary import ShotVocab, VocabLibrary, entry_block
from tests.media_helpers import fast_settings, make_harness
from tests.unit.test_formats import board

REQUEST = "Make me a 12-second ad for a luxury men's perfume for young men, luxurious and bold."


def test_vocabulary_loads_and_is_consistent(settings: Settings) -> None:
    lib = settings.vocabulary
    assert len(lib.shots) >= 14
    montage = settings.formats.grammar(AdFormat.MONTAGE)
    for name, v in lib.shots.items():
        assert v.fallback in lib.shots, name
        assert v.camera in montage.allowed_camera, name
        if v.shows_person:
            assert v.framing in montage.person_framing, name
    key = sorted(n for n, v in lib.shots.items() if v.key_shot)
    assert key == ["bottle_into_water", "hero_push_in", "spray_from_nozzle", "water_ribbon"]
    assert "spray_from_nozzle" in lib.prompt_block() and "spray_burst" not in lib.shots


def test_identity_mode_must_match_product(settings: Settings) -> None:
    data: dict[str, Any] = settings.vocabulary.shots["water_drop_leaf"].model_dump()
    with pytest.raises(ValidationError, match="identity_mode is 'none' exactly"):
        ShotVocab.model_validate(data | {"identity_mode": "direct"})
    with pytest.raises(ValidationError, match="unknown fallback"):
        VocabLibrary.model_validate({"shots": {"x": data | {"fallback": "nope"}}})


def test_entry_block_says_when_there_is_no_product(settings: Settings) -> None:
    lib = settings.vocabulary
    assert "NO product: no bottle, no hand" in entry_block(
        "water_drop_leaf", lib.get("water_drop_leaf")
    )
    assert "no vocabulary entry" in entry_block(None, None)


def test_grammar_needs_a_known_vocab_per_shot(settings: Settings) -> None:
    g, lib = settings.formats.grammar(AdFormat.MONTAGE), settings.vocabulary.shots
    d = board()
    assert grammar_problems(d.scenes, g, lib) == []
    d.scenes[1].vocab = None
    d.scenes[2].vocab = "dancing_man"
    text = "; ".join(grammar_problems(d.scenes, g, lib))
    assert "scene 2 has no vocab" in text and "scene 3 vocab 'dancing_man' is unknown" in text
    d = board()
    d.scenes[-1].vocab = "water_drop_leaf"
    assert "final shot must use a vocabulary entry that shows the product" in "; ".join(
        grammar_problems(d.scenes, g, lib)
    )


async def test_full_figure_person_in_a_person_free_shot_is_repaired(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    assert h.llm is not None
    good = h.llm._scripts["storyboard"][0]
    bad = copy.deepcopy(good)
    bad["output"]["scenes"][1]["shot"]["subject_action"] = (
        "A man in a long white robe walks across the frame in slow motion."
    )
    h.llm.override("storyboard", [bad, good])
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id, stop_before=None)
    assert state.storyboard is not None and h.llm.calls_for("storyboard") == 2
    names = [r.template for r in h.llm.requests]
    repair = h.llm.requests[names.index("storyboard@v8") + 1].messages[-1]
    text = repair.parts[0].text  # type: ignore[union-attr]
    assert "vocab 'water_drop_leaf' shows no person" in text and "man" in text


async def test_flags_come_from_the_vocabulary_not_from_text(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    assert h.llm is not None
    board = copy.deepcopy(h.llm._scripts["storyboard"][0])
    drop = board["output"]["scenes"][1]
    drop["description"] = "A single drop, cool as the perfume itself, lands on a dark leaf."
    h.llm.override("storyboard", [board])
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.storyboard is not None
    leaf = state.storyboard.scenes[1]
    # The text says "perfume" (a product term), the entry says no product: the entry wins.
    assert leaf.vocab == "water_drop_leaf" and "perfume" in leaf.description
    assert leaf.shows_product is False and leaf.shows_character is False
    hand = state.storyboard.scenes[7]
    assert hand.vocab == "hand_cuff" and hand.shows_character and hand.shows_product

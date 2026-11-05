"""PLAN_V2 item 1: the format library, format resolution and the montage grammar validator."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from adsmith.config import Settings
from adsmith.domain.enums import CameraMove, ScenePurpose
from adsmith.domain.formats import AdFormat, format_block, grammar_problems, resolve_format
from adsmith.domain.storyboard import StoryboardDraft
from adsmith.providers.llm.scripted import FIXTURES_DIR

MONTAGE = FIXTURES_DIR / "montage_en"


def board() -> StoryboardDraft:
    steps: list[dict[str, Any]] = json.loads((MONTAGE / "storyboard.json").read_text())
    return StoryboardDraft.model_validate(copy.deepcopy(steps[0]["output"]))


def problems(settings: Settings, draft: StoryboardDraft) -> str:
    return "; ".join(grammar_problems(draft.scenes, settings.formats.grammar(AdFormat.MONTAGE)))


def test_library_loads_every_format(settings: Settings) -> None:
    lib = settings.formats
    assert set(lib.formats) == set(AdFormat)
    m = lib.grammar(AdFormat.MONTAGE)
    assert (m.min_shots, m.max_shots, m.kept_s) == (8, 12, (1.0, 1.5))
    assert "Format: montage" in format_block(lib, AdFormat.MONTAGE)


@pytest.mark.parametrize(
    ("requested", "shots", "physical", "expected"),
    [
        (None, None, True, AdFormat.MONTAGE),
        (None, None, False, AdFormat.NARRATIVE),
        (AdFormat.NARRATIVE, None, True, AdFormat.NARRATIVE),
        (None, 1, True, AdFormat.SINGLE_TAKE),
        (None, 3, True, AdFormat.NARRATIVE),  # too few shots for a montage
        (None, 9, True, AdFormat.MONTAGE),
    ],
)
def test_resolve_format(
    settings: Settings,
    requested: AdFormat | None,
    shots: int | None,
    physical: bool,
    expected: AdFormat,
) -> None:
    fmt, why = resolve_format(
        settings.formats, requested=requested, requested_shots=shots, physical_product=physical
    )
    assert fmt is expected and why


def test_fixture_montage_passes_the_grammar(settings: Settings) -> None:
    assert problems(settings, board()) == ""


def test_too_few_shots(settings: Settings) -> None:
    d = board()
    d.scenes = d.scenes[:7]
    assert "needs 8-12 shots, not 7" in problems(settings, d)


def test_kept_duration_out_of_range(settings: Settings) -> None:
    d = board()
    d.scenes[0].duration_s = 2.5
    assert "each shot lasts 1-1.5s" in problems(settings, d)


def test_hero_must_be_last(settings: Settings) -> None:
    d = board()
    d.scenes[-1].purpose = ScenePurpose.BUILD
    assert "final shot must be the product hero" in problems(settings, d)


def test_person_cap_and_position(settings: Settings) -> None:
    d = board()
    d.scenes[2].shows_character = True
    text = problems(settings, d)
    assert "2 shots show a person" in text
    assert "must be last or second to last, not shot 3" in text


def test_camera_must_be_in_the_allowed_list(settings: Settings) -> None:
    d = board()
    d.scenes[1].shot.camera_movement = CameraMove.HANDHELD
    assert "camera moves not allowed in this format (2: handheld)" in problems(settings, d)


def test_montage_scene_bounds_are_the_kept_window(settings: Settings) -> None:
    from adsmith.agent.nodes.common import scene_duration_bounds  # noqa: PLC0415

    assert scene_duration_bounds(settings, None, AdFormat.MONTAGE) == (1.0, 1.5, 1.5)
    assert scene_duration_bounds(settings, None, AdFormat.NARRATIVE)[:2] == (3.0, 8.0)


def _vocab_problems(settings: Settings, draft: StoryboardDraft) -> str:
    g = settings.formats.grammar(AdFormat.MONTAGE)
    return "; ".join(grammar_problems(draft.scenes, g, settings.vocabulary.shots))


def test_montage_needs_three_physical_events(settings: Settings) -> None:
    d = board()
    assert _vocab_problems(settings, d) == ""  # fixture: spray, droplets, drop, splash
    for s in d.scenes:
        if s.vocab in {"water_drop_leaf", "droplets_on_glass"}:
            s.vocab = "turntable_glint"
    text = _vocab_problems(settings, d)
    assert "only 2 physical-event shot(s); this format needs at least 3" in text
    assert "spray_from_nozzle" in text


def test_montage_ends_on_the_hero_push_in(settings: Settings) -> None:
    d = board()
    d.scenes[-1].vocab = "bottle_on_rock"
    assert "final shot must use the vocabulary entry hero_push_in" in _vocab_problems(settings, d)


def test_format_block_states_the_event_rule(settings: Settings) -> None:
    text = format_block(settings.formats, AdFormat.MONTAGE)
    assert "At least 3 shots are physical events" in text and "hero_push_in" in text
    vocab = settings.vocabulary.prompt_block()
    assert "spray_from_nozzle" in vocab and "EVENT; PROVEN" in vocab

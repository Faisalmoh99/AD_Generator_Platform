"""P2 (ADR-0028): the Seedance prompt with a word budget, the identity and the key moment
first, "Cut to:" between shots, the physics failure library, and the shot count picked by code
from the ad's length (3 shots at 6 s). Pure, no I/O except one zero-key run."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.ad import (
    AdPromptDraft,
    parse_prompt_shots,
    render_ad_prompt,
    word_count,
    word_problems,
    writer_words,
)
from adsmith.domain.creative_rules import CreativeRules, creative_rule_problems
from adsmith.domain.enums import Stage
from adsmith.domain.formats import AdFormat, whole_ad_shot_count
from adsmith.providers.llm.scripted import FIXTURES_DIR, ScriptedLLMProvider
from tests.integration.test_multishot_pipeline import photo, runtime, settings
from tests.unit.test_creative_rules import draft, good, shot


@pytest.fixture(scope="module")
def rules() -> CreativeRules:
    return Settings().creative_rules


def writer_fixture() -> AdPromptDraft:
    raw = json.loads((FIXTURES_DIR / "multishot_en" / "ad_prompt_writer.json").read_text())
    return AdPromptDraft.model_validate(raw[0]["output"])


# ----------------------------------------------------------------------------- shot count


@pytest.mark.parametrize(("seconds", "shots"), [(4, 3), (6, 3), (6.9, 3), (7, 4), (8, 4), (9, 4)])
def test_code_picks_the_shot_count_from_the_length(seconds: float, shots: int) -> None:
    g = Settings().formats.grammar(AdFormat.MULTISHOT)
    assert whole_ad_shot_count(g, seconds) == shots


async def test_a_six_second_ad_gets_three_shots_hook_turn_hero(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    understand = json.loads((FIXTURES_DIR / "multishot_en" / "understand.json").read_text())
    understand[0]["output"]["duration_s"] = 6.0
    llm.override("understand", understand)
    ideas = json.loads((FIXTURES_DIR / "multishot_en" / "ideate.json").read_text())
    for c in ideas[0]["output"]["concepts"]:  # a 6 s film has 3 shots (ADR-0029: ideate knows)
        c["shot_sketch"] = c["shot_sketch"][:3]
        c["device_shot"] = min(c["device_shot"], 3)
    llm.override("ideate", ideas)
    four = json.loads((FIXTURES_DIR / "multishot_en" / "storyboard.json").read_text())[0]
    three = copy.deepcopy(four)
    scenes = three["output"]["scenes"]
    three["output"]["scenes"] = [scenes[0], scenes[2], scenes[3]]  # hook, reveal, product
    shrunk = copy.deepcopy(four)
    for s in shrunk["output"]["scenes"]:
        s["duration_s"] = 1.5  # four shots squeezed into 6 s: the turn and hero lose time
    llm.override("storyboard", [shrunk, three])
    orch = Orchestrator(rt)
    run = orch.create_run("Make a 6-second ad.", interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.CONTINUITY)
    sb = state.storyboard
    assert sb is not None and [s.purpose.value for s in sb.scenes] == ["hook", "reveal", "product"]
    assert [s.duration_s for s in sb.scenes] == [2, 2, 2]
    assert llm.calls_for("storyboard") == 2
    repair = llm.requests[-1]
    assert "gets exactly 3 shots" in json.dumps([str(m) for m in repair.messages])


# ----------------------------------------------------------------------------- layout


def test_identity_and_the_key_moment_end_within_the_first_30_words() -> None:
    d = writer_fixture()
    text = render_ad_prompt(d, duration_s=8, aspect_ratio="9:16")
    lines = text.splitlines()
    assert lines[0].startswith("Image 1 is the product: keep its shape")
    assert lines[1] == "Key moment, shot 3, slowly over 2 s: the bottle settles into his palm."
    assert word_count(lines[0]) + word_count(lines[1]) <= 30
    assert lines[2] == "An 8-second vertical 9:16 film, 4 shots, hard cuts."


def test_shots_after_the_first_open_with_cut_to_and_still_parse() -> None:
    text = render_ad_prompt(writer_fixture(), duration_s=8, aspect_ratio="9:16")
    shot_lines = [x for x in text.splitlines() if "Shot " in x and " s): " in x]
    assert shot_lines[0].startswith("Shot 1 (0-2 s):")
    assert all(x.startswith("Cut to: Shot ") for x in shot_lines[1:])
    assert [s.number for s in parse_prompt_shots(text)] == [1, 2, 3, 4]


def test_the_fixture_is_within_every_budget(rules: CreativeRules) -> None:
    d = writer_fixture()
    assert creative_rule_problems(d, rules) == []
    assert writer_words(d) <= rules.params.max_words
    assert word_problems(d, duration_s=8, max_words=180, key_within_words=30) == []


# ----------------------------------------------------------------------------- word budgets


def test_too_many_words_are_sent_back_with_what_to_cut() -> None:
    long_action = "the host " + "slowly and very gracefully " * 40 + "turns"
    d = good().model_copy(deep=True)
    d.shots[1].action = long_action
    problems = word_problems(d, duration_s=9, max_words=180, key_within_words=30)
    assert any("the limit is 180" in p and "one beat" in p for p in problems)


def test_a_long_key_event_pushes_the_key_moment_past_30_words() -> None:
    d = good().model_copy(deep=True)
    d.shots[2].event = (
        "the host turns, smiles, lifts the bottle by its base, holds it to the light and "
        "offers it with both hands to the guest"
    )
    problems = word_problems(d, duration_s=9, max_words=180, key_within_words=30)
    assert any("within the first 30" in p and "shorten the key shot's event" in p for p in problems)


def test_per_field_budgets(rules: CreativeRules) -> None:
    d = good().model_copy(deep=True)
    d.look = "warm " * 40
    d.audio = "oud " * 30
    msgs = creative_rule_problems(d, rules)
    assert any("location + look + lighting take" in m for m in msgs)
    assert any("the audio line takes" in m for m in msgs)


# ----------------------------------------------------------------------------- physics library


@pytest.mark.parametrize(
    ("action", "event", "rule"),
    [
        ("The host lifts the bottle and offers it.", "the host lifts the bottle", "contact"),
        ("She sprays once toward her collar.", "one spray", "visibility"),
        ("The smoke condenses into the bottle.", "the smoke condenses", "transformation"),
    ],
)
def test_a_motion_without_its_physics_wording_is_sent_back(
    rules: CreativeRules, action: str, event: str, rule: str
) -> None:
    d = draft(
        [
            shot(1, 0, 2, handled=True),
            shot(2, 2, 4),
            shot(3, 4, 6.5, action=action, event=event),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ]
    )
    assert any(f"shot 3 ({rule})" in m for m in creative_rule_problems(d, rules))


@pytest.mark.parametrize(
    ("action", "event"),
    [
        ("The host lifts the bottle by its base into the guest's open palm.", "it settles"),
        ("She sprays one visible fine mist toward her collar.", "one spray"),
        ("The smoke spirals tighter, thickens, then condenses into the bottle.", "it condenses"),
    ],
)
def test_the_library_wording_passes(rules: CreativeRules, action: str, event: str) -> None:
    d = draft(
        [
            shot(1, 0, 2, handled=True),
            shot(2, 2, 4),
            shot(3, 4, 6.5, action=action, event=event),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ]
    )
    assert [m for m in creative_rule_problems(d, rules) if "shot 3 (" in m] == []


def test_a_product_in_use_must_be_big_enough_to_see(rules: CreativeRules) -> None:
    d = draft(
        [
            shot(1, 0, 2, handled=True, product_in_frame="small"),
            shot(2, 2, 4),
            shot(3, 4, 6.5),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ]
    )
    assert any(
        "a hand uses the product but it is small" in m for m in creative_rule_problems(d, rules)
    )


@pytest.mark.parametrize(
    ("field", "text", "word"),
    [("action", "She turns fast to the door.", '"fast"'), ("camera", "macro, slow push-in", "")],
)
def test_fast_and_macro_hands_are_banned(
    rules: CreativeRules, field: str, text: str, word: str
) -> None:
    over: dict[str, Any] = {field: text}
    d = draft(
        [
            shot(1, 0, 2, handled=True),
            shot(2, 2, 4, **over),
            shot(3, 4, 6.5),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ]
    )
    msgs = creative_rule_problems(d, rules)
    expect = word or '"extreme close-up"'
    assert any(f"shot 2 {field} uses {expect}" in m for m in msgs)


def test_the_director_judges_physics_and_sees_the_library(rules: CreativeRules) -> None:
    assert "physics_anchored" in rules.judge_ids
    block = rules.prompt_block()
    assert "what carries the weight" in block and "Halai product-review ad" in block
    assert "slow push-in" in block


def test_the_director_judges_whether_the_turn_reads_on_a_phone(rules: CreativeRules) -> None:
    assert "turn_readable" in rules.judge_ids
    text = next(r.text for r in rules.judge if r.id == "turn_readable")
    assert "phone screen in 2 seconds with the sound off" in text and "micro-movement" in text
    assert "turn_readable" in rules.prompt_block()  # the writer sees it too

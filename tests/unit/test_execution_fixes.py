"""ADR-0029 phase 3: what the coffee video lost, fixed before the money is spent. Object fidelity
(the defining word on every mention, generic words refused, one dallah), at most two beats in the
key shot, the device's length, both faces in the bible, the new judged rules, and the device's
wording handed to the writer and the director. Zero keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.ad import render_ad_prompt
from adsmith.domain.continuity import CharacterSpec, ContinuityBible
from adsmith.domain.creative_rules import (
    CreativeRules,
    creative_rule_problems,
    device_length_problems,
    key_beat_problems,
)
from adsmith.domain.culture_bank import CultureBank, object_problems, with_gulf_objects
from adsmith.domain.enums import Dialect, Language, Stage
from tests.factories import make_bible, make_brief
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings
from tests.unit.test_creative_rules import draft, shot

# The coffee video's shots 2 and 3, as the model received them (run_01M6TBP6H8VTXWGXFJR6XPVQ7M).
COFFEE_KEY = (
    "Dallah gripped in his left hand, the father tilts it slowly; a thin golden ribbon fills the "
    "finjan pinched in his right fingertips; he offers it; his son smiles."
)
COFFEE_HERO = (
    "The kraft pouch stands upright between two finjans; the father's and son's hands lift the "
    "cups together, gripped between thumb and fingers."
)


@pytest.fixture(scope="module")
def rules() -> CreativeRules:
    return Settings().creative_rules


@pytest.fixture(scope="module")
def bank() -> CultureBank:
    return Settings().culture_bank


def dallah_ref() -> dict[str, object]:
    return {"name": "dallah", "description": "a brass coffee pot", "role": "prop", "shots": [2],
            "transforms_into_product": False, "handled": True, "max_size": "half",
            "lookalike": False}  # fmt: skip


def coffee() -> object:
    return draft(
        [
            shot(1, 0, 1.5, handled=True),
            shot(2, 1.5, 4, action=COFFEE_KEY, event="the father pours for his son"),
            shot(3, 4, 6, action=COFFEE_HERO, event="the cups lift", product_in_frame="fills"),
        ],
        references=[dallah_ref()],
        key_shot=2,
    )


def test_the_coffee_videos_prompt_is_sent_back_for_what_the_film_lost(
    rules: CreativeRules, bank: CultureBank
) -> None:
    d = with_gulf_objects(coffee(), bank)  # type: ignore[arg-type]
    objects = " ".join(object_problems(d, bank))
    assert 'shot 3 action says "cups"' in objects and "handleless finjan" in objects
    beats = " ".join(creative_rule_problems(d, rules))
    assert "the key shot 2 has 4 beats" in beats


def test_the_defining_word_is_glued_to_every_mention_once(bank: CultureBank) -> None:
    d = with_gulf_objects(coffee(), bank)  # type: ignore[arg-type]
    assert "between two handleless finjans" in d.shots[2].action
    assert "fills the handleless finjan pinched" in d.shots[1].action
    again = with_gulf_objects(d, bank)
    assert again.shots[2].action.count("handleless") == 1  # idempotent


def test_the_dallah_is_one_of_a_kind_in_the_references(bank: CultureBank) -> None:
    d = with_gulf_objects(coffee(), bank)  # type: ignore[arg-type]
    assert d.references[0].description.endswith("; exactly one in the whole film")
    text = render_ad_prompt(d, duration_s=6, aspect_ratio="9:16", image_names=["dallah"])
    assert "exactly one in the whole film" in text
    assert bank.glosses(["dallah"]) == {"dallah": "Saudi coffee pot, only one"}


def test_a_transformation_in_steps_is_one_beat_and_try_2_has_two() -> None:
    steps = "The smoke spirals tighter, thickens, then condenses into the bottle in his palm."
    one = draft([shot(1, 0, 2), shot(2, 2, 4.5, action=steps), shot(3, 4.5, 6.5)], key_shot=2)
    assert key_beat_problems(one, 2) == []
    try2 = (
        "Close on the burner between them: the rising smoke spirals tighter and condenses in "
        "mid-air into the perfume bottle. The bottle settles into the elder's open palm."
    )
    two = draft([shot(1, 0, 2), shot(2, 2, 4.5, action=try2), shot(3, 4.5, 6.5)], key_shot=2)
    assert key_beat_problems(two, 2) == []


def test_the_device_gets_its_length() -> None:
    assert device_length_problems(coffee(), "transformation", 2.5) == []  # type: ignore[arg-type]
    short = draft([shot(1, 0, 2), shot(2, 2, 4), shot(3, 4, 6)], key_shot=2)
    assert "needs at least 2.5 s; it lasts 2 s" in device_length_problems(short, "x", 2.5)[0]


def test_the_bible_describes_the_second_face() -> None:
    father = CharacterSpec(
        description="the father, early 60s, grey beard", age_range="58-65",
        appearance="warm brown eyes, deep smile lines", hair="grey, covered",
        grooming="short grey beard", build="broad",
        wardrobe=["white Saudi thobe", "red shemagh with black agal"],
    )  # fmt: skip
    text = make_bible(second_character=father).prompt_block()
    assert "CHARACTER: young Saudi man" in text and "SECOND CHARACTER: the father" in text
    old = make_bible().model_dump(mode="json")
    old.pop("second_character")
    assert ContinuityBible.model_validate(old).second_character is None  # old bibles load


def test_the_wardrobe_lint_checks_the_second_person_too() -> None:
    kurta = CharacterSpec(
        description="an older man", age_range="60", appearance="beard", hair="grey",
        grooming="beard", build="slim", wardrobe=["a kurta with a mandarin collar"],
    )  # fmt: skip
    brief = make_brief(language=Language.AR, dialect=Dialect.SAUDI_NAJDI, raw_request="اعلان لقهوة")
    assert Settings().culture.wardrobe_problems(brief, kurta)


def test_the_new_rules_are_judged_by_id(rules: CreativeRules) -> None:
    assert {"device_lands", "setup_filmed", "no_holdup_opening"} <= set(rules.judge_ids)
    assert "two beats" in rules.params_block() or "at most 2 beats" in rules.params_block()
    assert "try_7" in rules.lessons


async def test_the_writer_and_the_director_get_the_devices_wording(tmp_path: Path) -> None:
    orch = Orchestrator(runtime(settings(tmp_path)))
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.PROPS)
    assert state.concepts is not None and state.concepts.selected is not None
    llm = orch.rt._provider_override
    for template in ("ad_prompt_writer@v6", "director@v5"):
        asked = str(next(r for r in llm.requests if r.template == template).messages)  # type: ignore[union-attr]
        assert "Wording that renders it in one shot:" in asked, template
        assert "invisible_made_visible (Make the invisible visible)" in asked

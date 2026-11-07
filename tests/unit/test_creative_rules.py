"""ADR-0026 phase 1: the creative rules as data, the v2 ad prompt (References by code), and the
multishot grammar's product and minimum-length rules. Pure, no I/O."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from adsmith.config import Settings
from adsmith.domain.ad import (
    AdPrompt,
    AdPromptDraft,
    prompt_diff_summary,
    ref_key,
    render_ad_prompt,
    shot_list,
)
from adsmith.domain.creative_rules import CreativeRules, creative_rule_problems, product_share
from adsmith.domain.formats import AdFormat, grammar_problems
from adsmith.domain.storyboard import StoryboardDraft
from adsmith.providers.llm.scripted import FIXTURES_DIR

BURNER = {
    "name": "incense burner",
    "description": "A wide, low, carved dark-wood box with a small pierced brass dome lid.",
    "role": "prop",
    "shots": [1, 2, 3],
    "transforms_into_product": True,
    "handled": True,
    "max_size": "half",
    "lookalike": False,
}


@pytest.fixture(scope="module")
def rules() -> CreativeRules:
    return Settings().creative_rules


def shot(n: int, start: float, end: float, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": n,
        "start_s": start,
        "end_s": end,
        "action": f"beat {n}.",
        "event": "a hand lifts it by its base",
        "camera": "close-up, slow push-in",
        "sound": "room tone",
        "shows_product": True,
        "product_in_frame": "half",
        "handled": False,
    }
    return base | over


def draft(shots: list[dict[str, Any]], **over: Any) -> AdPromptDraft:
    base: dict[str, Any] = {
        "location": "a traditional Saudi majlis at night",
        "look": "warm amber grade, fine grain",
        "lighting": "brass lanterns from camera-left",
        "references": [],
        "shots": shots,
        "key_shot": 3,
        "audio": "charcoal crackle, then an oud phrase with a glass settle on the turn",
    }
    return AdPromptDraft.model_validate(base | over)


def good() -> AdPromptDraft:
    return draft(
        [
            shot(1, 0, 2, handled=True),
            shot(2, 2, 4),
            shot(3, 4, 6.5),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ]
    )


def test_a_prompt_that_follows_every_rule_has_no_problems(rules: CreativeRules) -> None:
    assert creative_rule_problems(good(), rules) == []


def test_the_writer_fixture_follows_every_rule(rules: CreativeRules) -> None:
    raw = json.loads((FIXTURES_DIR / "multishot_en" / "ad_prompt_writer.json").read_text())
    assert creative_rule_problems(AdPromptDraft.model_validate(raw[0]["output"]), rules) == []


@pytest.mark.parametrize(
    ("field", "text", "replacement"),
    [
        ("action", "A small ember glows on the floor.", "a thin thread of incense smoke"),
        ("action", "Match cut to the bottle.", "hard cut to"),
        ("action", "The smoke becomes the bottle.", "condenses in mid-air into"),
        ("action", "The bottle floats above the palm.", "rests in the open palm"),
        ("event", "a hint of smoke", "name the visible object or motion"),
    ],
)
def test_a_banned_word_is_sent_back_with_its_replacement(
    rules: CreativeRules, field: str, text: str, replacement: str
) -> None:
    d = good()
    d.shots[1] = d.shots[1].model_copy(update={field: text})
    problems = creative_rule_problems(d, rules)
    assert any("shot 2" in p and replacement in p for p in problems), problems


def test_static_is_allowed_only_on_the_hero_camera(rules: CreativeRules) -> None:
    d = good()
    d.shots[3] = d.shots[3].model_copy(update={"camera": "medium close-up, static"})
    assert creative_rule_problems(d, rules) == []
    d.shots[0] = d.shots[0].model_copy(update={"camera": "close-up, locked off"})
    assert any("shot 1 camera" in p for p in creative_rule_problems(d, rules))
    # The word is only banned in the camera text.
    d = good()
    d.shots[0] = d.shots[0].model_copy(update={"action": "A static ribbon of smoke rises."})
    assert creative_rule_problems(d, rules) == []


def test_the_key_shot_and_the_hero_need_two_seconds(rules: CreativeRules) -> None:
    d = draft(
        [
            shot(1, 0, 2.5, handled=True),
            shot(2, 2.5, 5),
            shot(3, 5, 6.5),
            shot(4, 6.5, 8, product_in_frame="fills"),
        ]
    )
    text = "; ".join(creative_rule_problems(d, rules))
    assert "the key shot 3 lasts 1.5 s" in text and "the hero shot lasts 1.5 s" in text


def test_the_hero_shows_the_product_at_half_the_frame_or_more(rules: CreativeRules) -> None:
    d = good()
    d.shots[3] = d.shots[3].model_copy(update={"product_in_frame": "small"})
    assert any("product hero" in p for p in creative_rule_problems(d, rules))


def test_product_screen_time_counts_a_transforming_prop(rules: CreativeRules) -> None:
    """The try-2 shape: the burner stands in for the product until it transforms in shot 3."""
    hidden = {"shows_product": False, "product_in_frame": "absent"}
    shots = [
        shot(1, 0, 2, **hidden),
        shot(2, 2, 4, **hidden),
        shot(3, 4, 6.5, handled=True),
        shot(4, 6.5, 9, product_in_frame="fills"),
    ]
    without = draft(shots)
    assert any("on screen in shots [3, 4]" in p for p in creative_rule_problems(without, rules))
    with_burner = draft(shots, references=[BURNER])
    assert product_share(with_burner) == ([1, 2, 3, 4], 1.0)
    assert creative_rule_problems(with_burner, rules) == []


def test_the_product_itself_and_a_hand_on_it_are_required(rules: CreativeRules) -> None:
    hidden = {"shows_product": False, "product_in_frame": "absent"}
    d = draft(
        [
            shot(1, 0, 2, **hidden),
            shot(2, 2, 4, **hidden),
            shot(3, 4, 6.5, **hidden),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ],
        references=[BURNER],
    )
    text = "; ".join(creative_rule_problems(d, rules))
    assert "the product itself is visible in 1 shot(s)" in text
    assert "hold or use the product in 0 shot(s)" in text


def test_flags_must_agree(rules: CreativeRules) -> None:
    d = good()
    d.shots[1] = d.shots[1].model_copy(update={"product_in_frame": "absent"})
    d.shots[2] = d.shots[2].model_copy(
        update={"shows_product": False, "product_in_frame": "absent", "handled": True}
    )
    text = "; ".join(creative_rule_problems(d, rules))
    assert "shot 2: product_in_frame must be 'absent'" in text
    assert "shot 3 is handled but the product is not visible" in text


def test_shots_never_name_an_image_number(rules: CreativeRules) -> None:
    d = good()
    d.shots[0] = d.shots[0].model_copy(update={"action": "He lifts the burner from Image 2."})
    assert any("names an image number" in p for p in creative_rule_problems(d, rules))


def test_references_are_checked(rules: CreativeRules) -> None:
    loc = {**BURNER, "name": "majlis", "role": "location", "transforms_into_product": True}
    d = draft(good().model_dump()["shots"], references=[{**BURNER, "shots": [1, 5]}, loc])
    text = "; ".join(creative_rule_problems(d, rules))
    assert "lists shots [5]" in text and "only a prop can transform" in text


# ----------------------------------------------------------------------------- rendering


def test_the_references_block_numbers_only_the_images_that_exist() -> None:
    hidden = {"shows_product": False, "product_in_frame": "absent"}
    d = draft(
        [
            shot(1, 0, 2, **hidden),
            shot(2, 2, 4, **hidden),
            shot(3, 4, 6.5, handled=True),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ],
        references=[BURNER],
    )
    no_image = render_ad_prompt(d, duration_s=9, aspect_ratio="9:16")
    assert no_image.startswith("Image 1 is the product")
    assert "References: The product appears in shots 3-4." in no_image
    assert "The incense burner: a wide, low, carved dark-wood box" in no_image
    assert "Image 2" not in no_image
    assert "transforms into the product in shot 3; before that it is never the product" in (
        no_image
    )
    with_image = render_ad_prompt(
        d, duration_s=9, aspect_ratio="9:16", image_names=["Mabkhara.png", "Incense_Burner.png"]
    )
    # The burner is the second reference image; the unnamed first one is still declared.
    assert "Image 3 is the incense burner: a wide" in with_image
    assert "Image 2 is the mabkhara, a prop; it is never the product." in with_image
    lines = with_image.splitlines()
    assert (
        lines[0] == "Image 1 is the product: keep its shape, colours, cap and materials identical."
    )
    assert lines[1] == "Key moment, shot 3, slowly over 2.5 s: a hand lifts it by its base."
    assert lines[2] == "A 9-second vertical 9:16 film, 4 shots, hard cuts."
    assert lines[3].startswith("References: ") and lines[4].startswith("Look: A traditional")
    assert lines[5].startswith("Shot 1 (0-2 s): Beat 1. Camera: close-up, slow push-in.")
    assert lines[6].startswith("Cut to: Shot 2 (2-4 s):")
    assert "Product fills the frame." in lines[8]
    assert lines[-1].endswith("No voice, no dialogue.")


def test_names_and_shot_lists() -> None:
    assert ref_key("The_Incense-Burner.PNG") == "incense burner" == ref_key("an incense burner")
    assert shot_list([4, 1, 2, 3]) == "1-4" and shot_list([1, 3, 4]) == "1, 3-4"


def test_the_human_prompt_diff_names_the_references_section() -> None:
    old = "Header.\nLook: a.\nShot 1 (0-9 s): x\nAudio: y"
    new = "Header.\nReferences: Image 1 is the product.\nLook: a.\nShot 1 (0-9 s): x\nAudio: y"
    assert "added References" in prompt_diff_summary(old, new)


def test_a_prompt_stored_before_v2_still_loads() -> None:
    """Try 1 and try 2 were stored with per-shot lighting and none of the v2 fields."""
    v1 = {
        "look": "warm",
        "shots": [
            {
                "number": i,
                "start_s": 2.0 * (i - 1),
                "end_s": 2.0 * i,
                "action": "a",
                "camera": "c",
                "lighting": "l",
                "sound": "s",
            }
            for i in (1, 2, 3, 4)
        ],
        "audio": "oud",
        "duration_s": 8,
        "aspect_ratio": "9:16",
        "text": "An 8-second ...",
        "template_version": "ad_prompt_writer@v1",
        "bible_version": 1,
    }
    p = AdPrompt.model_validate(v1)
    assert p.shots[0].lighting == "l" and p.references == [] and p.key_shot is None


def test_a_stored_v2_prompt_converts_back_to_its_draft() -> None:
    d = good()
    p = AdPrompt.from_draft(
        d, duration_s=9, aspect_ratio="9:16", text="t", template_version="x@v2", bible_version=1
    )
    assert p.to_draft() == d


# ----------------------------------------------------------------------------- grammar


def _storyboard() -> StoryboardDraft:
    raw = json.loads((FIXTURES_DIR / "multishot_en" / "storyboard.json").read_text())
    return StoryboardDraft.model_validate(raw[0]["output"])


def test_the_multishot_fixture_storyboard_follows_the_grammar(settings: Settings) -> None:
    g = settings.formats.grammar(AdFormat.MULTISHOT)
    assert grammar_problems(_storyboard().scenes, g) == []


def test_multishot_needs_the_product_in_three_shots_and_two_second_turns(
    settings: Settings,
) -> None:
    g = settings.formats.grammar(AdFormat.MULTISHOT)
    raw = copy.deepcopy(_storyboard().model_dump(mode="json"))
    raw["scenes"][0]["shows_product"] = False
    raw["scenes"][1]["duration_s"] = 3.0  # hook 2, build 3, reveal 1.5, product 1.5 = 8
    raw["scenes"][2]["duration_s"] = 1.5
    raw["scenes"][3]["duration_s"] = 1.5
    text = "; ".join(grammar_problems(StoryboardDraft.model_validate(raw).scenes, g))
    assert "the product is visible in shots [3, 4]" in text
    assert "scene 3 (reveal) lasts 1.5 s" in text and "scene 4 (product) lasts 1.5 s" in text


# ----------------------------------------------------------------------------- 2026-11-07 fixes


def test_emotion_on_turn_is_a_judged_rule_with_this_runs_lesson(rules: CreativeRules) -> None:
    assert "emotion_on_turn" in rules.judge_ids
    rule = next(r for r in rules.judge if r.id == "emotion_on_turn")
    assert "shows the feeling" in rule.text and "hands and a wrist alone" in rule.text
    assert "hand and a wrist" in rules.lessons["try_3"]
    assert "emotion_on_turn" in rules.prompt_block()


def test_the_action_never_states_the_products_size(rules: CreativeRules) -> None:
    d = good()
    d.shots[0] = d.shots[0].model_copy(
        update={"action": "The bottle stands on the console, filling about half the frame."}
    )
    problems = creative_rule_problems(d, rules)
    assert any("shot 1 action states a size" in p and "Framing line" in p for p in problems)


@pytest.mark.parametrize("words", ["at the right edge", "in soft focus", "in the corner"])
def test_a_peripheral_product_cannot_fill_half_the_frame(rules: CreativeRules, words: str) -> None:
    d = good()
    d.shots[0] = d.shots[0].model_copy(update={"action": f"The bottle stands {words}."})
    problems = creative_rule_problems(d, rules)
    assert any("shot 1 puts the product" in p and "'half'" in p for p in problems)
    d.shots[0] = d.shots[0].model_copy(update={"product_in_frame": "small"})
    assert not any("puts the product" in p for p in creative_rule_problems(d, rules))


@pytest.mark.parametrize(
    ("field", "text", "message"),
    [
        ("audio", "warm music swells and settles.}", 'audio contains a stray "}"'),
        ("look", "warm grade <look>", 'look contains a stray "<"'),
        ("lighting", "warm key (from the left", 'lighting has unbalanced "()"'),
    ],
)
def test_stray_characters_go_back_to_the_model(
    rules: CreativeRules, field: str, text: str, message: str
) -> None:
    d = good().model_copy(update={field: text})
    assert any(message in p for p in creative_rule_problems(d, rules))


def test_a_prop_earns_an_image_only_when_it_can_be_got_wrong() -> None:
    from adsmith.domain.ad import ReferenceDraft, earns_image  # noqa: PLC0415

    base: dict[str, Any] = {
        "name": "dallah", "description": "a brass pot", "role": "prop", "shots": [2],
        "transforms_into_product": False, "handled": False, "max_size": "small",
        "lookalike": False,
    }  # fmt: skip
    assert not earns_image(ReferenceDraft.model_validate(base))  # this run's dallah
    for change in (
        {"handled": True}, {"shots": [1, 2]}, {"max_size": "half"}, {"max_size": "fills"},
        {"lookalike": True}, {"transforms_into_product": True},
    ):  # fmt: skip
        assert earns_image(ReferenceDraft.model_validate(base | change)), change
    assert not earns_image(ReferenceDraft.model_validate(base | {"role": "location"}))


def test_a_text_only_prop_has_no_references_line() -> None:
    hidden = {"shows_product": False, "product_in_frame": "absent"}
    pot = {**BURNER, "name": "dallah", "transforms_into_product": False, "shots": [2]}
    d = draft(
        [
            shot(1, 0, 2, handled=True),
            shot(2, 2, 4, **hidden),
            shot(3, 4, 6.5),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ],
        references=[pot],
    )
    text = render_ad_prompt(d, duration_s=9, aspect_ratio="9:16")
    assert "dallah" not in next(x for x in text.splitlines() if x.startswith("References:"))
    assert "Image 2 is the dallah" in render_ad_prompt(
        d, duration_s=9, aspect_ratio="9:16", image_names=["dallah"]
    )


def test_emotion_on_turn_needs_the_face_and_names_the_real_cliche(rules: CreativeRules) -> None:
    rule = next(r for r in rules.judge if r.id == "emotion_on_turn")
    assert "at least partly visible (profile or three-quarter is fine)" in rule.text
    assert "Face turned away, seen from behind" in rule.text
    assert "eyes closed sniffing the wrist" in rule.text
    # ADR-0029, the coffee video: the face is the one whose gesture the insight is about
    assert "the father who honours his son, not only the son" in rule.text


def test_a_host_never_neglects_a_guest(rules: CreativeRules) -> None:
    rule = next(r for r in rules.judge if r.id == "culturally_gracious")
    assert "a ringing bell is answered" in rule.text and "an elder" in rule.text
    assert "she does not turn to the door" in rules.lessons["try_4"]
    assert "culturally_gracious" in rules.prompt_block()


@pytest.mark.parametrize(
    ("product", "clause"),
    [
        ("Women's perfume: a rounded pink glass bottle", "cap and materials identical"),
        ("Specialty coffee: an upright kraft pouch with a zip seal", "seal and emblem identical"),
        ("Premium dates: a dark wooden box with a velvet lining", "wood, lining and emblem"),
        ("A ceramic honey jar", "colours, materials and emblem identical"),
    ],
)
def test_the_identity_clause_follows_the_product_type(product: str, clause: str) -> None:
    from adsmith.domain.ad import AdPrompt, identity_line  # noqa: PLC0415

    ident = identity_line(product)
    assert clause in ident
    d = good()
    text = render_ad_prompt(d, duration_s=9, aspect_ratio="9:16", identity=ident)
    assert f"Image 1 is the product: {ident}." in text
    # A stored prompt keeps its clause, so a later re-render (the props stage) keeps it too.
    stored = AdPrompt.from_draft(
        d, duration_s=9, aspect_ratio="9:16", text=text, template_version="t@v3",
        bible_version=1, identity=ident,
    )  # fmt: skip
    assert f"Image 1 is the product: {ident}." in render_ad_prompt(
        stored, duration_s=9, aspect_ratio="9:16"
    )


def test_the_product_must_act_in_the_story(rules: CreativeRules) -> None:
    rule = next(r for r in rules.judge if r.id == "product_active")
    assert "only stands on a table in every shot fails" in rule.text
    assert "turning a pouch toward a cup" in rule.text
    assert "only stood on the tray" in rules.lessons["try_5"]


# ----------------------------------------------------------------------------- fitting the limit


def _stored(d: AdPromptDraft) -> AdPrompt:
    return AdPrompt.from_draft(
        d, duration_s=9, aspect_ratio="9:16", text="t", template_version="t@v3", bible_version=1
    )


def _with_props() -> AdPrompt:
    desc = (
        "a Saudi dallah: brass body pinched at the waist above a wide base, a tall crescent "
        "beak spout, a domed lid with a pointed finial"
    )
    pot = {**BURNER, "name": "dallah", "transforms_into_product": False, "description": desc,
           "shots": [1, 2, 3, 4]}  # fmt: skip
    tray = {**pot, "name": "tray", "description": "a round hammered brass tray with a low rim"}
    cup = {**pot, "name": "finjan", "description": "a small handleless white porcelain cup"}
    return _stored(draft(good().model_dump()["shots"], references=[pot, tray, cup]))


def test_a_prompt_within_the_limit_keeps_its_full_reference_lines() -> None:
    from adsmith.domain.ad import fit_references  # noqa: PLC0415

    p = _with_props()
    refs = [("dallah", "library"), ("tray", "generated"), ("finjan", "library")]
    text, dropped, compact = fit_references(
        p, refs, duration_s=9, aspect_ratio="9:16", max_chars=5000, glosses={}
    )
    assert not compact and dropped == [] and "Image 2 is the dallah: a Saudi dallah" in text


def test_over_the_limit_lines_are_shortened_before_anything_is_dropped() -> None:
    from adsmith.domain.ad import fit_references  # noqa: PLC0415

    p = _with_props()
    refs = [("dallah", "library"), ("tray", "generated"), ("finjan", "library")]
    glosses = {"dallah": "Saudi coffee pot", "finjan": "Arabic coffee cup"}
    full = render_ad_prompt(p, duration_s=9, aspect_ratio="9:16", image_names=[r[0] for r in refs])
    text, dropped, compact = fit_references(
        p, refs, duration_s=9, aspect_ratio="9:16", max_chars=len(full) - 1, glosses=glosses
    )
    assert compact and dropped == []
    line = next(x for x in text.splitlines() if x.startswith("References:"))
    assert "Image 2: the dallah (Saudi coffee pot), shots 1-4, never the product." in line
    assert "Image 3: the tray, " in line and "pinched at the waist" not in line


def test_generated_props_are_dropped_before_library_props_and_users_never() -> None:
    from adsmith.domain.ad import fit_references  # noqa: PLC0415

    p = _with_props()
    refs = [("dallah", "user"), ("tray", "generated"), ("finjan", "library")]
    glosses = {"finjan": "Arabic coffee cup"}
    only_user = render_ad_prompt(
        p, duration_s=9, aspect_ratio="9:16", image_names=["dallah"], glosses=glosses
    )
    text, dropped, _ = fit_references(
        p, refs, duration_s=9, aspect_ratio="9:16", max_chars=len(only_user), glosses=glosses
    )
    assert dropped == [1, 2]  # the generated tray first, then the library finjan; never the user's
    assert "Image 2: the dallah, " in text and "finjan (Arabic" not in text


def test_a_writer_typed_no_voice_line_is_not_repeated() -> None:
    from adsmith.domain.ad import strip_no_voice  # noqa: PLC0415

    d = good().model_copy(update={"audio": "warm oud resolves. No voice, no dialogue."})
    text = render_ad_prompt(d, duration_s=9, aspect_ratio="9:16")
    assert text.endswith("Audio: Warm oud resolves. No voice, no dialogue.")
    assert text.count("No voice") == 1
    assert strip_no_voice("Quiet tone. No voice-over, no dialogue.") == "Quiet tone."


def test_modesty_is_dress_and_faces_may_be_shown(rules: CreativeRules) -> None:
    from adsmith.agent.prompts import PromptLibrary  # noqa: PLC0415

    lib = PromptLibrary(Settings().prompts_dir)
    cont = lib.get("continuity").system
    assert "a woman's face in a headscarf may be fully visible" in cont
    assert 'no "eyes never shown"' in cont
    assert "never crop a face for consistency" in lib.get("ideate").system
    assert "her eyes are never shown" in rules.lessons["try_6"]


def test_the_coffee_video_lesson_names_what_the_film_lost(rules: CreativeRules) -> None:
    lesson = rules.lessons["try_7"]
    for fact in ("face never showed", "the sip was cut", "handles", "second time"):
        assert fact in lesson
    assert "try_7" in rules.lessons_block()


# ------------------------------------------------------------------------ the References budget


def test_planned_images_are_the_runs_images_then_props_that_earn_one() -> None:
    from adsmith.domain.ad import planned_images  # noqa: PLC0415

    p = _with_props()  # dallah, tray, finjan: all handled, in 4 shots
    assert planned_images(p) == ["dallah", "tray", "finjan"]
    assert planned_images(p, ["Tray.png"]) == ["Tray.png", "dallah", "finjan"]


def test_the_length_check_counts_the_references_lines_before_any_image_exists() -> None:
    from adsmith.domain.ad import ad_prompt_problems, budgeted_length  # noqa: PLC0415

    d = _with_props().to_draft()
    glosses = {"dallah": "Saudi coffee pot", "finjan": "Arabic coffee cup"}
    bare = len(render_ad_prompt(d, duration_s=9, aspect_ratio="9:16"))  # no images yet
    total, refs, names = budgeted_length(d, duration_s=9, glosses=glosses)
    assert names == ["dallah", "tray", "finjan"] and total > bare
    problems = ad_prompt_problems(d, None, duration_s=9, max_chars=total - 1, glosses=glosses)
    text = "; ".join(problems)
    assert f"is {total} characters; the limit is {total - 1}" in text
    assert f"The References line takes {refs} of them" in text
    assert "never list props in the look" in text
    assert ad_prompt_problems(d, None, duration_s=9, max_chars=total, glosses=glosses) == []


def test_the_writer_and_director_are_told_the_budget() -> None:
    from adsmith.agent.prompts import PromptLibrary  # noqa: PLC0415
    from adsmith.domain.ad import references_budget_note  # noqa: PLC0415

    note = references_budget_note(existing=[], glosses={}, max_chars=2500, identity=None)
    assert "takes about" in note and "more for each prop that will get a reference image" in note
    lib = PromptLibrary(Settings().prompts_dir)
    writer, director = lib.get("ad_prompt_writer"), lib.get("director")
    assert "never the props" in writer.system
    assert "references_budget" in writer.variables and "references_budget" in director.variables

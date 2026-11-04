"""Fail-safe scene flags: bias toward true, never downgrade, explain every override."""

from __future__ import annotations

from typing import Any

from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from adsmith.domain.scene import SceneDraft
from adsmith.domain.scene_flags import detect_mentions, enforce_scene_flags
from tests.factories import make_bible, make_scene, make_shot


def test_schema_defaults_are_true() -> None:
    draft = SceneDraft.model_validate(
        make_scene().model_dump(
            include={"purpose", "description", "duration_s", "shot"}, mode="json"
        )
    )
    assert draft.shows_character and draft.shows_product


def _neutral(**over: Any) -> dict[str, Any]:
    return {
        "description": "Gold liquid swirls in darkness.",
        "shot": make_shot(subject_action="smoke drifts upward", composition="centered"),
        "shows_character": False,
        "shows_product": False,
    } | over


def test_nothing_mentioned_keeps_flags() -> None:
    s = make_scene(**_neutral())
    assert enforce_scene_flags([s]) == []
    assert not s.shows_character and not s.shows_product


def test_english_mentions_upgrade_flags() -> None:
    s = make_scene(
        **_neutral(shot=make_shot(subject_action="his hand sets the bottle down", composition="c"))
    )
    overrides = enforce_scene_flags([s])
    assert {o.flag for o in overrides} == {"shows_character", "shows_product"}
    assert s.shows_character and s.shows_product


def test_arabic_mentions_with_article_and_clitics() -> None:
    s = make_scene(**_neutral(description="الشاب يرش العطر ثم يضع الزجاجة"))
    hits = detect_mentions(s)
    assert "شاب" in hits["shows_character"]
    assert {"عطر", "زجاجه"} <= set(hits["shows_product"])


def test_arabic_short_term_requires_exact_match() -> None:
    s = make_scene(**_neutral(description="الهواء بارد والدخان يتصاعد"))  # "the air is cold"
    assert detect_mentions(s)["shows_character"] == []


def test_bible_terms_catch_what_generic_terms_miss() -> None:
    bible = make_bible()  # wardrobe: black tailored shirt, silver watch; product NOIR
    s = make_scene(
        **_neutral(
            description="Close on a silver watch catching neon.",
            shot=make_shot(subject_action="NOIR glints on the table", composition="c"),
        )
    )
    assert enforce_scene_flags([s]) == []  # generic terms alone see nothing
    overrides = enforce_scene_flags([s], bible)
    assert {o.flag for o in overrides} == {"shows_character", "shows_product"}


def test_never_downgrades() -> None:
    s = make_scene(**_neutral(shows_character=True, shows_product=True))
    assert enforce_scene_flags([s]) == []
    assert s.shows_character and s.shows_product


def test_override_becomes_decision_event() -> None:
    s = make_scene(**_neutral(description="The man turns."))
    (override,) = enforce_scene_flags([s])
    event = override.as_event("run_x", Stage.STORYBOARD)
    assert event.type is EventType.DECISION and event.scene_id == s.id
    assert "shows_character=true" in event.summary and "man" in event.summary
    assert event.payload["override"] is True

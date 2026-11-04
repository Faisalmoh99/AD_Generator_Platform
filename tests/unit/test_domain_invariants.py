"""Object-local and cross-aggregate invariants, and JSON round-trips."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from adsmith.domain.brief import Brief, BriefDraft
from adsmith.domain.concept import ConceptSet, ConceptSetDraft
from adsmith.domain.edit import ScenePatchDraft, SceneRef, ShotSpecPatch
from adsmith.domain.enums import BibleSection, Dialect, FailureKind, Language, Stage, TimeOfDay
from adsmith.domain.generation import GenerationAttempt
from adsmith.domain.run import RunError, RunState
from adsmith.domain.storyboard import Storyboard, StoryboardDraft
from tests.factories import (
    make_bible,
    make_brief,
    make_concept_set,
    make_full_run,
    make_prompt,
    make_run,
    make_scene,
    make_storyboard,
    sha,
)
from tests.fixtures.concept_texts import EN_NIGHT_DRIVE_LIGHT_PARAPHRASE

# ---------------------------------------------------------------------------- brief


def test_english_brief_cannot_carry_dialect() -> None:
    with pytest.raises(ValidationError, match="dialect must be 'none'"):
        make_brief(dialect=Dialect.SAUDI_NAJDI)


def test_arabic_brief_needs_dialect() -> None:
    with pytest.raises(ValidationError, match="need a dialect"):
        make_brief(language=Language.AR, dialect=Dialect.NONE)


def test_brief_from_draft_applies_default_aspect() -> None:
    draft = BriefDraft.model_validate(
        make_brief().model_dump(exclude={"id", "raw_request", "aspect_ratio"})
    )
    brief = Brief.from_draft(draft, raw_request="x", default_aspect="9:16")
    assert brief.aspect_ratio == "9:16"
    draft2 = draft.model_copy(update={"aspect_ratio": "16:9"})
    assert Brief.from_draft(draft2, raw_request="x", default_aspect="9:16").aspect_ratio == "16:9"


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Brief.model_validate(make_brief().model_dump() | {"surprise": 1})


# ---------------------------------------------------------------------------- concepts


def test_concept_set_requires_exactly_three() -> None:
    cs = make_concept_set()
    with pytest.raises(ValidationError):
        ConceptSet(concepts=cs.concepts[:2])


def test_near_duplicate_concepts_rejected_with_actionable_message() -> None:
    cs = make_concept_set()
    draft = cs.model_dump()["concepts"]
    for c in draft:
        c.pop("id")
    # Turn concept 3 into a light paraphrase of concept 1.
    sentences = [s.strip() for s in EN_NIGHT_DRIVE_LIGHT_PARAPHRASE.split(".") if s.strip()]
    draft[2] |= {
        "title": sentences[0],
        "logline": sentences[1],
        "visual_hook": sentences[2],
        "emotional_driver": sentences[3],
        "narrative_arc": sentences[4:7],
    }
    with pytest.raises(ValidationError) as exc:
        ConceptSetDraft.model_validate({"concepts": draft})
    msg = str(exc.value)
    assert "concepts 1 and 3" in msg and "not distinct" in msg


def test_selection_must_reference_a_concept() -> None:
    cs = make_concept_set()
    with pytest.raises(ValueError, match="unknown concept"):
        cs.select("cpt_nope", "user", None)
    data = cs.model_dump() | {"selected_id": "cpt_nope", "selected_by": "user"}
    with pytest.raises(ValidationError):
        ConceptSet.model_validate(data)


def test_selected_id_and_by_go_together() -> None:
    data = make_concept_set().model_dump() | {"selected_id": "cpt_a"}
    with pytest.raises(ValidationError, match="together"):
        ConceptSet.model_validate(data)


# ---------------------------------------------------------------------------- storyboard


def test_storyboard_duration_must_sum_to_target() -> None:
    make_storyboard((3.75, 3.75, 3.75, 3.75))
    make_storyboard((3.75, 3.75, 3.75, 4.0))  # 15.25 is inside ±0.25
    with pytest.raises(ValidationError, match=r"sum to 14s .* target is 15s"):
        make_storyboard((3.5, 3.5, 3.5, 3.5))


def test_storyboard_from_draft_assigns_orders_and_reports_sum() -> None:
    drafts = [
        make_scene(i).model_dump(
            include={"purpose", "description", "duration_s", "shot", "shows_character"}
        )
        | {"shows_product": False}
        for i in range(1, 4)
    ]
    sb = Storyboard.from_draft(
        StoryboardDraft.model_validate({"scenes": drafts}), concept_id="c", target_duration_s=11.25
    )
    assert [s.order for s in sb.scenes] == [1, 2, 3]
    with pytest.raises(ValidationError, match="Adjust durations"):
        Storyboard.from_draft(
            StoryboardDraft.model_validate({"scenes": drafts}), concept_id="c", target_duration_s=15
        )


def test_storyboard_orders_must_be_contiguous() -> None:
    sb = make_storyboard()
    sb.scenes[1].order = 5
    with pytest.raises(ValueError, match=r"1\.\.n"):
        sb.check_invariants()


def test_neighbors() -> None:
    sb = make_storyboard()
    first, second, last = sb.scenes[0], sb.scenes[1], sb.scenes[-1]
    assert sb.neighbors(first.id) == (None, second)
    assert sb.neighbors(last.id)[1] is None


# ---------------------------------------------------------------------------- bible


def test_palette_must_be_hex() -> None:
    with pytest.raises(ValidationError):
        make_bible(
            color_grade={"palette": ["black", "#C9A227"], "contrast": "x", "saturation": "y"}
        )


def test_changed_sections_detects_only_what_changed() -> None:
    a = make_bible()
    b = a.model_copy(deep=True)
    assert b.character is not None
    b.character.wardrobe = ["white thobe"]
    assert a.changed_sections(b) == frozenset({BibleSection.CHARACTER})


def test_prompt_block_includes_only_requested_sections() -> None:
    bible = make_bible()
    block = bible.prompt_block(frozenset({BibleSection.PRODUCT, BibleSection.COLOR_GRADE}))
    assert "PRODUCT:" in block and "COLOR GRADE:" in block
    assert "CHARACTER:" not in block
    assert "AVOID: competitor brands" in block  # forbidden list always applies


# ---------------------------------------------------------------------------- edits


def test_patch_must_change_something_or_ask() -> None:
    with pytest.raises(ValidationError, match="changes nothing"):
        ScenePatchDraft(
            language=Language.EN, targets=[SceneRef(kind="ordinal", value="3")], confidence=0.9
        )
    ScenePatchDraft(language=Language.EN, confidence=0.3, clarification_needed="Which scene?")


def test_scene_change_needs_target() -> None:
    with pytest.raises(ValidationError, match="target"):
        ScenePatchDraft(
            language=Language.EN, shot=ShotSpecPatch(time_of_day=TimeOfDay.NIGHT), confidence=0.9
        )


def test_bible_change_paths_validated() -> None:
    with pytest.raises(ValidationError, match="unknown bible sections"):
        ScenePatchDraft(language=Language.AR, bible_changes={"shoes.color": "red"}, confidence=0.9)
    p = ScenePatchDraft(
        language=Language.AR, bible_changes={"character.wardrobe": "black thobe"}, confidence=0.9
    )
    assert p.changed_bible_sections() == frozenset({BibleSection.CHARACTER})


# ---------------------------------------------------------------------------- attempts


def test_failed_attempt_needs_kind() -> None:
    base: dict[str, Any] = {
        "scene_id": "s",
        "scene_version": 1,
        "phase": "keyframe",
        "attempt_no": 1,
        "provider": "mock",
        "model": "m",
        "request_hash": sha("r"),
        "prompt_used": "p",
    }
    GenerationAttempt.model_validate(base)  # in-flight attempt is fine
    with pytest.raises(ValidationError, match="failure_kind"):
        GenerationAttempt.model_validate(
            base | {"outcome": "failed", "finished_at": "2026-11-04T00:00:00Z"}
        )


# ---------------------------------------------------------------------------- run state


@pytest.mark.parametrize(
    ("stage", "missing"),
    [
        (Stage.IDEATE, "brief"),
        (Stage.SELECT, "concepts"),
        (Stage.STORYBOARD, "concepts"),
        (Stage.PROMPT, "brief"),
        (Stage.DONE, "final video"),
        (Stage.ERRORED, "error record"),
    ],
)
def test_stage_prerequisites(stage: Stage, missing: str) -> None:
    with pytest.raises(ValidationError, match=missing):
        make_run(stage=stage)


def test_storyboard_target_must_match_brief_duration() -> None:
    """Cross-aggregate rule: lives on RunState, the only object that sees both."""
    run = make_full_run()
    run.brief = make_brief(duration_s=30.0)
    with pytest.raises(ValueError, match=r"brief 30\.0s"):
        run.check_invariants()


def test_storyboard_must_belong_to_selected_concept() -> None:
    run = make_full_run()
    assert run.concepts is not None
    run.concepts.select("cpt_b", "user", None)
    with pytest.raises(ValueError, match="selected concept"):
        run.check_invariants()


def test_prompt_cannot_reference_future_bible() -> None:
    run = make_full_run()
    assert run.storyboard is not None
    run.storyboard.scenes[0].prompt = make_prompt(bible_version=2)
    with pytest.raises(ValueError, match="future bible"):
        run.check_invariants()


def test_errored_run_with_record_is_valid() -> None:
    make_run(
        stage=Stage.ERRORED,
        error=RunError(stage=Stage.UNDERSTAND, kind=FailureKind.INVALID_OUTPUT, reason_user="x"),
    )


def test_full_run_json_round_trip_is_lossless() -> None:
    run = make_full_run()
    again = RunState.model_validate_json(run.model_dump_json())
    assert again == run
    assert again.storyboard is not None and run.storyboard is not None
    assert [s.content_hash() for s in again.storyboard.scenes] == [
        s.content_hash() for s in run.storyboard.scenes
    ]

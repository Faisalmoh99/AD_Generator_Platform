"""Scene sub-state machine: legal paths, illegal edges, invalidation depth."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adsmith.domain.enums import FailureKind, ScenePhase, SceneStatus, StaleFrom
from adsmith.domain.scene import SCENE_TRANSITIONS, IllegalTransitionError, Scene, SceneFailure
from tests.factories import (
    make_image,
    make_issue,
    make_prompt,
    make_scene,
    make_verdict,
    make_video,
)


def status(s: Scene) -> SceneStatus:
    """Read status through a call so mypy doesn't narrow it across mutating methods."""
    return s.status


def ready_scene() -> Scene:
    s = make_scene()
    s.set_prompt(make_prompt())
    return s


def approved_scene() -> Scene:
    s = ready_scene()
    s.start()
    s.complete_keyframe(make_image())
    s.start()
    s.complete_clip(make_video())
    s.approve(make_verdict())
    return s


def test_happy_path() -> None:
    s = ready_scene()
    assert (s.status, s.phase) == (SceneStatus.PENDING, ScenePhase.KEYFRAME)
    s.start()
    assert status(s) is SceneStatus.GENERATING
    s.complete_keyframe(make_image())
    assert (s.status, s.phase) == (SceneStatus.PENDING, ScenePhase.CLIP)
    s.start()
    s.complete_clip(make_video())
    assert status(s) is SceneStatus.QA_REVIEW
    s.approve(make_verdict())
    assert status(s) is SceneStatus.APPROVED


def test_cannot_start_without_prompt() -> None:
    with pytest.raises(IllegalTransitionError, match="without a prompt"):
        make_scene().start()


@pytest.mark.parametrize(
    ("source", "target"),
    [(src, dst) for src in SceneStatus for dst in SceneStatus if dst not in SCENE_TRANSITIONS[src]],
)
def test_transition_table_rejects_missing_edges(source: SceneStatus, target: SceneStatus) -> None:
    s = make_scene()
    s.status = source  # bypass to probe the guard directly
    with pytest.raises(IllegalTransitionError):
        s._move(target)


def test_approved_is_only_left_via_stale() -> None:
    s = approved_scene()
    with pytest.raises(IllegalTransitionError):
        s.start()
    with pytest.raises(IllegalTransitionError):
        s.fail(SceneFailure(kind=FailureKind.TRANSIENT, reason_user="x"))


def test_approve_requires_passing_verdict() -> None:
    s = ready_scene()
    s.start()
    s.complete_keyframe(make_image())
    s.start()
    s.complete_clip(make_video())
    with pytest.raises(IllegalTransitionError):
        s.approve(make_verdict(score=0.2))


def _in_qa() -> Scene:
    s = ready_scene()
    s.start()
    s.complete_keyframe(make_image())
    s.start()
    s.complete_clip(make_video())
    return s


def test_reject_for_motion_issue_keeps_keyframe() -> None:
    s = _in_qa()
    s.reject(make_verdict(score=0.4, issues=[make_issue("motion")]))
    assert (s.status, s.phase) == (SceneStatus.PENDING, ScenePhase.CLIP)
    assert s.keyframe is not None and s.clip is None


def test_reject_for_product_issue_discards_keyframe() -> None:
    s = _in_qa()
    s.reject(make_verdict(score=0.4, issues=[make_issue("product")]))
    assert s.phase is ScenePhase.KEYFRAME and s.keyframe is None


def test_blocker_fails_even_with_high_score() -> None:
    v = make_verdict(score=0.95, issues=[make_issue("artifact", "blocker")])
    assert not v.passed


def test_fail_then_retry_via_stale() -> None:
    s = ready_scene()
    s.start()
    s.fail(SceneFailure(kind=FailureKind.EXHAUSTED, reason_user="gave up"))
    assert status(s) is SceneStatus.FAILED
    s.mark_stale(StaleFrom.KEYFRAME)
    assert s.failure is None
    s.requeue()
    assert (s.status, s.phase) == (SceneStatus.PENDING, ScenePhase.KEYFRAME)


def test_stale_clip_keeps_keyframe_and_prompt() -> None:
    s = approved_scene()
    s.mark_stale(StaleFrom.CLIP)
    assert s.keyframe is not None and s.prompt is not None
    assert s.clip is None and s.qa is None
    s.requeue()
    assert s.phase is ScenePhase.CLIP


def test_stale_prompt_clears_everything_and_requires_new_prompt() -> None:
    s = approved_scene()
    s.mark_stale(StaleFrom.PROMPT)
    assert s.prompt is None and s.keyframe is None
    with pytest.raises(IllegalTransitionError, match="new prompt"):
        s.requeue()
    s.set_prompt(make_prompt(keyframe_prompt="same scene, but at dawn"))
    s.requeue()
    assert s.phase is ScenePhase.KEYFRAME


def test_deeper_staleness_wins() -> None:
    s = approved_scene()
    s.mark_stale(StaleFrom.PROMPT)
    s.mark_stale(StaleFrom.CLIP)  # a later, shallower invalidation must not undo the deeper one
    assert s.stale_from is StaleFrom.PROMPT


def test_motion_only_prompt_revision_keeps_keyframe() -> None:
    s = _in_qa()
    s.reject(make_verdict(score=0.4, issues=[make_issue("motion")]))
    s.set_prompt(make_prompt(motion_prompt="slower dolly in, no shake"))
    assert s.keyframe is not None and s.phase is ScenePhase.CLIP


def test_look_prompt_revision_drops_keyframe() -> None:
    s = _in_qa()
    s.reject(make_verdict(score=0.4, issues=[make_issue("motion")]))
    s.set_prompt(make_prompt(keyframe_prompt="different composition"))
    assert s.keyframe is None and s.phase is ScenePhase.KEYFRAME


def test_release_lease_after_crash() -> None:
    s = ready_scene()
    s.start()
    s.release_lease()
    assert status(s) is SceneStatus.PENDING


def test_bump_version_archives_spec() -> None:
    s = ready_scene()
    s.bump_version("make it night", patch_id="pch_1")
    assert s.version == 2
    assert s.history[0].version == 1 and s.history[0].patch_id == "pch_1"
    assert s.history[0].prompt == make_prompt()


@pytest.mark.parametrize(
    "bad",
    [
        {"status": SceneStatus.FAILED},  # failed without failure record
        {"status": SceneStatus.STALE},  # stale without stale_from
        {"status": SceneStatus.QA_REVIEW},  # qa without a clip
        {"status": SceneStatus.APPROVED},  # approved without clip/verdict
        {"keyframe": make_image()},  # keyframe without prompt
    ],
)
def test_invalid_states_rejected_on_load(bad: dict[str, object]) -> None:
    data = make_scene().model_dump() | bad
    with pytest.raises(ValidationError):
        Scene.model_validate(data)

"""M11: the keyframe QA gate (ADR-0003 addendum). A still is reviewed alone before any video is
paid for; a rejected keyframe is regenerated (new seed, prompt revised from the critic's fixes);
only an approved keyframe is animated."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from adsmith.domain.enums import FailureKind, ScenePhase, SceneStatus, Stage
from adsmith.domain.events import EventType
from adsmith.domain.run import RunState
from tests.integration.test_qa_critic import (
    PASS,
    REJECT_MOTION,
    _requests,
    harness,
    issue,
    run_auto,
    scenes,
    verdict,
)
from tests.media_helpers import Harness

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def kf(
    score: float, *, issues: list[dict[str, Any]] | None = None, failed: tuple[str, ...] = ()
) -> dict[str, Any]:
    return verdict(score, issues=issues, failed=failed, frames=1)


KF_PASS = kf(0.88)
KF_REJECT = kf(0.5, issues=[issue("lighting", "major", 0)], failed=("lighting_matches",))


def keyframe_attempts(state: RunState, order: int = 1) -> list[Any]:
    return [a for a in scenes(state)[order - 1].attempts if a.phase is ScenePhase.KEYFRAME]


def events_of(h: Harness, run_id: str, strategy: str) -> list[Any]:
    evs = h.rt.storage.events.tail(run_id, limit=10_000)
    return [e for e in evs if e.payload.get("strategy") == strategy]


async def test_approved_keyframe_goes_to_video_once(tmp_path: Path) -> None:
    h = harness(tmp_path, [PASS], keyframe=[KF_PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert h.image.calls == 4 and h.video.calls == 4
    for s in scenes(state):
        assert s.keyframe_approved and s.keyframe_qa is not None and s.keyframe_qa.passed
        assert s.keyframe_qa.frames_inspected == 1
    assert len(_requests(h, "qa_keyframe")) == 4


async def test_rejected_keyframe_is_regenerated_before_any_video(tmp_path: Path) -> None:
    h = harness(tmp_path, [PASS], keyframe=[KF_REJECT, KF_PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert h.image.calls == 5 and h.video.calls == 4  # one extra still, NO extra clip
    kfs = keyframe_attempts(state)
    assert [a.qa_passed for a in kfs] == [False, True]
    assert kfs[0].params["seed"] != kfs[1].params["seed"]
    assert h.rt.storage.cache.get(kfs[0].request_hash) is None  # known-bad still evicted
    clips = [a for a in scenes(state)[0].attempts if a.phase is ScenePhase.CLIP]
    assert len(clips) == 1 and clips[0].started_at > kfs[1].finished_at  # video only after approval
    rec = events_of(h, state.id, "keyframe_regenerate")
    assert rec and rec[0].payload["saved_usd"] > 0
    rewrite = _requests(h, "prompt_rewrite")[0]
    assert "Reason: keyframe_feedback" in rewrite and "WILL BE REGENERATED" in rewrite


async def test_keyframe_rejected_three_times_fails_the_scene_without_video(tmp_path: Path) -> None:
    h = harness(tmp_path, [PASS], keyframe=[KF_REJECT, KF_REJECT, KF_REJECT, KF_PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE_PARTIAL
    s1 = scenes(state)[0]
    assert s1.status is SceneStatus.FAILED
    assert s1.failure is not None and s1.failure.kind is FailureKind.QUALITY_REJECTED
    assert "keyframe rejected 3" in s1.failure.reason_debug
    assert not [a for a in s1.attempts if a.phase is ScenePhase.CLIP]  # never paid for video
    assert h.video.calls == 3
    assert [s.status for s in scenes(state)[1:]] == [SceneStatus.APPROVED] * 3


async def test_motion_only_clip_rejection_does_not_rereview_the_keyframe(tmp_path: Path) -> None:
    h = harness(tmp_path, [REJECT_MOTION, PASS], keyframe=[KF_PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert h.image.calls == 4 and h.video.calls == 5
    assert len(_requests(h, "qa_keyframe")) == 4  # the approved still is not judged again


async def test_clip_product_issue_keeps_an_approved_keyframe(tmp_path: Path) -> None:
    # A veiled product in a CLIP frame is a motion/clip problem once the still was approved.
    veiled = verdict(0.4, issues=[issue("product", "major", 2)], failed=("product_visible",))
    h = harness(tmp_path, [veiled, PASS], keyframe=[KF_PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert h.image.calls == 4 and h.video.calls == 5
    s1 = scenes(state)[0]
    assert len(keyframe_attempts(state)) == 1 and s1.keyframe_approved
    assert "keyframe is KEPT" in _requests(h, "prompt_rewrite")[0]


async def test_keyframe_verdict_with_motion_issue_is_repaired(tmp_path: Path) -> None:
    bad = kf(0.5, issues=[issue("motion", "major", 0)])
    h = harness(tmp_path, [PASS], keyframe=[bad, KF_PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert h.image.calls == 4  # repaired verdict passed: no regeneration
    evs = h.rt.storage.events.tail(state.id, limit=10_000)
    assert any(
        e.type is EventType.LLM_CALL and "a still has no motion" in str(e.payload) for e in evs
    )

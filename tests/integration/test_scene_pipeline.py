"""M5: keyframe → generate → qa → assemble with real mock media and every recovery path."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from adsmith.agent import commands
from adsmith.config import ChaosConfig
from adsmith.domain.enums import FailureKind, RecoveryAction, ScenePhase, SceneStatus, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.run import RunState
from tests.media_helpers import REQUEST, Harness, fast_settings, make_harness

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


async def run_auto(h: Harness) -> RunState:
    run = h.orch.create_run(REQUEST, interactive=False)
    return await h.orch.run_until_blocked(run.id)


def events(h: Harness, run_id: str) -> list[AgentEvent]:
    return h.rt.storage.events.tail(run_id, limit=10_000)


def statuses(state: RunState) -> list[SceneStatus]:
    assert state.storyboard is not None
    return [s.status for s in state.storyboard.scenes]


def duration_of(h: Harness, state: RunState) -> float:
    assert state.final is not None
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(h.rt.storage.artifacts.resolve(state.final)),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


# ----------------------------------------------------------------------------- happy path


async def test_happy_path_produces_a_15s_ad(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert statuses(state) == [SceneStatus.APPROVED] * 4
    assert duration_of(h, state) == pytest.approx(15.0, abs=0.05)
    assert h.image.calls == 4 and h.video.calls == 4
    # budget: every reservation settled, spend = media attempts + LLM calls
    b = state.budget
    assert b.reserved_usd == pytest.approx(0.0)
    assert all(e.actual_usd is not None for e in b.entries)
    assert state.storyboard is not None
    media = 0.0
    for s in state.storyboard.scenes:
        media += sum(a.cost_usd for a in s.attempts)
    assert b.by_category["image"] + b.by_category["video"] == pytest.approx(media)
    pack = h.rt.storage.artifacts.run_dir(state.id) / "production_pack" / "storyboard.json"
    assert pack.exists()


async def test_interactive_storyboard_gate_before_any_video_spend(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    run = h.orch.create_run(REQUEST, interactive=True)
    gate = await h.orch.run_until_blocked(run.id)
    commands.choose_concept(h.rt.storage, run.id, 1)
    gate = await h.orch.run_until_blocked(run.id)
    assert gate.stage is Stage.AWAITING_STORYBOARD_APPROVAL
    assert h.video.calls == 0 and h.image.calls == 4  # keyframes only: cheap review step
    commands.approve_storyboard(h.rt.storage, run.id)
    done = await h.orch.run_until_blocked(run.id)
    assert done.stage is Stage.DONE


# ----------------------------------------------------------------------------- recovery paths


async def test_transient_errors_are_retried_with_backoff(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), fail_plan="1:keyframe:transient*2")
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    retries = [e for e in events(h, state.id) if e.type is EventType.RETRY_SCHEDULED]
    assert len(retries) == 2 and all("transient" in e.summary for e in retries)
    assert len(h.sleeps) >= 2 and all(0 <= d <= 2.0 for d in h.sleeps[:2])


async def test_transient_exhaustion_fails_only_that_scene(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), fail_plan="3:keyframe:transient")
    state = await run_auto(h)
    assert state.stage is Stage.DONE_PARTIAL
    assert statuses(state) == [
        SceneStatus.APPROVED,
        SceneStatus.APPROVED,
        SceneStatus.FAILED,
        SceneStatus.APPROVED,
    ]
    assert state.storyboard is not None
    failed = state.storyboard.scenes[2]
    assert failed.failure is not None and failed.failure.kind is FailureKind.TRANSIENT


async def test_content_policy_triggers_rewrite_and_recovers(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), fail_plan="2:clip:policy*1")
    state = await run_auto(h)
    assert state.stage is Stage.DONE and state.storyboard is not None
    scene = state.storyboard.scenes[1]
    assert scene.prompt is not None and "(revised" in scene.prompt.motion_prompt
    assert scene.prompt.template_version == "prompt_rewrite@v3"
    actions = [a.recovery_action for a in scene.attempts]
    assert RecoveryAction.POLICY_REWRITE in actions
    rec = [e for e in events(h, state.id) if e.type is EventType.RECOVERY]
    assert any("Policy rewrite" in e.summary and "kept the keyframe" in e.summary for e in rec)
    assert h.image.calls == 4  # motion-only rewrite reused the keyframe


async def test_policy_rewrites_are_bounded(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), fail_plan="2:clip:policy")
    state = await run_auto(h)
    assert state.storyboard is not None
    scene = state.storyboard.scenes[1]
    assert scene.status is SceneStatus.FAILED and scene.failure is not None
    assert scene.failure.kind is FailureKind.CONTENT_POLICY
    clip_attempts = [a for a in scene.attempts if a.phase is ScenePhase.CLIP]
    assert len(clip_attempts) == 3  # original + 2 rewrites


async def test_quality_rejection_regenerates_with_critic_feedback(tmp_path: Path) -> None:
    chaos = ChaosConfig(seed=1, first_attempt_only=True, transient=0, policy=0, quality=1.0)
    h = make_harness(fast_settings(tmp_path), chaos=chaos)
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    evs = events(h, state.id)
    rejected = [e for e in evs if e.type is EventType.RECOVERY and "rejected by QA" in e.summary]
    regen = [e for e in evs if e.type is EventType.RECOVERY and "QA regeneration" in e.summary]
    assert len(rejected) == 4 and len(regen) == 4
    assert h.image.calls == 4 and h.video.calls == 8  # keyframes reused, clips regenerated
    assert state.storyboard is not None
    for scene in state.storyboard.scenes:
        scores = [a.qa_score for a in scene.attempts if a.phase is ScenePhase.CLIP]
        assert scores == [0.45, 0.9]


async def test_quality_regeneration_is_bounded(tmp_path: Path) -> None:
    chaos = ChaosConfig(seed=1, first_attempt_only=False, transient=0, policy=0, quality=1.0)
    h = make_harness(fast_settings(tmp_path), chaos=chaos)
    state = await run_auto(h)
    assert state.stage is Stage.DONE_PARTIAL and state.final is None
    assert all(s is SceneStatus.FAILED for s in statuses(state))
    assert state.storyboard is not None
    assert state.storyboard.scenes[0].failure is not None
    assert state.storyboard.scenes[0].failure.kind is FailureKind.QUALITY_REJECTED


# ----------------------------------------------------------------------------- isolation


async def test_one_permanent_failure_ends_done_partial_with_the_rest(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), fail_plan="2:clip:permanent")
    state = await run_auto(h)
    assert state.stage is Stage.DONE_PARTIAL
    assert statuses(state) == [
        SceneStatus.APPROVED,
        SceneStatus.FAILED,
        SceneStatus.APPROVED,
        SceneStatus.APPROVED,
    ]
    assert state.storyboard is not None
    kept = sum(s.duration_s for s in state.storyboard.scenes if s.status is SceneStatus.APPROVED)
    assert duration_of(h, state) == pytest.approx(kept, abs=0.05)
    done = [e for e in events(h, state.id) if e.type is EventType.RUN_COMPLETED]
    assert "3 approved, 1 failed" in done[0].summary


# ----------------------------------------------------------------------------- budget


async def test_budget_guard_refuses_before_calling_the_provider(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path, budget_usd=0.6))  # ~1 clip after keyframes + LLM
    state = await run_auto(h)
    b = state.budget
    assert b.spent_usd <= b.limit_usd + 1e-9
    assert state.storyboard is not None
    refused = [
        s
        for s in state.storyboard.scenes
        if s.failure and s.failure.kind is FailureKind.BUDGET_EXCEEDED
    ]
    assert refused, "expected at least one scene to be refused"
    assert h.video.calls == 4 - len(refused)


# ----------------------------------------------------------------------------- cache


async def test_rerunning_the_same_storyboard_makes_zero_provider_calls(tmp_path: Path) -> None:
    settings = fast_settings(tmp_path)
    first = make_harness(settings)
    done = await run_auto(first)
    planned = next(v for v in first.rt.storage.runs.history(done.id) if v.stage is Stage.KEYFRAME)
    first.rt.storage.db.close()

    second = make_harness(settings)  # fresh process: new providers with zero call counts
    fork = second.rt.storage.runs.fork(done.id, from_version=planned.state_version).state
    state = await second.orch.run_until_blocked(fork.id)
    assert state.stage is Stage.DONE
    assert second.provider_calls == 0
    hits = [e for e in events(second, fork.id) if e.type is EventType.CACHE_HIT]
    assert len(hits) == 8  # 4 keyframes + 4 clips
    assert (
        state.budget.by_category.get("image", 0) == 0
        and state.budget.by_category.get("video", 0) == 0
    )


# ----------------------------------------------------------------------------- resume


async def test_resume_releases_leases_after_a_crash(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)

    def crash_mid_generation(s: RunState) -> None:  # a process died with scene 1 "generating"
        assert s.storyboard is not None
        s.storyboard.scenes[0].start()

    h.rt.storage.runs.update(state.id, crash_mid_generation, reason="simulated crash")
    resumed = await h.orch.resume(state.id)
    assert resumed.stage is Stage.DONE
    assert any("Released in-flight scenes [1]" in e.summary for e in events(h, state.id))

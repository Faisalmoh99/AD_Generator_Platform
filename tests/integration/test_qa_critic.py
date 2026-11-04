"""M6: the vision QA critic (LLMCritic) with a scripted LLM over real mock media.

Covers frame sampling, what is sent to the model, the deterministic verdict checks (→ repair),
and the QA loop: motion-only rejection keeps the keyframe, a look rejection regenerates it,
repeated rejection fails only that scene, and a critic failure is isolated.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from adsmith.agent.qa import check_verdict
from adsmith.domain.enums import FailureKind, ScenePhase, SceneStatus, Stage
from adsmith.domain.events import EventType
from adsmith.domain.qa import QAVerdictDraft
from adsmith.domain.run import RunState
from adsmith.media.frames import played_duration, sample_frames, sample_times
from adsmith.providers.llm.base import ImagePart, TextPart, UserMessage
from tests.media_helpers import REQUEST, Harness, fast_settings, make_harness

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

OK_CHECKS = {
    "product_visible": True,
    "matches_shot_spec": True,
    "lighting_matches": True,
    "continuity_ok": True,
    "artifacts_free": True,
    "brand_safe": True,
}


def verdict(
    score: float,
    *,
    issues: list[dict[str, Any]] | None = None,
    failed: tuple[str, ...] = (),
    frames: int = 4,
) -> dict[str, Any]:
    checks = OK_CHECKS | dict.fromkeys(failed, False)
    out = {
        "score": score,
        "checks": checks,
        "issues": issues or [],
        "revised_prompt_hint": "",
        "frames_inspected": frames,
    }
    return {"output": out}


def issue(category: str, severity: str = "major", frame: int | None = 1) -> dict[str, Any]:
    return {
        "category": category,
        "severity": severity,
        "observation": f"{category} problem visible in image {frame}",
        "fix": f"Edit the prompt to fix the {category} problem.",
        "frame_index": frame,
    }


PASS = verdict(0.88)
REJECT_MOTION = verdict(0.5, issues=[issue("motion")], failed=("artifacts_free",))
REJECT_PRODUCT = verdict(0.3, issues=[issue("product", "blocker", 0)], failed=("product_visible",))


def harness(tmp_path: Path, critic: list[dict[str, Any]], **over: object) -> Harness:
    settings = fast_settings(tmp_path, qa_mode="llm", scene_concurrency=1, **over)
    return make_harness(settings, scripts={"qa_critic": critic})


async def run_auto(h: Harness) -> RunState:
    run = h.orch.create_run(REQUEST, interactive=False)
    return await h.orch.run_until_blocked(run.id)


def scenes(state: RunState) -> list[Any]:
    assert state.storyboard is not None
    return state.storyboard.scenes


# ----------------------------------------------------------------------------- frame sampling


def test_sample_times_are_inside_the_played_part() -> None:
    assert sample_times(4.0) == [(0.15, 0.6), (0.5, 2.0), (0.85, 3.4)]
    assert all(t < 1.0 for _, t in sample_times(1.0, (0.0, 1.0)))
    with pytest.raises(ValueError, match="positive"):
        sample_times(0)


def test_played_duration_ignores_the_trimmed_tail() -> None:
    assert played_duration(3.5, 4.0) == 3.5  # model rendered 4 s, the ad plays 3.5 s
    assert played_duration(5.0, 4.2) == 4.2  # a short clip: judge what exists
    assert played_duration(3.0, None) == 3.0


async def test_sample_frames_decodes_bounded_jpegs(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    state = await run_auto(h)
    clip = scenes(state)[0].clip
    assert clip is not None
    frames = await sample_frames(h.rt.storage.artifacts.resolve(clip), 3.0, long_side=200)
    assert [f.fraction for f in frames] == [0.15, 0.5, 0.85]
    for f in frames:
        with Image.open(io.BytesIO(f.jpeg)) as img:
            assert img.format == "JPEG" and max(img.size) <= 200


# ----------------------------------------------------------------------------- verdict rules


def test_failed_check_needs_a_matching_issue() -> None:
    bad = QAVerdictDraft.model_validate(verdict(0.4, failed=("lighting_matches",))["output"])
    assert any("lighting_matches" in p for p in check_verdict(bad, n_images=4))
    ok = QAVerdictDraft.model_validate(
        verdict(0.4, issues=[issue("lighting")], failed=("lighting_matches",))["output"]
    )
    assert check_verdict(ok, n_images=4) == []


def test_frame_index_must_name_a_sent_image() -> None:
    bad = QAVerdictDraft.model_validate(
        verdict(0.8, issues=[issue("motion", "minor", 4)])["output"]
    )
    assert any("frame_index=4" in p for p in check_verdict(bad, n_images=4))


# ----------------------------------------------------------------------------- the QA loop


async def test_critic_sees_keyframe_plus_three_labelled_frames(tmp_path: Path) -> None:
    h = harness(tmp_path, [PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert all(s.status is SceneStatus.APPROVED for s in scenes(state))
    assert h.llm is not None
    reqs = [r for r in h.llm.requests if r.template.startswith("qa_critic@")]
    assert len(reqs) == 4  # one per scene
    first = reqs[0].messages[0]
    assert isinstance(first, UserMessage)
    images = [p for p in first.parts if isinstance(p, ImagePart)]
    labels = [p.text for p in first.parts if isinstance(p, TextPart) and p.text.startswith("Image")]
    assert len(images) == 4 and all(p.media_type == "image/jpeg" for p in images)
    assert labels[0].startswith("Image 0: keyframe") and "15%" in labels[1]
    user = first.parts[-1]
    assert isinstance(user, TextPart) and "keyframe_prompt:" in user.text
    # code, not the LLM, decides frames_inspected; critic cost is booked against the scene
    approved = scenes(state)[0]
    assert approved.qa is not None and approved.qa.frames_inspected == 4
    assert state.budget.by_category["llm"] > 0


async def test_motion_rejection_regenerates_clip_and_keeps_keyframe(tmp_path: Path) -> None:
    h = harness(tmp_path, [REJECT_MOTION, PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert h.image.calls == 4 and h.video.calls == 5  # one extra clip, no extra keyframe
    s1 = scenes(state)[0]
    assert s1.status is SceneStatus.APPROVED
    clips = [a for a in s1.attempts if a.phase is ScenePhase.CLIP]
    assert [a.qa_score for a in clips][-2:] == [0.5, 0.88]
    evs = h.rt.storage.events.tail(state.id, limit=10_000)
    rec = [e for e in evs if e.type is EventType.RECOVERY and e.payload.get("keyframe_kept")]
    assert rec, "expected a QA regeneration that kept the keyframe"


async def test_product_rejection_regenerates_the_keyframe(tmp_path: Path) -> None:
    h = harness(tmp_path, [REJECT_PRODUCT, PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert h.image.calls == 5 and h.video.calls == 5
    s1 = scenes(state)[0]
    keyframes = [a for a in s1.attempts if a.phase is ScenePhase.KEYFRAME]
    clips = [a for a in s1.attempts if a.phase is ScenePhase.CLIP]
    assert len(keyframes) == 2
    # The rewrite kept keyframe_prompt byte-identical (scripted echo), yet the retry is a real
    # generation with a new seed, and the rejected outputs are no longer served from cache.
    assert keyframes[0].prompt_used == keyframes[1].prompt_used
    assert keyframes[0].params["seed"] != keyframes[1].params["seed"]
    assert keyframes[1].outcome == "success"
    cache = h.rt.storage.cache
    assert cache.get(keyframes[0].request_hash) is None
    assert cache.get(clips[0].request_hash) is None
    assert cache.get(keyframes[1].request_hash) is not None


async def test_repeated_rejection_fails_only_that_scene(tmp_path: Path) -> None:
    # scene 1 is reviewed first and then after each regeneration (concurrency 1, scene order)
    h = harness(tmp_path, [REJECT_MOTION, PASS, PASS, PASS, REJECT_MOTION, REJECT_MOTION])
    state = await run_auto(h)
    assert state.stage is Stage.DONE_PARTIAL
    statuses = [s.status for s in scenes(state)]
    assert statuses == [SceneStatus.FAILED] + [SceneStatus.APPROVED] * 3
    failure = scenes(state)[0].failure
    assert failure is not None and failure.kind is FailureKind.QUALITY_REJECTED


async def test_inconsistent_verdict_goes_through_repair(tmp_path: Path) -> None:
    inconsistent = verdict(0.4, failed=("lighting_matches",))  # failed check, no issue
    h = harness(tmp_path, [inconsistent, PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    assert h.llm is not None
    reqs = [r for r in h.llm.requests if r.template.startswith("qa_critic@")]
    repair = reqs[1].messages[-1]
    assert isinstance(repair, UserMessage)
    assert any("lighting_matches" in p.text for p in repair.parts if isinstance(p, TextPart))
    assert all(s.status is SceneStatus.APPROVED for s in scenes(state))


async def test_critic_failure_is_isolated_to_its_scene(tmp_path: Path) -> None:
    h = harness(tmp_path, [{"error": "permanent"}, PASS])
    state = await run_auto(h)
    assert state.stage is Stage.DONE_PARTIAL
    statuses = [s.status for s in scenes(state)]
    assert statuses == [SceneStatus.FAILED] + [SceneStatus.APPROVED] * 3


async def test_packaged_scenario_runs_with_the_llm_critic(tmp_path: Path) -> None:
    settings = fast_settings(tmp_path, qa_mode="llm")
    h = make_harness(settings)
    state = await run_auto(h)
    assert state.config.qa_mode == "llm"
    assert state.stage is Stage.DONE

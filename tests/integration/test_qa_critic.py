"""M6: the vision QA critic (LLMCritic) with a scripted LLM over real mock media.

Covers frame sampling, what is sent to the model, the deterministic verdict checks (→ repair),
and the QA loop: motion-only rejection keeps the keyframe, a look rejection regenerates it,
repeated rejection fails only that scene, and a critic failure is isolated.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from adsmith.agent.qa import check_verdict
from adsmith.domain.continuity import atomic_parts
from adsmith.domain.enums import FailureKind, ScenePhase, SceneStatus, Stage
from adsmith.domain.events import EventType
from adsmith.domain.qa import QAVerdict, QAVerdictDraft
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


def harness(
    tmp_path: Path,
    critic: list[dict[str, Any]],
    keyframe: list[dict[str, Any]] | None = None,
    **over: object,
) -> Harness:
    settings = fast_settings(tmp_path, qa_mode="llm", scene_concurrency=1, **over)
    scripts = {"qa_critic": critic} | ({"qa_keyframe": keyframe} if keyframe else {})
    return make_harness(settings, scripts=scripts)


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


# ----------------------------------------------------------------------------- M11 refinements


def test_major_issue_located_in_the_keyframe_needs_a_new_keyframe() -> None:
    def v(category: str, severity: str, frame: int) -> bool:
        draft = verdict(0.4, issues=[issue(category, severity, frame)])["output"]
        return QAVerdict.from_draft(
            QAVerdictDraft.model_validate(draft), threshold=0.7
        ).needs_new_keyframe

    # lighting/composition are SOFT (capped at minor), so they never force a new keyframe alone
    assert not v("lighting", "major", 0)
    assert v("character", "major", 0)  # a non-soft issue located in the still does
    assert not v("lighting", "major", 2)
    assert not v("lighting", "minor", 0)
    assert not v("motion", "major", 0)
    assert v("product", "major", 2)  # look categories regenerate wherever they are seen


def _requests(h: Harness, template: str) -> list[str]:
    assert h.llm is not None
    out = []
    for r in h.llm.requests:
        if r.template.startswith(f"{template}@"):
            msg = r.messages[0]
            assert isinstance(msg, UserMessage)
            out.append("\n".join(p.text for p in msg.parts if isinstance(p, TextPart)))
    return out


async def test_prompt_writer_is_told_render_length_and_visible_window(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    await run_auto(h)
    texts = _requests(h, "prompt_writer")
    # perfume_en scene 1 is 3.5 s; the default video model (Kling) renders whole seconds → 4 s
    assert any("renders 4 seconds" in t and "only the first 3.5 seconds" in t for t in texts)


async def test_kept_keyframe_is_enforced_on_the_rewrite(tmp_path: Path) -> None:
    changed = {
        "output": {
            "keyframe_prompt": "A completely different still image of the bottle, " * 2,
            "motion_prompt": "Light slides slowly across the bottle over three seconds.",
            "negative_prompt": "text, watermark",
        }
    }
    h = make_harness(
        fast_settings(tmp_path, qa_mode="llm", scene_concurrency=1),
        scripts={
            "qa_critic": [REJECT_MOTION, PASS],
            "prompt_rewrite": [changed, {"transform": "echo_prompt"}],
        },
    )
    state = await run_auto(h)
    assert state.stage is Stage.DONE
    rewrites = _requests(h, "prompt_rewrite")
    assert "keyframe is KEPT" in rewrites[0] and "renders 4 seconds" in rewrites[0]
    assert h.llm is not None
    repair = [r for r in h.llm.requests if r.template.startswith("prompt_rewrite@")][1]
    last = repair.messages[-1]
    assert isinstance(last, UserMessage)
    assert any("byte-identical" in p.text for p in last.parts if isinstance(p, TextPart))
    assert h.image.calls == 4  # the keyframe really was reused


async def test_regenerated_keyframe_is_announced_to_the_rewrite(tmp_path: Path) -> None:
    h = harness(tmp_path, [REJECT_PRODUCT, PASS])
    await run_auto(h)
    assert "WILL BE REGENERATED" in _requests(h, "prompt_rewrite")[0]


# ----------------------------------------------------------------------------- stricter pass rule


def _verdict(
    score: float, *, issues: Sequence[dict[str, Any]] = (), failed: tuple[str, ...] = ()
) -> QAVerdict:
    draft = verdict(score, issues=list(issues), failed=failed)["output"]
    return QAVerdict.from_draft(QAVerdictDraft.model_validate(draft), threshold=0.7)


@pytest.mark.parametrize(
    ("v", "passed", "reason"),
    [
        (_verdict(0.9), True, None),
        (_verdict(0.9, issues=[issue("motion")]), True, None),  # one major is tolerated
        (_verdict(0.6), False, "score"),
        (
            _verdict(0.9, issues=[issue("product", "minor")], failed=("product_visible",)),
            False,
            "product",
        ),
        (
            _verdict(0.9, issues=[issue("continuity", "minor")], failed=("continuity_ok",)),
            False,
            "identity",
        ),
        (
            _verdict(0.9, issues=[issue("motion"), issue("artifact")]),
            True,
            None,
        ),  # majors alone don't fail
        (_verdict(0.9, issues=[issue("artifact", "minor")], failed=("text_free",)), False, "text"),
        (_verdict(0.9, issues=[issue("lighting", "blocker")]), True, None),  # soft: capped,
        (_verdict(0.9, issues=[issue("artifact", "blocker")]), False, "blocker"),
        (
            _verdict(0.9, issues=[issue("safety", "minor")], failed=("brand_safe",)),
            False,
            "brand-safe",
        ),
    ],
)
def test_pass_rule_is_decided_by_code(v: QAVerdict, passed: bool, reason: str | None) -> None:
    assert v.passed is passed
    if reason:
        assert any(reason in r for r in v.fail_reasons)


def test_real_m11_verdict_two_under_the_hard_soft_rule() -> None:
    # Verbatim shape of the real LTX S4 verdict (score 0.72): failed under the "2+ majors" rule,
    # passes under the hard/soft rule (missing motion beat + light dip are not hard failures).
    v = _verdict(
        0.72,
        issues=[
            issue("motion", "major", 2),
            issue("lighting", "major", 3),
            issue("motion", "minor", 3),
        ],
        failed=("matches_shot_spec", "lighting_matches"),
    )
    assert v.passed and v.fail_reasons == []
    assert [i.severity for i in v.issues] == ["major", "minor", "minor"]  # lighting capped


async def test_high_score_rejections_still_count_toward_the_limit(tmp_path: Path) -> None:
    two_majors = verdict(0.95, issues=[issue("product", frame=2)], failed=("product_visible",))
    h = harness(tmp_path, [two_majors, PASS, PASS, PASS, two_majors, two_majors])
    state = await run_auto(h)
    s1 = scenes(state)[0]
    assert s1.status is SceneStatus.FAILED  # 3 reviews, then given up: no endless loop
    clips = [a for a in s1.attempts if a.phase is ScenePhase.CLIP]
    assert [a.qa_passed for a in clips] == [False, False, False]
    assert all(a.qa_score == 0.95 for a in clips)


async def test_scene_approved_under_an_older_rule_still_loads(tmp_path: Path) -> None:
    """Regression (seen live in M11): tightening the pass rule made a stored approved snapshot
    unloadable, because the load-time invariant re-judged the old verdict with the new rule."""
    h = make_harness(fast_settings(tmp_path))
    state = await run_auto(h)
    old_style = verdict(0.72, issues=[issue("product")], failed=("product_visible",))["output"]
    old = QAVerdict.from_draft(QAVerdictDraft.model_validate(old_style), threshold=0.7)
    assert not old.passed  # fails today's rule

    def backdate(s: RunState) -> list[Any]:
        assert s.storyboard is not None
        s.storyboard.scenes[0].qa = old  # as if approved before the rule changed
        return []

    h.rt.storage.runs.update(state.id, backdate, reason="simulate an older approval")
    reloaded = h.rt.storage.runs.load(state.id)
    assert scenes(reloaded)[0].status is SceneStatus.APPROVED
    with pytest.raises(Exception, match="failing verdict"):
        scenes(reloaded)[1].approve(old)  # new decisions still use the current rule


async def test_product_checklist_is_derived_from_the_bible_and_sent(tmp_path: Path) -> None:
    h = harness(tmp_path, [PASS])
    state = await run_auto(h)
    assert state.bible is not None
    product = state.bible.product
    items = product.checklist()
    assert items[0].startswith("Shape:") and items[0].removeprefix("Shape: ") in product.form
    wordless = product.model_copy(update={"branding_text": ""})
    assert "WORDLESS" in wordless.checklist()[-1]
    texted = product.model_copy(update={"branding_text": "NOOR"})
    assert "only the text 'NOOR'" in texted.checklist()[-1]
    sent = _requests(h, "qa_critic")
    with_product = [s for s, sc in zip(sent, scenes(state), strict=True) if sc.shows_product]
    assert with_product and all(items[0] in t for t in with_product)


def test_composite_bible_fields_become_atomic_checks() -> None:
    form = (
        "Rectangular faceted glass bottle with thick beveled base, chamfered shoulders and a "
        "heavy squared cap, about hand-sized"
    )
    assert atomic_parts(form) == [
        "Rectangular faceted glass bottle",
        "thick beveled base",
        "chamfered shoulders",
        "a heavy squared cap",
        "about hand-sized",
    ]
    assert atomic_parts("matte") == ["matte"]

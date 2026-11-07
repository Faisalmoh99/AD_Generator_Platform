"""Phase B units: the ad prompt checks, the generation's state machine, format resolution and
the whole-ad branches of the transition table (pure, no I/O)."""

from __future__ import annotations

from typing import Any

import pytest

from adsmith.agent.transitions import next_stage
from adsmith.config import Settings
from adsmith.domain.ad import (
    AdGeneration,
    AdPrompt,
    AdPromptDraft,
    IllegalAdTransitionError,
    ad_prompt_problems,
    render_ad_prompt,
)
from adsmith.domain.enums import FailureKind, ScenePhase, Stage
from adsmith.domain.formats import AdFormat, resolve_format
from adsmith.domain.generation import GenerationAttempt
from adsmith.domain.qa import QAChecks, QAVerdict
from adsmith.domain.run import RunConfig, RunState
from adsmith.domain.scene import SceneFailure
from tests.factories import make_full_run, make_video, sha


def shots(durations: list[float]) -> list[dict[str, Any]]:
    out, t = [], 0.0
    for i, d in enumerate(durations, 1):
        out.append(
            {
                "number": i,
                "start_s": t,
                "end_s": t + d,
                "action": f"beat {i}.",
                "event": "a hand lifts",
                "camera": "close-up, slow push-in",
                "sound": "room tone",
                "shows_product": True,
                "product_in_frame": "half",
                "handled": i == 1,
            }
        )
        t += d
    return out


def draft(durations: list[float]) -> AdPromptDraft:
    return AdPromptDraft.model_validate(
        {
            "location": "a warm majlis",
            "look": "warm amber film look",
            "lighting": "warm lamps from the left",
            "references": [],
            "shots": shots(durations),
            "key_shot": 1,
            "audio": "oud then strings",
        }
    )


def generation(**over: Any) -> AdGeneration:
    d = draft([3, 3, 3, 3])
    prompt = AdPrompt.from_draft(
        d,
        duration_s=12,
        aspect_ratio="9:16",
        text=render_ad_prompt(d, duration_s=12, aspect_ratio="9:16"),
        template_version="ad_prompt_writer@v1",
        bible_version=1,
    )
    base: dict[str, Any] = {
        "prompt": prompt,
        "model": "seedance_20_ref",
        "provider": "mock",
        "duration_s": 12,
        "resolution": "720p",
        "aspect_ratio": "9:16",
    }
    return AdGeneration(**(base | over))


def attempt() -> GenerationAttempt:
    return GenerationAttempt(
        scene_id="ad",
        scene_version=1,
        phase=ScenePhase.CLIP,
        attempt_no=1,
        provider="mock",
        model="m",
        request_hash=sha("r"),
        prompt_used="p",
    )


def passing(passed: bool = True) -> QAVerdict:
    checks = QAChecks(
        product_visible=True,
        matches_shot_spec=True,
        lighting_matches=True,
        continuity_ok=passed,
        artifacts_free=True,
        brand_safe=True,
    )
    return QAVerdict(
        score=0.9 if passed else 0.5, checks=checks, frames_inspected=13, threshold=0.7
    )


# ----------------------------------------------------------------------------- the ad prompt


def test_a_prompt_that_follows_the_storyboard_has_no_problems() -> None:
    assert (
        ad_prompt_problems(draft([3, 3, 3, 3]), [3, 3, 3, 3], duration_s=12, max_chars=2500) == []
    )


def test_rendered_prompt_carries_the_fixed_rules() -> None:
    text = render_ad_prompt(draft([4, 4, 4]), duration_s=12, aspect_ratio="9:16")
    assert text.startswith("Image 1 is the product: keep its shape")
    assert "A 12-second vertical 9:16 film, 3 shots, hard cuts." in text
    assert "No text, logos, subtitles or watermark." in text
    assert "Shot 2 (4-8 s): Beat 2." in text and "No voice, no dialogue." in text


@pytest.mark.parametrize(
    ("durations", "scene_durations", "message"),
    [
        ([3, 3, 3], [3, 3, 3, 3], "write exactly 4 shots"),
        ([3, 3, 3, 4], [3, 3, 3, 3], "shot 4 must end at 12"),
        ([2, 3, 3, 4], [3, 3, 3, 3], "shot 1 must end at 3"),
        ([3, 3, 3, 3], [3, 3, 3, 3.0], None),
    ],
)
def test_prompt_problems_follow_the_storyboard(
    durations: list[float], scene_durations: list[float], message: str | None
) -> None:
    problems = ad_prompt_problems(draft(durations), scene_durations, duration_s=12, max_chars=2500)
    if message is None:
        assert problems == []
    else:
        assert any(message in p for p in problems), problems


def test_a_prompt_over_the_model_limit_is_repaired() -> None:
    problems = ad_prompt_problems(draft([3, 3, 3, 3]), [3, 3, 3, 3], duration_s=12, max_chars=200)
    assert any("the limit is 200" in p for p in problems)


# ----------------------------------------------------------------------------- the state machine


def test_generation_needs_an_approved_estimate_to_start() -> None:
    g = generation()
    with pytest.raises(IllegalAdTransitionError):
        g.start(attempt())
    g.set_estimate(3.6)
    with pytest.raises(IllegalAdTransitionError, match="not been approved"):
        g.start(attempt())
    g.approve("user")
    g.start(attempt())
    assert g.status == "generating" and g.approved_usd == 3.6


def test_review_approves_or_flags_and_never_regenerates() -> None:
    for ok, status in ((True, "approved"), (False, "flagged")):
        g = generation()
        g.set_estimate(3.6)
        g.approve("user")
        g.start(attempt())
        g.complete(make_video("ad"))
        g.review(passing(ok))
        assert g.status == status and len(g.attempts) == 1


def test_a_human_accepts_a_flagged_generation_and_can_renew() -> None:
    g = generation()
    g.set_estimate(3.6)
    g.approve("user")
    g.start(attempt())
    g.complete(make_video("ad"))
    g.review(passing(False))
    g.accept("the cap is right at full size")
    assert g.status == "approved" and g.accepted_by_human
    g.renew("another take")
    assert (g.version, g.status, g.est_usd, g.artifact) == (2, "pending", None, None)
    assert g.history[0].status == "approved" and g.history[0].est_usd == 3.6


def test_a_crash_releases_the_job_but_keeps_the_approval() -> None:
    g = generation()
    g.set_estimate(3.6)
    g.approve("user")
    g.start(attempt())
    g.release()
    assert g.status == "estimated" and g.approved


def test_invariants_on_load() -> None:
    with pytest.raises(ValueError, match="requires the generated video"):
        generation(status="qa_review")
    with pytest.raises(ValueError, match="failure record"):
        generation(status="failed")
    with pytest.raises(ValueError, match="needs an estimate"):
        generation(approved_usd=1.0)


# ----------------------------------------------------------------------------- format resolution


def test_a_photo_makes_the_run_multishot_unless_a_format_is_forced(settings: Settings) -> None:
    lib = settings.formats
    fmt, why = resolve_format(
        lib, requested=None, requested_shots=None, physical_product=True, has_photo=True
    )
    assert fmt is AdFormat.MULTISHOT and "photo" in why
    fmt, _ = resolve_format(
        lib,
        requested=AdFormat.MONTAGE,
        requested_shots=None,
        physical_product=True,
        has_photo=True,
        forced=AdFormat.NARRATIVE,
    )
    assert fmt is AdFormat.NARRATIVE
    fmt, _ = resolve_format(lib, requested=None, requested_shots=None, physical_product=True)
    assert fmt is AdFormat.MONTAGE  # no photo: today's behaviour
    g = lib.grammar(AdFormat.MULTISHOT)
    assert (
        g.generation == "whole" and g.identity == "photo" and (g.min_shots, g.max_shots) == (3, 4)
    )


# ----------------------------------------------------------------------------- transitions


def whole_run(**gen: Any) -> RunState:
    state = make_full_run(Stage.PROMPT)
    state.config = RunConfig(interactive=False)
    state.generation = generation(**gen)
    return state


def test_prompt_goes_to_the_director_then_the_estimate_for_a_whole_ad() -> None:
    assert next_stage(whole_run(), Stage.PROMPT) is Stage.DIRECTOR
    assert next_stage(whole_run(), Stage.DIRECTOR) is Stage.PROPS
    assert next_stage(whole_run(), Stage.PROPS) is Stage.COPY
    assert next_stage(whole_run(), Stage.COPY) is Stage.ESTIMATE
    plain = make_full_run(Stage.PROMPT)
    assert next_stage(plain, Stage.PROMPT) is Stage.KEYFRAME


def test_estimate_waits_for_a_human_unless_code_approved() -> None:
    s = whole_run()
    assert s.generation is not None
    s.generation.set_estimate(3.6)
    assert next_stage(s, Stage.ESTIMATE) is Stage.AWAITING_GENERATION_APPROVAL
    s.generation.approve("auto_mock")
    assert next_stage(s, Stage.ESTIMATE) is Stage.GENERATE
    failed = whole_run()
    assert failed.generation is not None
    failed.generation.fail(
        SceneFailure(kind=FailureKind.BUDGET_EXCEEDED, reason_user="over the cap")
    )
    assert next_stage(failed, Stage.ESTIMATE) is Stage.ASSEMBLE


def test_generate_qa_and_assemble_for_a_whole_ad() -> None:
    s = whole_run()
    g = s.generation
    assert g is not None
    g.set_estimate(3.6)
    g.approve("user")
    g.start(attempt())
    g.complete(make_video("ad"))
    assert next_stage(s, Stage.GENERATE) is Stage.QA
    g.review(passing(False))
    assert next_stage(s, Stage.QA) is Stage.ASSEMBLE  # warn only: no loop back to generate
    s.final = make_video("final")
    assert next_stage(s, Stage.ASSEMBLE) is Stage.DONE_PARTIAL
    g.accept("ok")
    assert next_stage(s, Stage.ASSEMBLE) is Stage.DONE

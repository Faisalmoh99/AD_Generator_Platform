"""M11: video-model capability profile + the single-take hero format (one continuous scene)."""

from __future__ import annotations

import copy
from typing import Any

from adsmith.config import Settings
from adsmith.domain.enums import Stage
from adsmith.domain.shot_lint import product_presence_problem
from tests.unit.test_orchestrator import REQUEST, full_scripts, make_orch, steps


def single_take(**over: Any) -> list[dict[str, Any]]:
    sb = copy.deepcopy(steps("storyboard"))
    hero = sb[0]["output"]["scenes"][-1]
    hero = hero | {"duration_s": 15.0, "purpose": "product", "shows_product": True}
    hero["shot"] = hero["shot"] | {
        "subject_action": "A man strides down a sunlit street as the perfume bottle slips from "
        "his hand and tumbles toward the lens in extreme slow motion until it fills the frame.",
        **over.pop("shot", {}),
    }
    sb[0]["output"]["scenes"] = [hero | over]
    return sb


def _repair_text(provider: Any, template: str) -> str:
    names = [r.template for r in provider.requests]
    msg = provider.requests[names.index(template) + 1].messages[-1]
    return str(msg.parts[0].text)


async def test_single_take_storyboard_is_accepted(settings: Settings) -> None:
    orch, provider = make_orch(settings, full_scripts(storyboard=single_take()))
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.KEYFRAME and state.storyboard is not None
    assert len(state.storyboard.scenes) == 1 and state.storyboard.scenes[0].duration_s == 15.0
    assert provider.calls_for("storyboard") == 1  # "as"/"until" chain is fine in a single take
    assert provider.calls_for("prompt_writer") == 1


async def test_single_take_must_end_on_the_product(settings: Settings) -> None:
    bad = single_take(purpose="hook")
    orch, provider = make_orch(settings, full_scripts(storyboard=[bad[0], single_take()[0]]))
    run = orch.create_run(REQUEST, interactive=False)
    await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert "single-take ad ends on the product" in _repair_text(provider, "storyboard@v4")


async def test_multi_scene_ads_keep_the_8s_scene_cap(settings: Settings) -> None:
    sb = copy.deepcopy(steps("storyboard"))
    scenes = sb[0]["output"]["scenes"]
    scenes[0]["duration_s"] = 9.0
    rest = (15.0 - 9.0) / (len(scenes) - 1)
    for s in scenes[1:]:
        s["duration_s"] = max(3.0, rest)
    while sum(s["duration_s"] for s in scenes) > 15.0 and len(scenes) > 2:
        scenes.pop(1)
    scenes[-1]["duration_s"] = 15.0 - sum(s["duration_s"] for s in scenes[:-1])
    orch, provider = make_orch(settings, full_scripts(storyboard=[sb[0], steps("storyboard")[0]]))
    run = orch.create_run(REQUEST, interactive=False)
    await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert "each scene lasts at most 8s" in _repair_text(provider, "storyboard@v4")


async def test_video_profile_is_injected_into_planning_prompts(settings: Settings) -> None:
    orch, provider = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=False)
    await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    for template in ("ideate", "select", "storyboard"):
        req = next(r for r in provider.requests if r.template.startswith(f"{template}@"))
        text = "\n".join(p.text for m in req.messages for p in getattr(m, "parts", ()))
        assert "<video_model_profile>" in text and "Renders badly:" in text, template
        assert "one continuous take up to 15 s" in text, template


def test_single_take_product_presence_rules() -> None:
    ok = [("product", True, "the perfume bottle flies toward the lens and fills the frame")]
    assert product_presence_problem(ok, "luxury perfume") is None
    hidden = [("product", False, "a man walks down a street in slow motion")]
    assert "single-take ad must show the product" in str(
        product_presence_problem(hidden, "luxury perfume")
    )
    wrong_end = [("hook", True, "the perfume bottle spins in the air")]
    assert "ends on the product" in str(product_presence_problem(wrong_end, "luxury perfume"))

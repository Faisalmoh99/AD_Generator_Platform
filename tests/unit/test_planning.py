"""M4b: storyboard → continuity → prompt, scene-flag wiring, failure isolation, Production Pack."""

from __future__ import annotations

import copy
import json
from typing import Any

from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.enums import FailureKind, SceneStatus, Stage
from adsmith.domain.events import EventType
from tests.unit.test_orchestrator import REQUEST, full_scripts, make_orch, steps


async def test_full_planning_pipeline_and_production_pack(settings: Settings) -> None:
    orch, provider = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)

    assert state.stage is Stage.KEYFRAME
    assert state.storyboard and state.bible and state.storyboard.total_duration_s == 15.0
    assert all(s.prompt is not None for s in state.storyboard.scenes)
    assert all(s.prompt.bible_version == 1 for s in state.storyboard.scenes if s.prompt)
    assert provider.calls_for("prompt_writer") == 4

    pack = orch.rt.storage.artifacts.run_dir(run.id) / "production_pack"
    data = json.loads((pack / "storyboard.json").read_text())
    assert data["schema_version"] == 1 and len(data["scenes"]) == 4
    assert data["scenes"][0]["prompt"]["keyframe_prompt"].startswith("Vertical 9:16")
    assert data["category_cliches_avoided"]
    md = (pack / "storyboard.md").read_text()
    assert "## Shot list" in md and "**Keyframe prompt**" in md and "cap cap" not in md
    assert any(e.type is EventType.EXPORT for e in orch.rt.storage.events.tail(run.id, limit=500))


async def test_prompt_cache_is_warmed_by_first_scene(settings: Settings) -> None:
    orch, provider = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=False)
    await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    writes = [r for r in provider.requests if r.template.startswith("prompt_writer")]
    contexts = {r.messages[0].parts[0].text for r in writes}  # type: ignore[union-attr]
    assert len(contexts) == 1  # identical cached CONTEXT for every scene
    assert all(r.messages[0].parts[0].cache for r in writes)  # type: ignore[union-attr]


async def test_one_scene_prompt_failure_is_isolated(settings: Settings) -> None:
    pw = steps("prompt_writer")
    scripted = [pw[0], {"raw": "nope"}, {"raw": "nope"}, {"raw": "nope"}, pw[2], pw[3]]
    orch, _ = make_orch(settings, full_scripts(prompt_writer=scripted))
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.KEYFRAME and state.storyboard is not None  # stage not failed
    statuses = [s.status for s in state.storyboard.scenes]
    assert statuses.count(SceneStatus.FAILED) == 1 and statuses.count(SceneStatus.PENDING) == 3
    failed = next(s for s in state.storyboard.scenes if s.status is SceneStatus.FAILED)
    assert failed.failure is not None and failed.failure.kind is FailureKind.INVALID_OUTPUT
    assert failed.failure.reason_user.startswith("Failed while writing generation prompts")


async def test_storyboard_rejects_scenes_below_video_model_minimum(settings: Settings) -> None:
    good = steps("storyboard")
    short = copy.deepcopy(good)
    short[0]["output"]["scenes"][0]["duration_s"] = 2.0
    short[0]["output"]["scenes"][1]["duration_s"] = 5.5  # still sums to 15
    orch, provider = make_orch(settings, full_scripts(storyboard=[short[0], good[0]]))
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.KEYFRAME
    repair = provider.requests[[r.template for r in provider.requests].index("storyboard@v9") + 1]
    assert "video model's 3s minimum" in repair.messages[-1].parts[0].text  # type: ignore[union-attr]


async def test_continuity_upgrades_flags_with_bible_terms(settings: Settings) -> None:
    sb: list[dict[str, Any]] = copy.deepcopy(steps("storyboard"))
    scene = sb[0]["output"]["scenes"][0]
    scene["shows_character"] = False
    scene["description"] = "The silver wristwatch glints on the steering wheel."
    # Only bible vocabulary (wardrobe: "silver wristwatch") may reveal the character here, so the
    # generic storyboard-stage check sees nothing and the continuity-stage check must catch it.
    scene["shot"]["subject_action"] = "the car glides forward under neon"
    scene["shot"]["composition"] = "car centered on the lower third"
    orch, _ = make_orch(settings, full_scripts(storyboard=sb))
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.storyboard is not None and state.storyboard.scenes[0].shows_character
    overrides = [
        e
        for e in orch.rt.storage.events.tail(run.id, limit=500)
        if e.type is EventType.DECISION and e.payload.get("override")
    ]
    assert overrides and overrides[0].stage is Stage.CONTINUITY


async def test_zero_key_arabic_run_uses_arabic_scenario_and_pack(settings: Settings) -> None:
    """Quickstart A in Arabic: scripted scenario chosen from the request script, Arabic pack."""
    orch = Orchestrator(AgentRuntime.from_settings(settings))  # no provider override
    run = orch.create_run("سوّ لي إعلان 15 ثانية لعطر رجالي فاخر", interactive=False)
    assert run.config.scripted_scenario == "perfume_ar"
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.KEYFRAME and state.brief is not None
    assert state.brief.language.value == "ar"
    md = (
        orch.rt.storage.artifacts.run_dir(run.id) / "production_pack" / "storyboard.md"
    ).read_text()
    assert "## قائمة اللقطات" in md and "أمر الإطار الرئيسي" in md


async def test_storyboard_must_match_the_requested_shot_count(settings: Settings) -> None:
    understand = copy.deepcopy(steps("understand"))
    understand[0]["output"]["requested_shots"] = 4  # the fixture storyboard has 4 scenes
    good = steps("storyboard")
    three = copy.deepcopy(good)
    three[0]["output"]["scenes"] = three[0]["output"]["scenes"][:3]
    orch, provider = make_orch(
        settings, full_scripts(understand=understand, storyboard=[three[0], good[0]])
    )
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.KEYFRAME and state.storyboard is not None
    assert len(state.storyboard.scenes) == 4
    repair = provider.requests[[r.template for r in provider.requests].index("storyboard@v9") + 1]
    assert "asked for exactly 4 shot(s)" in repair.messages[-1].parts[0].text  # type: ignore[union-attr]

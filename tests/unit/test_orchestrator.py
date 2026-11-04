"""Run loop, atomic commits, human gate, auto-select, failures, resume-from-checkpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent import commands, orchestrator
from adsmith.agent.context import AgentRuntime
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.enums import FailureKind, Language, Stage
from adsmith.domain.events import EventType
from adsmith.domain.run import RunState
from adsmith.providers.llm.scripted import FIXTURES_DIR, ScriptedLLMProvider
from adsmith.storage import ConcurrencyConflictError

EN = FIXTURES_DIR / "perfume_en"
REQUEST = "Make me a 15-second ad for a luxury men's perfume targeting young men, cinematic."
AR_REQUEST = "سوّ لي إعلان ١٥ ثانية لعطر رجالي فاخر يستهدف الشباب، بأسلوب سينمائي"


def steps(name: str) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads((EN / f"{name}.json").read_text())
    return data


def full_scripts(**over: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    names = ("understand", "ideate", "select", "storyboard", "continuity", "prompt_writer")
    return {n: steps(n) for n in names} | over


def make_orch(
    settings: Settings, scripts: dict[str, list[dict[str, Any]]]
) -> tuple[Orchestrator, ScriptedLLMProvider]:
    provider = ScriptedLLMProvider(scripts)
    return Orchestrator(AgentRuntime.from_settings(settings, llm_provider=provider)), provider


def types(orch: Orchestrator, run_id: str) -> list[str]:
    return [e.type.value for e in orch.rt.storage.events.tail(run_id, limit=1000)]


# ----------------------------------------------------------------------------- happy paths


async def test_interactive_run_stops_at_concept_gate(settings: Settings) -> None:
    orch, _ = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=True)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)

    assert state.stage is Stage.AWAITING_CONCEPT_CHOICE
    assert state.brief is not None and state.concepts is not None
    assert state.concepts.selected_id is None
    assert types(orch, run.id) == [
        "decision",  # run created
        "stage_started",
        "llm_call",
        "decision",
        "stage_completed",  # understand
        "stage_started",
        "llm_call",
        "decision",
        "stage_completed",  # ideate
        "stage_started",
        "llm_call",
        "decision",  # the judge recommends (it does not select in interactive mode)
        "stage_completed",  # select
        "awaiting_input",
    ]
    hist = orch.rt.storage.runs.history(run.id)
    assert [(h.state_version, h.stage) for h in hist] == [
        (1, Stage.UNDERSTAND),
        (2, Stage.IDEATE),
        (3, Stage.SELECT),
        (4, Stage.AWAITING_CONCEPT_CHOICE),
    ]


async def test_llm_cost_is_booked_and_matches_trace(settings: Settings) -> None:
    orch, _ = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=True)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    calls = [
        e for e in orch.rt.storage.events.tail(run.id, limit=100) if e.type is EventType.LLM_CALL
    ]
    traced = 0.0
    for e in calls:
        cost = e.payload["cost_usd"]
        assert isinstance(cost, float)
        traced += cost
    assert state.budget.spent_usd == pytest.approx(traced) and traced > 0
    assert len(state.budget.entries) == len(calls) == 3  # understand, ideate, select


async def test_choose_concept_then_continue(settings: Settings) -> None:
    orch, _ = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=True)
    gate = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    state = commands.choose_concept(orch.rt.storage, run.id, 3, expected_version=gate.state_version)
    assert state.stage is Stage.STORYBOARD
    assert state.concepts is not None and state.concepts.selected_by == "user"
    assert state.concepts.selected is state.concepts.concepts[2] or (
        state.concepts.selected_id == state.concepts.concepts[2].id
    )


async def test_commands_validate_stage_and_version(settings: Settings) -> None:
    orch, _ = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=True)
    with pytest.raises(commands.CommandRejectedError, match="not waiting"):
        commands.choose_concept(orch.rt.storage, run.id, 1)
    gate = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    with pytest.raises(commands.CommandRejectedError, match="between 1 and 3"):
        commands.choose_concept(orch.rt.storage, run.id, 4)
    with pytest.raises(ConcurrencyConflictError):
        commands.choose_concept(orch.rt.storage, run.id, 1, expected_version=gate.state_version - 1)


async def test_auto_mode_selects_with_judge(settings: Settings) -> None:
    orch, _ = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.KEYFRAME  # planning complete; keyframe node arrives in M5
    assert state.concepts is not None and state.concepts.selected_by == "agent"
    assert state.concepts.selected_id == state.concepts.concepts[0].id  # highest weighted score
    select_event = next(
        e
        for e in orch.rt.storage.events.tail(run.id, limit=100)
        if e.stage is Stage.SELECT and e.type is EventType.DECISION
    )
    assert "Selected concept 1" in select_event.summary and "scores" in select_event.payload


async def test_stop_before(settings: Settings) -> None:
    orch, provider = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.IDEATE)
    assert state.stage is Stage.IDEATE and provider.calls_for("ideate") == 0


async def test_language_is_overridden_by_script(settings: Settings) -> None:
    wrong = steps("understand")
    orch, _ = make_orch(settings, full_scripts(understand=wrong))  # fixture says "en"
    run = orch.create_run(AR_REQUEST, interactive=True)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.IDEATE)
    assert state.brief is not None and state.brief.language is Language.AR
    assert any("Overrode language" in e.summary for e in orch.rt.storage.events.tail(run.id))


# ----------------------------------------------------------------------------- failures


async def test_typed_failure_marks_errored_and_resume_recovers(settings: Settings) -> None:
    orch, provider = make_orch(
        settings, full_scripts(understand=[{"error": "permanent"}, *steps("understand")])
    )
    run = orch.create_run(REQUEST, interactive=True)
    failed = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert failed.stage is Stage.ERRORED and failed.error is not None
    assert failed.error.stage is Stage.UNDERSTAND and failed.error.kind is FailureKind.PERMANENT
    assert failed.error.reason_user.startswith("Failed while understanding the request")
    assert "error" in types(orch, run.id)

    resumed = await orch.resume(run.id)
    assert resumed.stage is Stage.AWAITING_CONCEPT_CHOICE and resumed.error is None
    assert provider.calls_for("understand") == 2


async def test_failed_calls_are_still_booked(settings: Settings) -> None:
    orch, _ = make_orch(settings, full_scripts(ideate=[{"raw": "not json"}]))
    run = orch.create_run(REQUEST, interactive=True)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.ERRORED and state.error is not None
    assert state.error.kind is FailureKind.INVALID_OUTPUT
    # understand (1 call) + ideate (1 + 2 repairs), all booked
    assert len(state.budget.entries) == 1 + 1 + settings.llm_max_repairs


async def test_arabic_failure_message_before_brief_exists(settings: Settings) -> None:
    orch, _ = make_orch(settings, full_scripts(understand=[{"error": "permanent"}]))
    run = orch.create_run(AR_REQUEST, interactive=True)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.error is not None and state.error.reason_user.startswith("تعذّر فهم الطلب")


async def test_unexpected_exception_becomes_run_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(state: RunState, ctx: object) -> NodeResult:
        raise KeyError("bug in node")

    monkeypatch.setitem(orchestrator.NODES, Stage.UNDERSTAND, broken)
    orch, _ = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.ERRORED and state.error is not None
    assert "KeyError" in state.error.reason_debug


async def test_result_is_discarded_if_run_moved_meanwhile(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, _ = make_orch(settings, full_scripts())
    real_understand = orchestrator.NODES[Stage.UNDERSTAND]

    async def racing(state: RunState, ctx: Any) -> NodeResult:
        result = await real_understand(state, ctx)

        def move(s: RunState) -> None:  # another writer moves the run on while we "think"
            s.brief = result.deltas[0].brief  # type: ignore[union-attr]
            s.stage = Stage.IDEATE

        orch.rt.storage.runs.update(state.id, move, reason="concurrent writer")
        return result

    monkeypatch.setitem(orchestrator.NODES, Stage.UNDERSTAND, racing)
    run = orch.create_run(REQUEST)
    state = await orch.step(run.id)
    assert state.stage is Stage.IDEATE  # the concurrent write stands
    assert any(
        "Discarded the understand result" in e.summary for e in orch.rt.storage.events.tail(run.id)
    )


# ----------------------------------------------------------------------------- resume


async def test_resume_from_checkpoint_in_a_new_process(settings: Settings) -> None:
    orch1, p1 = make_orch(settings, full_scripts())
    run = orch1.create_run(REQUEST, interactive=False)
    await orch1.run_until_blocked(run.id, stop_before=Stage.SELECT)  # "crash" after ideate
    orch1.rt.storage.db.close()

    orch2, p2 = make_orch(settings, full_scripts())  # fresh runtime, same data dir
    state = await orch2.resume(run.id, stop_before=Stage.STORYBOARD)
    assert state.stage is Stage.STORYBOARD
    assert (p2.calls_for("understand"), p2.calls_for("ideate"), p2.calls_for("select")) == (0, 0, 1)
    assert p1.calls_for("select") == 0


async def test_scripted_scenario_follows_request_language(
    settings: Settings, tmp_path: Path
) -> None:
    orch = Orchestrator(AgentRuntime.from_settings(settings))
    assert orch.create_run(REQUEST).config.scripted_scenario == "perfume_en"

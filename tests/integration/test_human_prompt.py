"""Human creative direction at the spend gate: `adsmith approve <run> --prompt-file <path>`
replaces the model-written prompt, prices it again and keeps reservation, critic and assembly
unchanged."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adsmith.agent import commands
from adsmith.agent.commands import CommandRejectedError
from adsmith.agent.orchestrator import Orchestrator
from adsmith.agent.qa import ad_shot_windows
from adsmith.cli.main import app
from adsmith.domain.ad import parse_prompt_shots, prompt_diff_summary
from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from tests.integration.test_multishot_pipeline import (
    REQUEST,
    mock_provider,
    photo,
    runtime,
    settings,
)

HUMAN = """An 8-second vertical 9:16 film in 4 shots, hard cuts between shots.
The product is exactly the one in the reference image: keep it identical. No text anywhere.
Look: Warm, calm Gulf home in a muted sand and cream grade.
Shot 1 (0-2 s): Incense smoke slips under a closed honey-wood front door. Sound: footsteps.
Shot 2 (2-4 s): Outside, an elderly woman raises her hand to knock; smoke curls at her feet.
Shot 3 (4-6 s): The door opens before she knocks; the hostess holds a pierced gold dome burner.
Shot 4 (6-8 s): Match cut on the dome: the perfume bottle's pierced gold dome cap. No smoke.
Audio: ember crackle and footsteps, then a warm oud-inspired instrumental. No voice."""


async def at_gate(tmp_path: Path) -> tuple[Orchestrator, str]:
    orch = Orchestrator(runtime(settings(tmp_path)))
    run = orch.create_run(REQUEST, interactive=True, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)
    commands.choose_concept(orch.rt.storage, run.id, 1)
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL
    return orch, run.id


def test_shots_are_parsed_and_the_diff_names_the_changed_parts() -> None:
    shots = parse_prompt_shots(HUMAN)
    assert [(s.number, s.start_s, s.end_s) for s in shots] == [
        (1, 0, 2), (2, 2, 4), (3, 4, 6), (4, 6, 8),
    ]  # fmt: skip
    edited = HUMAN.replace("raises her hand to knock", "lifts her hand")
    diff = prompt_diff_summary(HUMAN, edited)
    assert diff.startswith("changed Shot 2;") and "unchanged header, Look, Shot 1" in diff


async def test_a_human_prompt_is_priced_again_then_sent_verbatim(tmp_path: Path) -> None:
    orch, run_id = await at_gate(tmp_path)
    rt = orch.rt
    before = rt.storage.runs.load(run_id).generation
    assert before is not None and before.est_usd is not None
    commands.replace_generation_prompt(
        rt.storage, run_id, HUMAN, max_chars=2500, source="p.txt", endcard_tagline="كنا ننتظرك"
    )
    state = rt.storage.runs.load(run_id)
    assert state.stage is Stage.ESTIMATE and state.generation is not None
    assert state.generation.est_usd is None and state.generation.status == "pending"
    state = await orch.run_until_blocked(run_id)  # priced again, and it waits at the gate
    g = state.generation
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL and g is not None
    assert g.prompt_text == HUMAN and not g.approved
    assert g.est_usd == pytest.approx(before.est_usd)  # the price depends on size and length
    assert mock_provider(rt, state).calls == 0
    evs = list(rt.storage.events.tail(run_id, limit=3000))
    direction = [e for e in evs if e.summary.startswith("HUMAN creative direction: prompt")]
    assert len(direction) == 1 and direction[0].type is EventType.DECISION
    assert "changed" in direction[0].payload["diff"]  # type: ignore[operator]
    assert direction[0].payload["new_prompt"] == HUMAN
    assert sum(1 for e in evs if e.summary.startswith("Estimate:")) == 2

    commands.approve_generation(rt.storage, run_id)
    state = await orch.run_until_blocked(run_id)
    assert state.stage is Stage.DONE and state.generation is not None
    provider = mock_provider(rt, state)
    assert provider.calls == 1  # one generation, reserved and settled as before
    assert state.budget.by_category["video"] == pytest.approx(2.4192)
    attempt = state.generation.attempts[-1]
    assert attempt.prompt_used == HUMAN  # the request (and its cache key) uses the human text
    windows = ad_shot_windows(state.generation, 8.0)
    assert "knock" in windows[1].plan  # the critic's windows follow the human shots
    assert state.endcard is not None and state.endcard.tagline == "كنا ننتظرك"
    assert not state.endcard.tagline_placeholder


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (HUMAN.replace("Shot 4 (6-8 s)", "Shot 4 (6-9 s)"), "ends at 9 s, not at 8 s"),
        (HUMAN.replace("Shot 3 (4-6 s)", "Shot 3 (5-6 s)"), "shot 3 must start at 4 s"),
        ("No shots here at all.", "no numbered shots"),
        (HUMAN + " x" * 1500, "the limit is 2500"),
    ],
)
async def test_a_bad_human_prompt_is_refused_and_nothing_changes(
    tmp_path: Path, text: str, message: str
) -> None:
    orch, run_id = await at_gate(tmp_path)
    before = orch.rt.storage.runs.load(run_id)
    with pytest.raises(CommandRejectedError, match=message):
        commands.replace_generation_prompt(orch.rt.storage, run_id, text, max_chars=2500)
    after = orch.rt.storage.runs.load(run_id)
    assert after.state_version == before.state_version and after.generation is not None
    assert after.generation.human_prompt is None


async def test_only_at_the_spend_gate(tmp_path: Path) -> None:
    orch = Orchestrator(runtime(settings(tmp_path)))
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)  # mock + auto: DONE
    with pytest.raises(CommandRejectedError, match="not waiting for a spend approval"):
        commands.replace_generation_prompt(orch.rt.storage, run.id, HUMAN, max_chars=2500)


def test_cli_prompt_file_no_approve_reprices_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, run_id = asyncio.run(at_gate(tmp_path))
    s = orch.rt.settings
    monkeypatch.chdir(tmp_path)  # never the repo's .env: no real key reaches this test
    # the CLI binds logging to its captured stream, closed after invoke: keep it global-free
    monkeypatch.setattr("adsmith.cli.main.configure_logging", lambda *a, **k: None)
    monkeypatch.setenv("ADSMITH_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("ADSMITH_ACCOUNT_DB", str(s.account_db))
    monkeypatch.setenv("ADSMITH_MOCK_LATENCY_S", "0")
    monkeypatch.setenv("ADSMITH_MEDIA_LONG_SIDE", "320")
    f = tmp_path / "human.txt"
    f.write_text(HUMAN, encoding="utf-8")
    res = CliRunner().invoke(app, ["approve", run_id, "--prompt-file", str(f), "--no-approve"])
    assert res.exit_code == 0, res.output
    state = orch.rt.storage.runs.load(run_id)
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL and state.generation is not None
    assert state.generation.prompt_text == HUMAN and not state.generation.approved
    assert "written by a human creative director" in res.output
    bad = CliRunner().invoke(app, ["approve", run_id, "--no-approve"])
    assert bad.exit_code != 0  # --no-approve without a prompt file is refused


async def test_the_critic_judges_against_the_human_shot_list(tmp_path: Path) -> None:
    from adsmith.providers.llm.scripted import ScriptedLLMProvider  # noqa: PLC0415
    from tests.integration.test_ad_critic import verdict_output  # noqa: PLC0415

    orch = Orchestrator(runtime(settings(tmp_path, qa_mode="llm")))
    llm = orch.rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    llm.override("qa_ad_critic", [{"output": verdict_output()}])
    run = orch.create_run(REQUEST, interactive=True, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)
    commands.choose_concept(orch.rt.storage, run.id, 1)
    await orch.run_until_blocked(run.id)
    commands.replace_generation_prompt(orch.rt.storage, run.id, HUMAN, max_chars=2500)
    await orch.run_until_blocked(run.id)
    commands.approve_generation(orch.rt.storage, run.id)
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE
    req = next(r for r in llm.requests if r.template.startswith("qa_ad_critic@"))
    msg = req.messages[0]
    assert hasattr(msg, "parts")
    body = "\n".join(p.text for p in msg.parts if hasattr(p, "text"))
    assert "written by a human creative director" in body
    assert "raises her hand to knock" in body and HUMAN in body

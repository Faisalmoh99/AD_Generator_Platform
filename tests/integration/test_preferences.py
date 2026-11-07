"""ADR-0029 part 3: human decisions at the gates are logged with the options they beat (concept,
tagline, brand, the prompt at the spend gate, the finished ad), and a human can pick one of the
copywriter's lines by number. Zero keys."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adsmith.agent import commands
from adsmith.agent.commands import CommandRejectedError
from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.enums import Stage
from adsmith.storage.preferences import PreferenceOption, PreferenceRecord
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings


async def at_spend_gate(tmp_path: Path) -> tuple[Orchestrator, Any]:
    orch = Orchestrator(runtime(settings(tmp_path)))
    run = orch.create_run(REQUEST, interactive=True, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.AWAITING_CONCEPT_CHOICE
    commands.choose_concept(orch.rt.storage, run.id, 2)
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL
    return orch, state


async def test_a_concept_choice_is_logged_with_the_two_it_beat(tmp_path: Path) -> None:
    orch, state = await at_spend_gate(tmp_path)
    [rec] = orch.rt.storage.preferences.all(run_id=state.id)
    assert rec.kind == "concept" and len(rec.options) == 3
    assert rec.chosen == 1 and rec.recommended == 0  # the judge recommended concept 1
    assert rec.options[0].weighted is not None and "visual_magic" in rec.options[0].scores
    assert "Image:" in rec.options[1].text and rec.context["request"] == REQUEST
    assert len(rec.pairs()) == 2


async def test_a_tagline_and_a_brand_chosen_by_number_reach_the_end_card(tmp_path: Path) -> None:
    orch, state = await at_spend_gate(tmp_path)
    sel = state.copy_selection
    assert sel is not None and sel.tagline_index == 0
    state = commands.choose_copy(orch.rt.storage, state.id, tagline=2, brand=3)
    assert state.copy_selection is not None and state.endcard is not None
    assert state.endcard.tagline == sel.taglines[1].text
    assert state.endcard.brand_name == sel.brands[2].text
    state = commands.choose_copy(orch.rt.storage, state.id, tagline=4)  # code's pick is kept
    recs = [r for r in orch.rt.storage.preferences.all(run_id=state.id) if r.kind == "tagline"]
    assert [(r.chosen, r.recommended) for r in recs] == [(1, 0), (3, 0)]
    assert len(recs[0].options) == 5 and recs[0].options[0].weighted == 8.3
    brand = next(r for r in orch.rt.storage.preferences.all(run_id=state.id) if r.kind == "brand")
    assert brand.chosen == 2
    with pytest.raises(CommandRejectedError, match="between 1 and 5"):
        commands.choose_copy(orch.rt.storage, state.id, tagline=6)


async def test_a_tagline_a_human_writes_beats_all_five(tmp_path: Path) -> None:
    orch, state = await at_spend_gate(tmp_path)
    commands.set_endcard(orch.rt.storage, state.id, tagline="Scent stays after the door.")
    rec = next(r for r in orch.rt.storage.preferences.all(run_id=state.id) if r.kind == "tagline")
    assert rec.written == "Scent stays after the door." and rec.chosen is None
    assert len(rec.pairs()) == 5 and rec.pairs()[0][0] == "Scent stays after the door."


async def test_the_spend_gate_logs_approve_and_decline(tmp_path: Path) -> None:
    orch, state = await at_spend_gate(tmp_path)
    commands.decline_generation(orch.rt.storage, state.id, reason="the turn is too small")
    rec = next(r for r in orch.rt.storage.preferences.all(run_id=state.id) if r.kind == "prompt")
    assert rec.chosen is None and rec.options[0].text.startswith("Image 1 is the product")
    assert rec.pairs() == []

    orch2, state2 = await at_spend_gate(tmp_path / "b")
    commands.approve_generation(orch2.rt.storage, state2.id)
    rec2 = next(r for r in orch2.rt.storage.preferences.all(run_id=state2.id) if r.kind == "prompt")
    assert rec2.chosen == 0 and rec2.context["model"]


def test_pairs_are_preferred_then_rejected() -> None:
    opts = [PreferenceOption(text=t) for t in ("a", "b", "c")]
    assert PreferenceRecord(run_id="r", kind="concept", options=opts, chosen=2).pairs() == [
        ("c", "a"),
        ("c", "b"),
    ]

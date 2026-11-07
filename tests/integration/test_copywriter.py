"""P3 (ADR-0028 part 2): the Arabic copywriter stage between PROPS and ESTIMATE. Five taglines
and three brand names, code checks them, a judge scores them, code weights and picks; the pick is
on the end card and at the gate; a human's brand or tagline always wins. Zero keys."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from adsmith.agent import commands
from adsmith.agent.nodes.copywriter import copywriter
from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.copy import (
    CopyJudgementDraft,
    CopySetDraft,
    copy_dialect,
    line_problems,
    pick,
)
from adsmith.domain.enums import Stage
from adsmith.providers.llm.scripted import FIXTURES_DIR, ScriptedLLMProvider
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings


def fixture(name: str) -> dict[str, Any]:
    raw = json.loads((FIXTURES_DIR / "multishot_en" / f"{name}.json").read_text())
    out: dict[str, Any] = raw[0]["output"]
    return out


async def to_gate(tmp_path: Path, **overrides: list[dict[str, Any]]) -> Any:
    rt = runtime(settings(tmp_path))
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    for name, steps in overrides.items():
        llm.override(name, steps)
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    return orch, llm, state


async def test_five_lines_are_judged_and_code_picks_the_end_card(tmp_path: Path) -> None:
    orch, llm, state = await to_gate(tmp_path)
    sel, card = state.copy_selection, state.endcard
    assert sel is not None and card is not None
    assert llm.calls_for("copywriter") == 1 and llm.calls_for("copy_judge") == 1
    assert len(sel.taglines) == 5 and len(sel.brands) == 3
    # weights .30/.25/.15/.15/.15: line 1 = 8.3; line 3 is generic (penalty 1.5)
    assert sel.tagline == "The last guest leaves first." and sel.brand == "Last Guest"
    assert sel.taglines[0].weighted == 8.3 and sel.taglines[2].generic
    assert card.brand_name == "Last Guest" and card.tagline == "The last guest leaves first."
    assert card.brand_placeholder and not card.tagline_placeholder
    evs = [e.summary for e in orch.rt.storage.events.tail(state.id, limit=3000)]
    assert any(e.startswith("Copy (English): «Last Guest» — «The last guest leaves first.»")
               for e in evs)  # fmt: skip
    pack = orch.rt.storage.artifacts.run_dir(state.id) / "production_pack" / "storyboard.md"
    text = pack.read_text()
    assert "## End-card copy (English)" in text and "(chosen)" in text


async def test_a_line_that_breaks_a_rule_is_sent_back(tmp_path: Path) -> None:
    bad = copy.deepcopy(fixture("copywriter"))
    bad["taglines"][0]["text"] = "The most luxury perfume for the last guest"
    good = fixture("copywriter")
    _, llm, state = await to_gate(tmp_path, copywriter=[{"output": bad}, {"output": good}])
    assert llm.calls_for("copywriter") == 2
    repair = json.dumps(
        [
            str(m)
            for m in [r for r in llm.requests if r.template.startswith("copywriter@")][-1].messages
        ]
    )
    assert "has 8 words; at most 5" in repair and "empty luxury word (luxury)" in repair
    assert state.copy_selection is not None


async def test_a_given_brand_is_kept_and_no_brand_is_written(tmp_path: Path) -> None:
    understand = json.loads((FIXTURES_DIR / "multishot_en" / "understand.json").read_text())
    understand[0]["output"]["brand_name"] = "Dar Al Oud"
    writer = copy.deepcopy(fixture("copywriter"))
    writer["brands"] = []
    judge = copy.deepcopy(fixture("copy_judge"))
    judge["brands"] = []
    _, llm, state = await to_gate(
        tmp_path, understand=understand, copywriter=[{"output": writer}],
        copy_judge=[{"output": judge}],
    )  # fmt: skip
    assert state.endcard is not None and state.endcard.brand_name == "Dar Al Oud"
    assert not state.endcard.brand_placeholder
    assert state.copy_selection is not None and state.copy_selection.brands == []
    asked = json.dumps(
        [
            str(m)
            for m in next(r for r in llm.requests if r.template.startswith("copywriter@")).messages
        ]
    )
    assert "write no brand names" in asked


async def test_with_brand_and_tagline_given_the_stage_costs_nothing(tmp_path: Path) -> None:
    understand = json.loads((FIXTURES_DIR / "multishot_en" / "understand.json").read_text())
    understand[0]["output"] |= {
        "brand_name": "Dar Al Oud", "tagline": "Scent is the last word.", "tagline_language": "en",
    }  # fmt: skip
    _, llm, state = await to_gate(tmp_path, understand=understand)
    assert llm.calls_for("copywriter") == 0 and llm.calls_for("copy_judge") == 0
    assert state.endcard is not None and state.endcard.tagline == "Scent is the last word."


async def test_a_human_tagline_at_the_gate_wins_and_the_brand_stays(tmp_path: Path) -> None:
    orch, _, state = await to_gate(tmp_path)
    commands.set_endcard(orch.rt.storage, state.id, tagline="Honour leaves with you.")
    state = await orch.run_until_blocked(state.id)
    assert state.stage is Stage.DONE and state.endcard is not None
    assert state.endcard.tagline == "Honour leaves with you."
    assert state.endcard.brand_name == "Last Guest"  # the system's brand fills the gap


async def test_the_same_concept_is_never_written_twice(tmp_path: Path) -> None:
    orch, llm, state = await to_gate(tmp_path)
    result = await copywriter(state, orch.rt.node_context(state))
    assert result.llm_calls == [] and result.deltas == []
    assert llm.calls_for("copywriter") == 1


# ----------------------------------------------------------------------------- pure rules


def test_dialect_white_saudi_by_default_hijazi_for_the_hijaz() -> None:
    assert copy_dialect("اعلان لقهوة سعودية مختصة", arabic=True).startswith("white Saudi")
    assert copy_dialect("اعلان لمحمصة في جدة", arabic=True).startswith("Hijazi")
    assert copy_dialect("an ad for a Makkah bakery", arabic=True).startswith("Hijazi")
    assert copy_dialect("an ad for my perfume", arabic=False) == "English"


def test_arabic_lines_are_checked_by_code() -> None:
    rubric = Settings().copy_rubric

    def problems(text: str) -> list[str]:
        return line_problems(text, what="tagline 1", rubric=rubric, arabic=True, max_words=5)

    assert problems("قبل لا تهز الفنجان") == []
    assert any("empty luxury word" in p for p in problems("قهوة بالفخامة كلها"))
    assert any("religious word" in p for p in problems("ما شاء الله على الطعم"))
    assert any("Latin letters" in p for p in problems("قهوة Best"))
    assert any("digits" in p for p in problems("قهوة رقم 1"))
    assert any("real brand" in p for p in problems("قهوة ستاربكس starbucks"))
    assert any("at most 5" in p for p in problems("هذي القهوة اللي تخلي الضيف يطول"))


def test_the_generic_penalty_moves_the_pick() -> None:
    rubric = Settings().copy_rubric
    d = CopySetDraft.model_validate(fixture("copywriter"))
    j = CopyJudgementDraft.model_validate(fixture("copy_judge"))
    sel = pick(d, j, rubric, dialect="English", prompt_hash="h")
    assert sel.tagline_index == 0
    j2 = j.model_copy(deep=True)
    j2.taglines[0].generic = True  # 8.3 - 1.5 = 6.8 < line 5: 7.6
    assert pick(d, j2, rubric, dialect="English", prompt_hash="h").tagline_index == 4

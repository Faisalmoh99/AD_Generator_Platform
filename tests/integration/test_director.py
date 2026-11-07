"""ADR-0026 phase 2: the creative director between PROMPT and ESTIMATE (zero keys).

Code decides what is kept: all rules pass → the writer's prompt stays; a failed rule → round 2,
and the rewrite replaces it (storyboard synced when re-timed); output that never passes the code
checks → the writer's prompt stays with a warning. The same prompt is never reviewed twice and a
human's prompt is never reviewed."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from adsmith.agent import commands
from adsmith.agent.nodes.director import _round_summary
from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.director import DirectorRound, RuleVerdict
from adsmith.domain.enums import Language, Stage
from adsmith.domain.events import EventType
from adsmith.domain.run import RunState
from adsmith.providers.llm.scripted import FIXTURES_DIR, ScriptedLLMProvider
from tests.factories import make_full_run
from tests.integration.test_human_prompt import HUMAN
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings


def fixture() -> dict[str, Any]:
    raw = json.loads((FIXTURES_DIR / "multishot_en" / "director.json").read_text())
    out: dict[str, Any] = raw[0]["output"]
    return out


def failing_round(rewrite: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(fixture())
    out["verdicts"][0] = {
        "rule_id": "turn_withheld",
        "passed": False,
        "note": "Shot 1 shows the bottle being offered: the gift is answered before the turn.",
    }
    out["changes"] = ["Give the turn half a second more."]
    out["rewrite"] = rewrite
    return out


def retimed() -> dict[str, Any]:
    """The writer's fixture with shot 2 shortened and the turn (shot 3) lengthened."""
    r: dict[str, Any] = copy.deepcopy(fixture()["rewrite"])
    r["shots"][1]["end_s"] = 3.5
    r["shots"][2]["start_s"] = 3.5
    r["shots"][2]["action"] = (
        "The host lifts the bottle with both hands, holds it a beat, and offers it; the guest's "
        "open palm rises to take it."
    )
    return r


async def run_to_gate(tmp_path: Path, steps: list[dict[str, Any]] | None = None) -> Any:
    rt = runtime(settings(tmp_path))
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    if steps is not None:
        llm.override("director", steps)
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=True, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    concept = state.concepts.concepts[0].id if state.concepts else ""
    state = await commands_choose(orch, run.id, concept)
    return orch, llm, state


async def commands_choose(orch: Orchestrator, run_id: str, concept_id: str) -> RunState:
    commands.choose_concept(orch.rt.storage, run_id, concept_id)
    return await orch.run_until_blocked(run_id)


def summaries(orch: Orchestrator, run_id: str) -> list[Any]:
    evs = orch.rt.storage.events.tail(run_id, limit=3000)
    return [e for e in evs if e.stage == "director" and e.type is EventType.DECISION]


async def test_all_rules_pass_keeps_the_writers_prompt_in_one_call(tmp_path: Path) -> None:
    orch, llm, state = await run_to_gate(tmp_path)
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL
    r, g = state.director_review, state.generation
    assert r is not None and g is not None and llm.calls_for("director") == 1
    assert r.outcome == "kept" and r.failed == [] and r.after_text == r.before_text
    assert g.prompt.template_version == "ad_prompt_writer@v5"
    evs = summaries(orch, state.id)
    assert evs[0].summary == "Creative director (round 1): all 14 rules pass"
    assert evs[-1].summary.startswith("Creative director: writer's prompt kept")
    req = next(x for x in llm.requests if x.template == "director@v4")
    assert any(type(p).__name__ == "ImagePart" for p in getattr(req.messages[0], "parts", ()))


async def test_a_passing_review_with_a_different_rewrite_still_keeps_the_writer(
    tmp_path: Path,
) -> None:
    """Code decides, not the LLM: no failed rule → no change."""
    out = copy.deepcopy(fixture())
    out["rewrite"] = retimed()
    _, llm, state = await run_to_gate(tmp_path, [{"output": out}])
    assert state.director_review is not None and state.director_review.outcome == "kept"
    assert state.generation is not None and state.generation.prompt.shots[1].end_s == 4
    assert llm.calls_for("director") == 1


async def test_a_failed_rule_rewrites_retimes_and_syncs_the_storyboard(tmp_path: Path) -> None:
    confirm = copy.deepcopy(fixture())
    confirm["rewrite"] = retimed()
    steps = [{"output": failing_round(retimed())}, {"output": confirm}]
    orch, llm, state = await run_to_gate(tmp_path, steps)
    r, g, sb = state.director_review, state.generation, state.storyboard
    assert r is not None and g is not None and sb is not None
    assert llm.calls_for("director") == 2 and [x.round for x in r.rounds] == [1, 2]
    assert r.outcome == "rewritten" and r.retimed and r.failed == ["turn_withheld"]
    assert g.prompt.template_version == "director@v4"
    assert [(s.start_s, s.end_s) for s in g.prompt.shots] == [(0, 2), (2, 3.5), (3.5, 6), (6, 8)]
    assert "Shot 3 (3.5-6 s): The host lifts the bottle with both hands, holds it" in g.prompt.text
    assert [s.duration_s for s in sb.scenes] == [2, 1.5, 2.5, 2]

    # The stored text differs only by the References numbering the PROPS stage added.
    def no_refs(text: str) -> list[str]:
        return [x for x in text.splitlines() if not x.startswith("References:")]

    assert r.before_text != r.after_text and no_refs(r.after_text) == no_refs(g.prompt.text)
    evs = summaries(orch, state.id)
    assert "1 of 14 rules failed (turn_withheld)" in evs[0].summary
    assert evs[-1].summary.startswith("Creative director: director's rewrite used (re-timed")
    # The round-2 request carries the round-1 verdicts.
    second = [x for x in llm.requests if x.template == "director@v4"][-1]
    parts: tuple[Any, ...] = getattr(second.messages[0], "parts", ())
    assert "turn_withheld: FAIL" in parts[-1].text


async def test_a_rewrite_that_never_passes_the_checks_falls_back_with_a_warning(
    tmp_path: Path,
) -> None:
    bad = copy.deepcopy(fixture()["rewrite"])
    bad["shots"][0]["action"] = "A small ember on the floor; match cut to the bottle."
    steps = [{"output": failing_round(bad)}]
    orch, llm, state = await run_to_gate(tmp_path, steps)
    r, g = state.director_review, state.generation
    assert r is not None and g is not None and state.stage is Stage.AWAITING_GENERATION_APPROVAL
    assert r.outcome == "fallback" and r.rounds == [] and "ember" in r.note
    assert g.prompt.template_version == "ad_prompt_writer@v5"
    assert llm.calls_for("director") == 3  # the first answer + 2 repairs, then fallback
    last = summaries(orch, state.id)[-1]
    assert last.level == "warn" and "the rewrite failed the code checks" in last.summary


async def test_round_two_failing_uses_the_round_one_rewrite(tmp_path: Path) -> None:
    bad = copy.deepcopy(fixture())
    bad["verdicts"] = bad["verdicts"][:3]  # not one verdict per rule
    steps = [{"output": failing_round(retimed())}, {"output": bad}]
    _, llm, state = await run_to_gate(tmp_path, steps)
    r, g = state.director_review, state.generation
    assert r is not None and g is not None
    assert r.outcome == "rewritten" and len(r.rounds) == 1 and "round 2 failed" in r.note
    assert g.prompt.shots[1].end_s == 3.5 and llm.calls_for("director") == 4


async def test_the_same_prompt_is_not_reviewed_twice(tmp_path: Path) -> None:
    orch, llm, state = await run_to_gate(tmp_path)
    orch.rt.storage.runs.update(
        state.id, lambda s: setattr(s, "stage", Stage.DIRECTOR), reason="test: back to director"
    )
    state = await orch.run_until_blocked(state.id)
    assert llm.calls_for("director") == 1
    assert "already reviewed" in summaries(orch, state.id)[-1].summary


async def test_a_human_prompt_skips_the_director(tmp_path: Path) -> None:
    orch, llm, state = await run_to_gate(tmp_path)
    commands.replace_generation_prompt(orch.rt.storage, state.id, HUMAN, max_chars=2500)
    state = await orch.run_until_blocked(state.id)
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL
    assert state.generation is not None and state.generation.prompt_text == HUMAN
    assert llm.calls_for("director") == 1


def test_the_trace_follows_the_request_language() -> None:
    state = make_full_run(Stage.DIRECTOR)
    assert state.brief is not None
    state.brief.language = Language.AR
    rnd = DirectorRound(
        round=1,
        verdicts=[RuleVerdict(rule_id="one_location", passed=False, note="two places")],
        summary="s",
    )
    assert _round_summary(state, rnd, 8) == (
        "مراجعة المخرج (الجولة 1): 1 من 8 قواعد لم تتحقق (one_location)، فأُعيدت الكتابة"
    )

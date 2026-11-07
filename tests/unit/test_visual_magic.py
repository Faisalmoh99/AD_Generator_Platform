"""ADR-0029 phase 2: the visual_magic criterion, the judge's shuffled order mapped back to the
concepts, the image read first, and stored verdicts from before the criterion still loading."""

from __future__ import annotations

from pathlib import Path

from adsmith.agent.nodes.select import back_to_concepts, judge_order
from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.concept import ConceptJudgementDraft
from adsmith.domain.enums import Stage
from adsmith.domain.qa import StoredAdRubricScores
from adsmith.domain.rubric import RUBRIC_KEYS
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings


def _score(n: int, v: int) -> dict[str, object]:
    return {"concept_number": n, **dict.fromkeys(RUBRIC_KEYS, v),
            "uses_category_cliche": False, "rationale": "x"}  # fmt: skip


def test_the_order_is_a_stable_permutation_per_run() -> None:
    a = judge_order("run_a", 3)
    assert sorted(a) == [0, 1, 2] and a == judge_order("run_a", 3)
    assert len({tuple(judge_order(f"run_{i}", 3)) for i in range(30)}) > 1


def test_scores_go_back_to_the_concept_they_belong_to() -> None:
    j = ConceptJudgementDraft.model_validate({"scores": [_score(1, 9), _score(2, 5), _score(3, 2)]})
    back = back_to_concepts(j, [2, 0, 1])  # the judge saw concept 3 first
    assert {s.concept_number: s.insight for s in back.scores} == {3: 9, 1: 5, 2: 2}


def test_a_verdict_stored_before_visual_magic_still_loads() -> None:
    old = {k: 6 for k in RUBRIC_KEYS if k != "visual_magic"}
    assert StoredAdRubricScores.model_validate(old).visual_magic is None


async def test_the_judge_reads_the_image_first_in_a_shuffled_order(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path, select_shuffle_scripted=True))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.STORYBOARD)
    llm = rt._provider_override
    asked = str(next(r for r in llm.requests if r.template == "select@v7").messages)  # type: ignore[union-attr]
    assert "visual_magic (Visual magic)" in asked
    order = judge_order(run.id, 3)
    assert state.concepts is not None
    first = state.concepts.concepts[order[0]]
    assert f"Concept 1: {first.title}\\nThe image ({first.device_id})" in asked
    # the fixture's first score (the best) belongs to whichever concept was shown first
    assert state.concepts.selected_id == first.id

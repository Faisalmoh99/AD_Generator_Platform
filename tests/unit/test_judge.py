"""Auto-selection on the creative rubric (ADR-0024): weights as data, cliché penalty, the winner
computed by code with deterministic tie-breaks, and the insight-anchored concept schema."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from adsmith.config import Settings
from adsmith.domain.concept import ConceptJudgementDraft, ConceptSet, CreativeConceptSetDraft
from adsmith.domain.enums import Stage
from adsmith.domain.rubric import RUBRIC_KEYS, Rubric
from adsmith.providers.llm.scripted import FIXTURES_DIR
from tests.unit.test_orchestrator import REQUEST, full_scripts, make_orch, steps


def score(n: int, *, cliche: bool = False, **over: int) -> dict[str, Any]:
    base: dict[str, Any] = dict.fromkeys(RUBRIC_KEYS, 7)
    return base | {"concept_number": n, "uses_category_cliche": cliche, "rationale": "r"} | over


def judge(*scores: dict[str, Any]) -> ConceptJudgementDraft:
    return ConceptJudgementDraft.model_validate({"scores": list(scores)})


@pytest.fixture
def rubric(settings: Settings) -> Rubric:
    return settings.rubric


def test_the_rubric_is_the_six_agreed_criteria_with_the_agreed_weights(rubric: Rubric) -> None:
    assert rubric.weights == {
        "insight": 0.20,
        "hook": 0.20,
        "story_turn": 0.15,
        "originality": 0.15,
        "cultural_meaning": 0.15,
        "product_role": 0.15,
    }
    assert sum(rubric.weights.values()) == pytest.approx(1.0)
    assert "producibility" not in rubric.weights  # ease of execution is not a criterion


def test_rubric_config_is_validated() -> None:
    with pytest.raises(ValidationError, match="sum to 1"):
        Rubric.model_validate(
            {
                "cliche_penalty": 1.5,
                "criteria": {
                    k: {"weight": 0.5, "label": k, "definition": "d", "concept_question": "q",
                        "video_question": "q"}
                    for k in RUBRIC_KEYS
                },
            }
        )  # fmt: skip


def test_weighted_total_and_cliche_penalty(rubric: Rubric) -> None:
    j = judge(score(1), score(2, cliche=True), score(3))
    assert j.scores[0].weighted(rubric) == pytest.approx(7.0)
    assert j.scores[0].weighted(rubric) - j.scores[1].weighted(rubric) == pytest.approx(
        rubric.cliche_penalty
    )


def test_a_flagged_cliche_loses_even_with_better_scores(rubric: Rubric) -> None:
    j = judge(score(1, cliche=True, hook=9, story_turn=9, product_role=9), score(2), score(3))
    assert j.winner(rubric).concept_number == 2


def test_insight_and_hook_carry_the_most_weight(rubric: Rubric) -> None:
    j = judge(score(1, insight=9, hook=9), score(2, originality=9, product_role=9), score(3))
    assert j.winner(rubric).concept_number == 1


def test_tie_breaks_on_insight_then_hook_then_lowest_number(rubric: Rubric) -> None:
    assert judge(score(1), score(2), score(3)).winner(rubric).concept_number == 1
    j = judge(score(1, insight=6, hook=8), score(2), score(3))
    assert j.scores[0].weighted(rubric) == pytest.approx(j.scores[1].weighted(rubric))
    assert j.winner(rubric).concept_number == 2  # same total, higher insight


def test_one_score_per_concept() -> None:
    with pytest.raises(ValidationError, match="exactly one score"):
        judge(score(1), score(1), score(3))


# ----------------------------------------------------------------------------- creative concepts


def creative() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES_DIR / "perfume_en" / "ideate.json").read_text())[0]
    return copy.deepcopy(raw["output"])


def test_a_creative_concept_keeps_story_hook_and_role_on_the_stored_concept() -> None:
    cs = ConceptSet.from_creative(CreativeConceptSetDraft.model_validate(creative()), ["x"])
    c = cs.concepts[0]
    assert c.story is not None and c.hook is not None and c.product_role is not None
    assert c.narrative_arc == [c.story.setup, c.story.turn, c.story.payoff]
    assert c.visual_hook == c.hook.image and c.insight_number == 1
    assert cs.category_cliches == ["x"] and len(c.shot_sketch) >= 3


def test_near_duplicate_creative_concepts_are_repaired() -> None:
    data = creative()
    data["concepts"][2] = dict(data["concepts"][0], title="The Night Drive II")
    with pytest.raises(ValidationError, match="not distinct"):
        CreativeConceptSetDraft.model_validate(data)


def test_product_role_is_a_closed_set() -> None:
    data = creative()
    data["concepts"][0]["product_role"]["role"] = "decoration"
    with pytest.raises(ValidationError):
        CreativeConceptSetDraft.model_validate(data)


# ----------------------------------------------------------------------------- pipeline


async def test_insights_reach_ideate_and_the_rubric_reaches_select(settings: Settings) -> None:
    orch, provider = make_orch(settings, full_scripts())
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.STORYBOARD)
    assert state.insights is not None and len(state.insights.insights) == 3
    assert state.concepts is not None
    assert state.concepts.category_cliches == list(state.insights.category_cliches)

    def text(template: str) -> str:
        req = next(r for r in provider.requests if r.template.startswith(f"{template}@"))
        return "\n".join(p.text for m in req.messages for p in getattr(m, "parts", ()))

    assert state.insights.insights[0].statement in text("ideate")
    assert "story_turn (Story and turn)" in text("select")
    assert "cultural_meaning (Culture as meaning)" in text("select")
    picks = [
        e for e in orch.rt.storage.events.tail(run.id, limit=500)
        if e.stage is Stage.SELECT and "weights" in e.payload
    ]  # fmt: skip
    assert picks and picks[0].payload["weights"] == settings.rubric.weights


async def test_a_concept_pointing_at_a_missing_insight_is_repaired(settings: Settings) -> None:
    bad = copy.deepcopy(steps("ideate"))
    bad[0]["output"]["concepts"][1]["insight_number"] = 5  # only 3 insights exist
    orch, provider = make_orch(settings, full_scripts(ideate=[bad[0], steps("ideate")[0]]))
    run = orch.create_run(REQUEST, interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.STORYBOARD)
    assert state.stage is Stage.STORYBOARD
    names = [r.template for r in provider.requests]
    repair = provider.requests[names.index("ideate@v7") + 1].messages[-1]
    assert "insight_number [5] does not exist" in repair.parts[0].text  # type: ignore[union-attr]

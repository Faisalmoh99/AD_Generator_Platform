"""Auto-selection scoring: weights, cliché penalty, deterministic tie-breaks."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from adsmith.domain.concept import CLICHE_PENALTY, JUDGE_WEIGHTS, ConceptJudgementDraft


def score(n: int, *, cliche: bool = False, **over: int) -> dict[str, Any]:
    base: dict[str, Any] = {
        "concept_number": n,
        "brief_fit": 7,
        "originality": 7,
        "producibility": 7,
        "product_hero": 7,
        "cultural_fit": 7,
        "uses_category_cliche": cliche,
        "rationale": "r",
    }
    return base | over


def judge(*scores: dict[str, Any]) -> ConceptJudgementDraft:
    return ConceptJudgementDraft.model_validate({"scores": list(scores)})


def test_weights_sum_to_one_and_originality_matters() -> None:
    assert sum(JUDGE_WEIGHTS.values()) == pytest.approx(1.0)
    assert JUDGE_WEIGHTS["originality"] >= 0.2


def test_cliche_penalty_applies() -> None:
    j = judge(score(1), score(2, cliche=True), score(3))
    assert j.scores[0].weighted() - j.scores[1].weighted() == pytest.approx(CLICHE_PENALTY)


def test_flagged_cliche_loses_even_with_better_scores() -> None:
    """The perfume case: the judge liked concept 1 but itself called it a cliché."""
    j = judge(
        score(1, cliche=True, brief_fit=9, producibility=9, product_hero=9, cultural_fit=9),
        score(2, originality=9),
        score(3),
    )
    assert j.winner().concept_number == 2


def test_tie_breaks_on_brief_fit_then_lowest_number() -> None:
    assert judge(score(1), score(2), score(3)).winner().concept_number == 1
    j = judge(score(1, brief_fit=6, originality=8), score(2), score(3))
    assert j.scores[0].weighted() == j.scores[1].weighted()
    assert j.winner().concept_number == 2


def test_one_score_per_concept() -> None:
    with pytest.raises(ValidationError, match="exactly one score"):
        judge(score(1), score(1), score(3))

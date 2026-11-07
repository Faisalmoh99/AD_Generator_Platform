"""select: the LLM scores each concept on the creative rubric (config/rubric.toml: insight, hook,
story and turn, originality, culture as meaning, product's role, visual magic) and code computes
the winner (ADR-0024, ADR-0029). Ease of execution is not a criterion. Auto mode selects the
winner; interactive mode only records the scores and a recommendation (the human picks).

Position bias is the best-documented bias of LLM judges, so the judge sees the concepts in a
per-run shuffled order and code maps every score back to the concept it belongs to; each concept
opens with its one image, before the prose (style outweighs substance in LLM judging)."""

from __future__ import annotations

import hashlib
import random

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SelectConcept
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import brief_block, concept_block, decision, language_name
from adsmith.config import LLMNode
from adsmith.domain.concept import ConceptJudgementDraft, ConceptScore, is_reused
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState


def judge_order(run_id: str, n: int) -> list[int]:
    """The order (0-based concept indices) the judge sees: a shuffle seeded by the run id, so a
    resumed run shows the same order and a test can predict it."""
    order = list(range(n))
    seed = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8], "big")
    random.Random(seed).shuffle(order)
    return order


def back_to_concepts(judgement: ConceptJudgementDraft, order: list[int]) -> ConceptJudgementDraft:
    """The judge's numbers are positions in ``order``; each score goes back to its concept."""
    scores = [
        s.model_copy(update={"concept_number": order[s.concept_number - 1] + 1})
        for s in judgement.scores
    ]
    return ConceptJudgementDraft(scores=sorted(scores, key=lambda s: s.concept_number))


async def select(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None and state.concepts is not None
    concepts = state.concepts.concepts
    rubric = ctx.settings.rubric
    shuffle = state.config.llm_provider != "scripted" or ctx.settings.select_shuffle_scripted
    order = judge_order(state.id, len(concepts)) if shuffle else list(range(len(concepts)))
    prompt = ctx.prompts.get("select").render(
        brief=brief_block(state.brief),
        rubric=rubric.prompt_block("concept"),
        concepts="\n\n".join(
            concept_block(concepts[j], i, image_first=True) for i, j in enumerate(order, start=1)
        ),
        user_language=language_name(state.brief.language),
        cliches="\n".join(f"- {c}" for c in state.concepts.category_cliches) or "(none listed)",
    )
    result = await ctx.llm.call(LLMNode.SELECT, prompt, ConceptJudgementDraft)
    judgement = back_to_concepts(result.value, order)
    # Novelty by code (ADR-0028, ADR-0029): a concept that stands on the same people, place and
    # object as a recent winner, or on a device one of the last three used, loses the penalty.
    penalties = {
        i: rubric.novelty_penalty
        for i, c in enumerate(concepts, start=1)
        if rubric.novelty_penalty and is_reused(c.motifs, c.device_id, ctx.motif_ledger)
    }

    def total(s: ConceptScore) -> float:
        return round(s.weighted(rubric) - penalties.get(s.concept_number, 0.0), 4)

    best = judgement.winner(rubric, penalties)
    chosen = concepts[best.concept_number - 1]
    ranking = sorted(judgement.scores, key=lambda s: -total(s))
    verb = "Recommends" if state.config.interactive else "Selected"
    summary = (
        f"{verb} concept {best.concept_number} '{chosen.title}' "
        f"(creative score {total(best):.2f}/10): "
        + ", ".join(
            f"#{s.concept_number}={total(s):.2f}"
            + (f" (cliché −{rubric.cliche_penalty:g})" if s.uses_category_cliche else "")
            + (
                f" (recent motifs −{rubric.novelty_penalty:g})"
                if s.concept_number in penalties
                else ""
            )
            for s in ranking
        )
    )
    deltas = (
        []
        if state.config.interactive
        else [
            SelectConcept(
                concept_id=chosen.id,
                by="agent",
                rationale=f"{best.rationale} (creative score {total(best):.2f}/10)",
            )
        ]
    )
    return NodeResult(
        deltas=list(deltas),
        events=[
            decision(
                ctx.run_id,
                Stage.SELECT,
                summary,
                weights=rubric.weights,
                scores=[
                    s.model_dump()
                    | {"weighted": total(s), "novelty_penalty": penalties.get(s.concept_number, 0)}
                    for s in judgement.scores
                ],
                recommended=best.concept_number,
                judge_order=[j + 1 for j in order],
            )
        ],
        llm_calls=result.calls,
    )

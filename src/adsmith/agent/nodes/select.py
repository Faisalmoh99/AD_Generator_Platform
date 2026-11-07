"""select: the LLM scores each concept on the creative rubric (config/rubric.toml: insight, hook,
story and turn, originality, culture as meaning, product's role) and code computes the winner
(ADR-0024). Ease of execution is not a criterion. Auto mode selects the winner; interactive mode
only records the scores and a recommendation (the human picks)."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SelectConcept
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import brief_block, concept_block, decision, language_name
from adsmith.config import LLMNode
from adsmith.domain.concept import (
    REUSED_MOTIFS,
    ConceptJudgementDraft,
    ConceptScore,
    recent_overlap,
)
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState


async def select(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None and state.concepts is not None
    concepts = state.concepts.concepts
    rubric = ctx.settings.rubric
    prompt = ctx.prompts.get("select").render(
        brief=brief_block(state.brief),
        rubric=rubric.prompt_block("concept"),
        concepts="\n\n".join(concept_block(c, i) for i, c in enumerate(concepts, start=1)),
        user_language=language_name(state.brief.language),
        cliches="\n".join(f"- {c}" for c in state.concepts.category_cliches) or "(none listed)",
    )
    result = await ctx.llm.call(LLMNode.SELECT, prompt, ConceptJudgementDraft)
    judgement = result.value
    # Novelty by code (ADR-0028): a concept that stands on the same people, place and object as
    # a recent winner (the motif ledger) loses the rubric's novelty penalty.
    penalties = {
        i: rubric.novelty_penalty
        for i, c in enumerate(concepts, start=1)
        if rubric.novelty_penalty and recent_overlap(c.motifs, ctx.motif_ledger) >= REUSED_MOTIFS
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
            )
        ],
        llm_calls=result.calls,
    )

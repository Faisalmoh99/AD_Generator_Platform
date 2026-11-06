"""select: the LLM scores each concept on a fixed rubric and code computes the winner. Auto mode
selects it; interactive mode only records the scores and a recommendation (the human picks)."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SelectConcept
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import (
    brief_block,
    concept_block,
    decision,
    language_name,
    profile_block_for,
)
from adsmith.config import LLMNode
from adsmith.domain.concept import CLICHE_PENALTY, JUDGE_WEIGHTS, ConceptJudgementDraft
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState


async def select(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None and state.concepts is not None
    concepts = state.concepts.concepts
    prompt = ctx.prompts.get("select").render(
        brief=brief_block(state.brief),
        video_profile=profile_block_for(ctx.settings, state),
        concepts="\n\n".join(concept_block(c, i) for i, c in enumerate(concepts, start=1)),
        user_language=language_name(state.brief.language),
        cliches="\n".join(f"- {c}" for c in state.concepts.category_cliches) or "(none listed)",
    )
    result = await ctx.llm.call(LLMNode.SELECT, prompt, ConceptJudgementDraft)
    judgement = result.value
    best = judgement.winner()
    chosen = concepts[best.concept_number - 1]
    ranking = sorted(judgement.scores, key=lambda s: -s.weighted())
    verb = "Recommends" if state.config.interactive else "Selected"
    summary = (
        f"{verb} concept {best.concept_number} '{chosen.title}' "
        f"(score {best.weighted():.2f}/10): "
        + ", ".join(
            f"#{s.concept_number}={s.weighted():.2f}"
            + (f" (cliché −{CLICHE_PENALTY:g})" if s.uses_category_cliche else "")
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
                rationale=f"{best.rationale} (weighted {best.weighted():.2f}/10)",
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
                weights=dict(JUDGE_WEIGHTS),
                scores=[s.model_dump() | {"weighted": s.weighted()} for s in judgement.scores],
                recommended=best.concept_number,
            )
        ],
        llm_calls=result.calls,
    )

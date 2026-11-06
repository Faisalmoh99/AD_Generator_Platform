"""ideate: Brief + insights → exactly 3 distinct concepts, each standing on one insight with a
story (setup, turn, payoff), a hook, the product's role and the cultural meaning (ideate v6,
ADR-0024). Distinctness and the insight reference are checked by code (→ schema repair)."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetConcepts
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import (
    brief_block,
    decision,
    insights_block,
    language_name,
    profile_block_for,
)
from adsmith.config import LLMNode
from adsmith.domain.concept import ConceptSet, CreativeConceptSetDraft
from adsmith.domain.enums import Stage
from adsmith.domain.formats import format_block
from adsmith.domain.run import RunState
from adsmith.domain.similarity import pairwise_similarities


async def ideate(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None  # guaranteed by RunState stage prerequisites
    found = state.insights
    cliches = list(found.category_cliches) if found else []
    prompt = ctx.prompts.get("ideate").render(
        brief=brief_block(state.brief),
        insights=insights_block(found),
        cliches="\n".join(f"- {c}" for c in cliches) or "(none listed)",
        user_language=language_name(state.brief.language),
        duration=f"{state.brief.duration_s:g}",
        video_profile=profile_block_for(ctx.settings, state),
        format_block=format_block(ctx.settings.formats, state.brief.format),
    )
    count = len(found.insights) if found else 5

    def convert(d: CreativeConceptSetDraft) -> ConceptSet:
        bad = [c.insight_number for c in d.concepts if c.insight_number > count]
        if bad:
            raise ValueError(
                f"insight_number {bad} does not exist: there are {count} insights (1..{count})"
            )
        return ConceptSet.from_creative(d, cliches)

    result = await ctx.llm.call_and_convert(
        LLMNode.IDEATE, prompt, CreativeConceptSetDraft, convert
    )
    concepts = result.value
    sims = pairwise_similarities([c.signature_text() for c in concepts.concepts])
    roles = ", ".join(
        f"{c.title} ({c.product_role.role if c.product_role else '-'}, insight "
        f"{c.insight_number or '-'})"
        for c in concepts.concepts
    )
    event = decision(
        ctx.run_id,
        Stage.IDEATE,
        f"Concepts: {roles} (max pairwise similarity {max(sims.values()):.0%})",
        similarities={f"{i + 1}-{j + 1}": round(s, 3) for (i, j), s in sims.items()},
        category_cliches=cliches,
    )
    return NodeResult(
        deltas=[SetConcepts(concepts=concepts)], events=[event], llm_calls=result.calls
    )

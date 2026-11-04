"""ideate: Brief → exactly 3 distinct concepts (distinctness enforced by the schema validator)."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetConcepts
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import brief_block, decision, language_name
from adsmith.config import LLMNode
from adsmith.domain.concept import ConceptSet, ConceptSetDraft
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState
from adsmith.domain.similarity import pairwise_similarities


async def ideate(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None  # guaranteed by RunState stage prerequisites
    prompt = ctx.prompts.get("ideate").render(
        brief=brief_block(state.brief),
        user_language=language_name(state.brief.language),
        duration=f"{state.brief.duration_s:g}",
    )
    result = await ctx.llm.call_and_convert(
        LLMNode.IDEATE, prompt, ConceptSetDraft, ConceptSet.from_draft
    )
    concepts = result.value
    sims = pairwise_similarities([c.signature_text() for c in concepts.concepts])
    event = decision(
        ctx.run_id,
        Stage.IDEATE,
        "Concepts: "
        + " | ".join(c.title for c in concepts.concepts)
        + f" (max pairwise similarity {max(sims.values()):.0%}); avoided clichés: "
        + "; ".join(concepts.category_cliches),
        similarities={f"{i + 1}-{j + 1}": round(s, 3) for (i, j), s in sims.items()},
        category_cliches=list(concepts.category_cliches),
    )
    return NodeResult(
        deltas=[SetConcepts(concepts=concepts)], events=[event], llm_calls=result.calls
    )

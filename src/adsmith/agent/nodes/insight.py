"""insight: Brief → the category's clichés + 3-5 human truths the concepts must stand on
(creative layer, ADR-0024). The concepts are written on these truths in the next stage."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetInsights
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import brief_block, decision, language_name
from adsmith.config import LLMNode
from adsmith.domain.concept import InsightSet, InsightSetDraft
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState


async def insight(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None
    prompt = ctx.prompts.get("insight").render(
        brief=brief_block(state.brief), user_language=language_name(state.brief.language)
    )
    result = await ctx.llm.call_and_convert(
        LLMNode.INSIGHT, prompt, InsightSetDraft, lambda d: InsightSet(**d.model_dump())
    )
    found = result.value
    summary = "Insights: " + " | ".join(
        f"{i}. {x.statement}" for i, x in enumerate(found.insights, 1)
    )
    return NodeResult(
        deltas=[SetInsights(insights=found)],
        events=[
            decision(
                ctx.run_id,
                Stage.INSIGHT,
                summary,
                insights=[x.statement for x in found.insights],
                category_cliches=list(found.category_cliches),
            )
        ],
        llm_calls=result.calls,
    )

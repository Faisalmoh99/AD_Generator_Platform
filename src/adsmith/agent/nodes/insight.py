"""insight: Brief → the category's clichés + 3-5 human truths the concepts must stand on
(creative layer, ADR-0024). The concepts are written on these truths in the next stage."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetInsights
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import brief_block, decision, language_name
from adsmith.config import LLMNode
from adsmith.domain.concept import InsightSet, InsightSetDraft
from adsmith.domain.culture_bank import NO_ANCHOR
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState


async def insight(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None
    bank = ctx.settings.culture_bank
    entries = bank.for_brief(state.brief)
    prompt = ctx.prompts.get("insight").render(
        brief=brief_block(state.brief),
        user_language=language_name(state.brief.language),
        culture_bank=bank.prompt_block(entries),
    )
    allowed = {e.id for e in entries} | {NO_ANCHOR}

    def convert(d: InsightSetDraft) -> InsightSet:
        bad = [x.culture_anchor for x in d.insights if x.culture_anchor not in allowed]
        if bad:
            raise ValueError(
                f"culture_anchor {bad} is not in the culture bank given; use one of "
                f"{sorted(allowed)} or 'none'"
            )
        return InsightSet.from_draft(d)

    result = await ctx.llm.call_and_convert(LLMNode.INSIGHT, prompt, InsightSetDraft, convert)
    found = result.value
    summary = "Insights: " + " | ".join(
        f"{i}. {x.statement}" + (f" [{x.culture_anchor}]" if x.culture_anchor != NO_ANCHOR else "")
        for i, x in enumerate(found.insights, 1)
    )
    return NodeResult(
        deltas=[SetInsights(insights=found)],
        events=[
            decision(
                ctx.run_id,
                Stage.INSIGHT,
                summary,
                insights=[x.statement for x in found.insights],
                culture_anchors=[x.culture_anchor for x in found.insights],
                culture_bank_offered=[e.id for e in entries],
                category_cliches=list(found.category_cliches),
            )
        ],
        llm_calls=result.calls,
    )

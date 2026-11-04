"""understand: raw request (EN / MSA / Saudi dialect) → typed Brief."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetBrief
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import decision
from adsmith.config import LLMNode
from adsmith.domain.brief import Brief, BriefDraft
from adsmith.domain.enums import Dialect, Language, Stage
from adsmith.domain.run import RunState
from adsmith.domain.similarity import detect_language


async def understand(state: RunState, ctx: NodeContext) -> NodeResult:
    prompt = ctx.prompts.get("understand").render(
        request=state.request,
        default_duration=f"{state.config.target_duration_s:g}",
    )
    result = await ctx.llm.call_and_convert(
        LLMNode.UNDERSTAND,
        prompt,
        BriefDraft,
        lambda d: Brief.from_draft(
            d, raw_request=state.request, default_aspect=state.config.aspect_ratio
        ),
    )
    brief = result.value
    events = []

    # Deterministic cross-check: the script the user typed in decides the reply language.
    detected = Language(detect_language(state.request))
    if detected is not brief.language:
        dialect = Dialect.NONE if detected is Language.EN else Dialect.MSA
        events.append(
            decision(
                ctx.run_id,
                Stage.UNDERSTAND,
                f"Overrode language {brief.language.value}→{detected.value}: the request is "
                f"written in {detected.value} script.",
                llm_language=brief.language.value,
                detected=detected.value,
            )
        )
        brief = Brief.model_validate(
            brief.model_dump() | {"language": detected, "dialect": dialect}
        )

    events.append(
        decision(
            ctx.run_id,
            Stage.UNDERSTAND,
            f"Brief: {brief.product} for {brief.target_audience}, {brief.duration_s:g}s "
            f"{brief.aspect_ratio}, {brief.language.value}/{brief.dialect.value}; "
            f"{len(brief.assumptions)} assumption(s).",
            assumptions=list(brief.assumptions),
        )
    )
    return NodeResult(deltas=[SetBrief(brief=brief)], events=events, llm_calls=result.calls)

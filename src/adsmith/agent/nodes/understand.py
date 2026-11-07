"""understand: raw request (EN / MSA / Saudi dialect) → typed Brief."""

from __future__ import annotations

from adsmith.agent.context import NodeContext, ad_endpoint
from adsmith.agent.deltas import SetBrief
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import decision
from adsmith.config import LLMNode
from adsmith.domain.brief import Brief, BriefDraft
from adsmith.domain.dialect import corrected_dialect
from adsmith.domain.enums import Dialect, Language, Stage
from adsmith.domain.formats import AdFormat, resolve_format
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

    fix = corrected_dialect(state.request, brief.dialect) if brief.language is Language.AR else None
    if fix is not None:
        dialect, cues = fix
        events.append(
            decision(
                ctx.run_id,
                Stage.UNDERSTAND,
                f"Corrected dialect {brief.dialect.value}→{dialect.value} from cues {cues}.",
                llm_dialect=brief.dialect.value,
                cues=cues,
            )
        )
        brief = Brief.model_validate(brief.model_dump() | {"dialect": dialect})

    fmt, why = resolve_format(
        ctx.settings.formats,
        requested=brief.requested_ad_format,
        requested_shots=brief.requested_shots,
        physical_product=brief.physical_product,
        forced=state.config.forced_format,
        has_photo=state.product_photo is not None,
    )
    update: dict[str, object] = {"format": fmt}
    if fmt is AdFormat.MULTISHOT:
        # One generation renders the whole ad: whole seconds, inside the model's range.
        ep = ad_endpoint(ctx.settings, state.config.ad_model, state.config.ad_provider)
        fitted = float(min(max(round(brief.duration_s), ep.min_duration_s), ep.max_duration_s))
        if fitted != brief.duration_s:
            events.append(
                decision(
                    ctx.run_id,
                    Stage.UNDERSTAND,
                    f"Duration {brief.duration_s:g}s → {fitted:g}s: one {ep.endpoint} generation "
                    f"renders whole seconds from {ep.min_duration_s} to {ep.max_duration_s} s.",
                )
            )
            update["duration_s"] = fitted
        g = ctx.settings.formats.grammar(fmt)
        asked = brief.requested_shots
        if asked is not None and not g.min_shots <= asked <= g.max_shots:
            events.append(
                decision(
                    ctx.run_id,
                    Stage.UNDERSTAND,
                    f"Requested {asked} shots, but one generation holds {g.min_shots}-"
                    f"{g.max_shots}: the storyboard chooses the count in that range.",
                )
            )
            update["requested_shots"] = None
    brief = Brief.model_validate(brief.model_dump() | update)
    events.append(
        decision(
            ctx.run_id,
            Stage.UNDERSTAND,
            f"Brief: {brief.product} for {brief.target_audience}, {brief.duration_s:g}s "
            f"{brief.aspect_ratio}, {brief.language.value}/{brief.dialect.value}; "
            f"{len(brief.assumptions)} assumption(s). Format: {fmt.value} ({why}).",
            assumptions=list(brief.assumptions),
            format=fmt.value,
            format_why=why,
        )
    )
    return NodeResult(deltas=[SetBrief(brief=brief)], events=events, llm_calls=result.calls)

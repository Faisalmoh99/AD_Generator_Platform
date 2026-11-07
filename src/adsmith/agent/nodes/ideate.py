"""ideate: concepts on the insights. On a whole-ad run it writes six candidates on six axes code
assigns, and code keeps the three that differ most, away from recent winners' motifs (ADR-0028
part 3); other formats write three. The LLM never chooses which survive. Each concept is shown
with one visual device from the library (ADR-0029): code checks the id, that the candidates use
different devices, that no concept copies a worked exemplar, and on a whole-ad run that the shot
sketch has as many lines as the film has shots."""

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
from adsmith.domain.concept import (
    AXES,
    CONCEPT_COUNT,
    ConceptSet,
    CreativeConceptSetDraft,
    MotifEntry,
    assign_axes,
    is_reused,
    pick_distinct,
)
from adsmith.domain.enums import Stage
from adsmith.domain.formats import AdFormat, format_block, whole_ad_shot_count
from adsmith.domain.run import RunState
from adsmith.domain.similarity import pairwise_similarities
from adsmith.domain.visual_devices import DeviceLibrary


def ledger_block(ledger: tuple[MotifEntry, ...]) -> str:
    if not ledger:
        return "(no recent winners recorded)"
    return "\n".join(
        f"- {e.title} [{e.axis}]: {', '.join(e.motifs)}"
        + (f" (device: {e.device})" if e.device else "")
        for e in ledger
    )


def device_problems(
    d: CreativeConceptSetDraft, allowed: list[str], shots: int | None, library: DeviceLibrary
) -> list[str]:
    """Code checks on the visual devices (ADR-0029), sent back through schema repair."""
    out: list[str] = []
    for i, c in enumerate(d.concepts, 1):
        if c.device_id not in allowed:
            out.append(
                f"concept {i}: device_id {c.device_id!r} is not one of the listed devices "
                f"for this product ({', '.join(allowed)})"
            )
        if c.device_shot > len(c.shot_sketch):
            out.append(
                f"concept {i}: device_shot {c.device_shot} is past its {len(c.shot_sketch)} "
                "shot_sketch lines"
            )
        if shots is not None and len(c.shot_sketch) != shots:
            out.append(
                f"concept {i}: the film has exactly {shots} shots, so write exactly {shots} "
                f"shot_sketch lines, not {len(c.shot_sketch)} (let the device carry a beat "
                "the shots cannot)"
            )
        copied = library.copied_exemplar(c.motifs)
        if copied is not None:
            out.append(
                f"concept {i} copies the worked example {copied.id!r} (motifs "
                f"{', '.join(c.motifs)}): examples show the quality bar, never the content; "
                "change who, where and what object"
            )
    used = [c.device_id for c in d.concepts]
    if len(allowed) >= len(used) and len(set(used)) != len(used):
        out.append(
            f"each concept uses a different device (got {', '.join(used)}): spread the "
            "ideas over the devices, not only the axes"
        )
    return out


async def ideate(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None  # guaranteed by RunState stage prerequisites
    found = state.insights
    cliches = list(found.category_cliches) if found else []
    bank = ctx.settings.culture_bank
    whole = state.brief.format is AdFormat.MULTISHOT
    n = ctx.settings.ideate_candidates if whole else CONCEPT_COUNT
    themes = [
        e.theme
        for x in (found.insights if found else [])
        if (e := bank.get(x.culture_anchor)) is not None
    ]
    axes = assign_axes(themes, n)
    library = ctx.settings.visual_devices
    devices = library.for_categories(bank.categories_for(state.brief))
    allowed = [d.id for d in devices]
    shots = (
        whole_ad_shot_count(
            ctx.settings.formats.grammar(state.brief.format), state.brief.duration_s
        )
        if whole
        else None
    )
    prompt = ctx.prompts.get("ideate").render(
        brief=brief_block(state.brief),
        insights=insights_block(found, bank),
        cliches="\n".join(f"- {c}" for c in cliches) or "(none listed)",
        user_language=language_name(state.brief.language),
        duration=f"{state.brief.duration_s:g}",
        video_profile=profile_block_for(ctx.settings, state),
        format_block=format_block(ctx.settings.formats, state.brief.format),
        count=str(n),
        axes="\n".join(f"{i}. {a}: {AXES[a]}" for i, a in enumerate(axes, 1)),
        recent=ledger_block(ctx.motif_ledger),
        devices=library.prompt_block(devices),
        exemplars=library.exemplars_block(),
        shots=str(shots) if shots else "",
    )
    count = len(found.insights) if found else 5
    kept: list[int] = []

    def convert(d: CreativeConceptSetDraft) -> ConceptSet:
        problems = []
        if len(d.concepts) != n:
            problems.append(f"write exactly {n} concepts, one per axis, not {len(d.concepts)}")
        bad = [c.insight_number for c in d.concepts if c.insight_number > count]
        if bad:
            problems.append(
                f"insight_number {bad} does not exist: there are {count} insights (1..{count})"
            )
        got = sorted(c.axis for c in d.concepts)
        if got != sorted(axes):
            problems.append(f"use each axis exactly once: {', '.join(axes)} (got {', '.join(got)})")
        problems += device_problems(d, allowed, shots, library)
        if problems:
            raise ValueError("; ".join(problems))
        kept[:] = pick_distinct(
            [c.to_concept() for c in d.concepts],
            [c.motifs for c in d.concepts],
            ctx.motif_ledger,
            devices=[c.device_id for c in d.concepts],
        )
        return ConceptSet.from_creative(d, cliches, keep=kept)

    result = await ctx.llm.call_and_convert(
        LLMNode.IDEATE, prompt, CreativeConceptSetDraft, convert
    )
    concepts = result.value
    sims = pairwise_similarities([c.signature_text() for c in concepts.concepts])
    roles = ", ".join(
        f"{c.title} ({c.axis or '-'}, {c.device_id or '-'}, "
        f"{c.product_role.role if c.product_role else '-'}, insight {c.insight_number or '-'})"
        for c in concepts.concepts
    )
    events = [
        decision(
            ctx.run_id,
            Stage.IDEATE,
            f"Concepts: {roles} (max pairwise similarity {max(sims.values()):.0%})",
            similarities={f"{i + 1}-{j + 1}": round(s, 3) for (i, j), s in sims.items()},
            category_cliches=cliches,
        )
    ]
    if n > CONCEPT_COUNT:
        dropped = [i + 1 for i in range(n) if i not in kept]
        events.append(
            decision(
                ctx.run_id,
                Stage.IDEATE,
                f"Diversity: {n} concepts written on axes {', '.join(axes)}; code kept "
                f"{[i + 1 for i in kept]} (least alike, fresh motifs first); dropped {dropped}",
                axes=axes,
                kept=[i + 1 for i in kept],
                ledger=[e.model_dump() for e in ctx.motif_ledger],
                devices=[c.device_id for c in concepts.concepts],
                reused_motifs=[
                    c.title
                    for c in concepts.concepts
                    if is_reused(c.motifs, c.device_id, ctx.motif_ledger)
                ],
            )
        )
    return NodeResult(
        deltas=[SetConcepts(concepts=concepts)], events=events, llm_calls=result.calls
    )

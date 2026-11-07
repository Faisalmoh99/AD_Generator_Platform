"""prompt: one provider-agnostic generation prompt per scene that needs one.

Concurrency vs. caching: the shared CONTEXT (bible + concept + storyboard) is identical for every
scene. The first scene is written alone, so it *writes* the prompt cache, then the remaining scenes
run in parallel and *read* it: one cache write and N-1 reads, at roughly two calls of latency.
Failures are isolated: a scene whose prompt can't be produced is marked failed (with its calls
booked); the others proceed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from adsmith.agent.context import NodeContext, ad_endpoint, ad_model_key, render_window
from adsmith.agent.deltas import Delta, FailScenes, SetAdGeneration, SetScenePrompts
from adsmith.agent.i18n import failure_message
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import (
    brief_block,
    concept_block,
    decision,
    scene_block,
    storyboard_block,
)
from adsmith.agent.nodes.photo import product_photo_parts
from adsmith.agent.recovery.errors import AgentError
from adsmith.agent.structured import StructuredResult
from adsmith.config import LLMNode
from adsmith.domain.ad import (
    AdGeneration,
    AdPrompt,
    AdPromptDraft,
    ad_prompt_problems,
    identity_line,
    render_ad_prompt,
)
from adsmith.domain.continuity import ContinuityBible
from adsmith.domain.creative_rules import creative_rule_problems
from adsmith.domain.culture_bank import with_gulf_objects
from adsmith.domain.enums import Stage
from adsmith.domain.formats import AdFormat
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene, SceneFailure, ScenePrompt, ScenePromptDraft
from adsmith.domain.shot_lint import negative_contradictions
from adsmith.domain.storyboard import Storyboard
from adsmith.domain.vocabulary import entry_block


def _neighbours(sb: Storyboard, scene: Scene) -> str:
    prev, nxt = sb.neighbors(scene.id)
    parts = []
    if prev:
        parts.append(
            f"Previous shot (scene {prev.order}): {prev.shot.subject_action}; "
            f"{prev.shot.time_of_day.value}; {prev.shot.lighting}"
        )
    if nxt:
        parts.append(
            f"Next shot (scene {nxt.order}): {nxt.shot.subject_action}; "
            f"{nxt.shot.time_of_day.value}; {nxt.shot.lighting}"
        )
    return "\n".join(parts) or "(single scene)"


async def prompt(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.storyboard is not None
    sb = state.storyboard
    if ctx.settings.formats.grammar(sb.format).generation == "whole":
        return await _whole_ad_prompt(state, ctx)
    todo = [s for s in sb.scenes if s.prompt is None and s.failure is None]

    async def write(scene: Scene) -> StructuredResult[ScenePrompt]:
        return await write_scene_prompt(state, ctx, scene)

    outcomes: list[StructuredResult[ScenePrompt] | BaseException] = []
    if todo:
        first = await asyncio.gather(write(todo[0]), return_exceptions=True)
        rest = await asyncio.gather(*(write(s) for s in todo[1:]), return_exceptions=True)
        outcomes = [*first, *rest]

    prompts: dict[str, ScenePrompt] = {}
    failures: dict[str, SceneFailure] = {}
    calls: list[LLMCallRecord] = []
    for scene, outcome in zip(todo, outcomes, strict=True):
        if isinstance(outcome, AgentError):
            calls.extend(outcome.llm_calls)
            failures[scene.id] = SceneFailure(
                kind=outcome.kind,
                reason_user=failure_message(outcome.kind, Stage.PROMPT, state.language),
                reason_debug=outcome.message,
            )
        elif isinstance(outcome, BaseException):
            raise outcome  # a bug, not a provider failure: the orchestrator records it
        else:
            prompts[scene.id] = outcome.value
            calls.extend(outcome.calls)

    deltas: list[Delta] = []
    if prompts:
        deltas.append(
            SetScenePrompts(
                prompts=prompts, for_versions={sid: sb.by_id(sid).version for sid in prompts}
            )
        )
    if failures:
        deltas.append(FailScenes(failures=failures))
    summary = f"Wrote prompts for {len(prompts)}/{len(todo)} scene(s)"
    if failures:
        summary += f"; {len(failures)} failed and were isolated"
    return NodeResult(
        deltas=deltas, events=[decision(ctx.run_id, Stage.PROMPT, summary)], llm_calls=calls
    )


async def write_scene_prompt(
    state: RunState, ctx: NodeContext, scene: Scene
) -> StructuredResult[ScenePrompt]:
    """One scene's prompt (also used by the scene runner when a shot falls back to another
    vocabulary entry mid-generation). ``scene`` may be a candidate copy not yet in ``state``."""
    assert state.brief and state.concepts and state.storyboard and state.bible
    concept = state.concepts.selected
    assert concept is not None
    sb, bible = state.storyboard, state.bible
    template = ctx.prompts.get("prompt_writer")
    render_s, visible_s = render_window(ctx.settings, state, scene.duration_s)
    rendered = template.render(
        bible=bible.prompt_block(),
        concept=concept_block(concept),
        storyboard=storyboard_block(sb),
        aspect_ratio=state.brief.aspect_ratio,
        scene=scene_block(scene),
        neighbours=_neighbours(sb, scene),
        sections=", ".join(sorted(s.value for s in scene.uses_bible_sections())),
        scene_bible=bible.prompt_block(scene.uses_bible_sections()),
        render_s=f"{render_s:g}",
        visible_s=f"{visible_s:g}",
        shot_vocab=entry_block(scene.vocab, ctx.settings.vocabulary.get(scene.vocab)),
        montage=sb.format is AdFormat.MONTAGE,
        wardrobe_lexicon=ctx.settings.culture.prompt_block(state.brief)
        if scene.shows_character
        else "(no person in this shot)",
    )
    return await ctx.llm.call_and_convert(
        LLMNode.PROMPT_WRITER,
        rendered,
        ScenePromptDraft,
        _to_prompt(template.template_id, bible, scene),
    )


def _to_prompt(
    template_id: str, bible: ContinuityBible, scene: Scene
) -> Callable[[ScenePromptDraft], ScenePrompt]:
    def convert(d: ScenePromptDraft) -> ScenePrompt:
        clashes = negative_contradictions(
            scene.shot.subject_action, scene.shot.composition, d.negative_prompt
        )
        if clashes:
            raise ValueError(
                "negative_prompt contradicts this shot: "
                + "; ".join(clashes)
                + ". Remove or narrow those terms (e.g. 'crowds', 'bystanders')."
            )
        return ScenePrompt(
            **d.model_dump(), template_version=template_id, bible_version=bible.version
        )

    return convert


async def _whole_ad_prompt(state: RunState, ctx: NodeContext) -> NodeResult:
    """Multishot (ADR-0023): ONE numbered-shot prompt for the whole ad, checked by code against
    the storyboard (shot count, timecodes, length), and the run's generation that will render it.
    The scenes get no per-scene prompt: nothing renders them one by one."""
    res = await write_ad_prompt(state, ctx)
    gen = res.value
    shots = " | ".join(f"{s.number} {s.start_s:g}-{s.end_s:g}s" for s in gen.prompt.shots)
    return NodeResult(
        deltas=[SetAdGeneration(generation=gen)],
        events=[
            decision(
                ctx.run_id,
                Stage.PROMPT,
                f"Ad prompt: {len(gen.prompt.shots)} shots in one {gen.duration_s}s generation "
                f"({gen.model}, {gen.resolution}, {gen.aspect_ratio}, audio "
                f"{'on' if gen.generate_audio else 'off'}): {shots}; "
                f"{len(gen.prompt.text)} characters",
                shots=len(gen.prompt.shots),
                chars=len(gen.prompt.text),
            )
        ],
        llm_calls=res.calls,
    )


def cliches_block(state: RunState) -> str:
    cl = state.concepts.category_cliches if state.concepts else []
    return "\n".join(f"- {c}" for c in cl) or "(none listed)"


def prop_image_names(state: RunState) -> list[str]:
    """Names of the run's reference images, in the order they follow the product photo."""
    return [r.name for r in state.reference_images]


async def write_ad_prompt(state: RunState, ctx: NodeContext) -> StructuredResult[AdGeneration]:
    """``ad_prompt_writer`` v2 (ADR-0026): the human-directed prompt's structure, structured
    references, the product's size per shot, one location, the key shot; it SEES the product
    photo. Code checks the timecodes against the storyboard and every creative rule it can."""
    assert state.brief and state.concepts and state.storyboard and state.bible
    concept = state.concepts.selected
    assert concept is not None
    sb, bible, cfg = state.storyboard, state.bible, state.config
    ep = ad_endpoint(ctx.settings, cfg.ad_model)
    duration = round(sb.target_duration_s)
    durations = [s.duration_s for s in sb.scenes]
    t, marks = 0.0, []
    for i, d in enumerate(durations, 1):
        marks.append(f"shot {i} {t:g}-{t + d:g} s ({sb.scenes[i - 1].purpose.value})")
        t += d
    rules = ctx.settings.creative_rules
    bank = ctx.settings.culture_bank
    names = prop_image_names(state)
    ident = identity_line(f"{bible.product.name} {bible.product.form}")
    template = ctx.prompts.get("ad_prompt_writer")
    rendered = template.render(
        brief=brief_block(state.brief),
        concept=concept_block(concept),
        bible=bible.prompt_block(),
        creative_rules=rules.prompt_block(),
        gulf_objects=bank.objects_block(),
        cliches=cliches_block(state),
        storyboard=storyboard_block(sb),
        duration=str(duration),
        aspect_ratio=state.brief.aspect_ratio,
        audio="on" if cfg.ad_generate_audio else "off",
        shots=str(len(durations)),
        timecodes="; ".join(marks),
        max_chars=str(ep.max_prompt_chars),
        photo="attached as Image 0" if state.product_photo is not None else "none",
        user_refs=", ".join(names) or "none",
    )
    aspect = state.brief.aspect_ratio
    model_key = ad_model_key(ctx.settings, cfg.ad_model)
    images = await product_photo_parts(state, ctx)

    def convert(d: AdPromptDraft) -> AdGeneration:
        d = with_gulf_objects(d, bank)  # a named Gulf object gets its canonical description
        problems = ad_prompt_problems(
            d, durations, duration_s=duration, max_chars=ep.max_prompt_chars, image_names=names,
            identity=ident,
        )  # fmt: skip
        problems += creative_rule_problems(d, rules)
        if problems:
            raise ValueError("; ".join(problems))
        ad_prompt = AdPrompt.from_draft(
            d,
            duration_s=duration,
            aspect_ratio=aspect,
            text=render_ad_prompt(
                d, duration_s=duration, aspect_ratio=aspect, image_names=names, identity=ident
            ),
            template_version=template.template_id,
            bible_version=bible.version,
            identity=ident,
        )
        return AdGeneration(
            prompt=ad_prompt,
            model=model_key,
            provider=cfg.ad_provider,
            duration_s=duration,
            resolution=cfg.ad_resolution or ep.default_resolution,
            aspect_ratio=aspect,
            generate_audio=cfg.ad_generate_audio,
        )

    return await ctx.llm.call_and_convert(
        LLMNode.PROMPT_WRITER, rendered, AdPromptDraft, convert, images=images
    )

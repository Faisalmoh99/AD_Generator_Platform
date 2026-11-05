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

from adsmith.agent.context import NodeContext, render_window
from adsmith.agent.deltas import Delta, FailScenes, SetScenePrompts
from adsmith.agent.i18n import failure_message
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import concept_block, decision, scene_block, storyboard_block
from adsmith.agent.recovery.errors import AgentError
from adsmith.agent.structured import StructuredResult
from adsmith.config import LLMNode
from adsmith.domain.continuity import ContinuityBible
from adsmith.domain.enums import Stage
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
    assert state.brief and state.concepts and state.storyboard and state.bible
    concept = state.concepts.selected
    assert concept is not None
    sb, bible = state.storyboard, state.bible
    todo = [s for s in sb.scenes if s.prompt is None and s.failure is None]
    template = ctx.prompts.get("prompt_writer")

    async def write(scene: Scene) -> StructuredResult[ScenePrompt]:
        render_s, visible_s = render_window(ctx.settings, state, scene.duration_s)
        rendered = template.render(
            bible=bible.prompt_block(),
            concept=concept_block(concept),
            storyboard=storyboard_block(sb),
            aspect_ratio=state.brief.aspect_ratio if state.brief else "9:16",
            scene=scene_block(scene),
            neighbours=_neighbours(sb, scene),
            sections=", ".join(sorted(s.value for s in scene.uses_bible_sections())),
            scene_bible=bible.prompt_block(scene.uses_bible_sections()),
            render_s=f"{render_s:g}",
            visible_s=f"{visible_s:g}",
            shot_vocab=entry_block(scene.vocab, ctx.settings.vocabulary.get(scene.vocab)),
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

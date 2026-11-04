"""storyboard: selected concept → ordered scenes with shot specs. Durations must sum to the brief
and fit what the video model can render; violations feed the schema-repair loop."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetStoryboard
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import (
    brief_block,
    concept_block,
    decision,
    flag_override_events,
    language_name,
    scene_duration_bounds,
)
from adsmith.config import LLMNode
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState
from adsmith.domain.scene_flags import enforce_scene_flags
from adsmith.domain.storyboard import Storyboard, StoryboardDraft


async def storyboard(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief is not None and state.concepts is not None
    concept = state.concepts.selected
    assert concept is not None
    min_s, max_s = scene_duration_bounds(ctx.settings)
    duration = state.brief.duration_s
    max_scenes = max(2, min(6, int(duration // min_s)))

    def convert(draft: StoryboardDraft) -> Storyboard:
        short = [
            f"{i}: {s.duration_s:g}s" for i, s in enumerate(draft.scenes, 1) if s.duration_s < min_s
        ]
        if short:
            raise ValueError(
                f"scenes shorter than the video model's {min_s:g}s minimum: {', '.join(short)}. "
                f"Use at most {max_scenes} scenes and redistribute the time."
            )
        return Storyboard.from_draft(draft, concept_id=concept.id, target_duration_s=duration)

    prompt = ctx.prompts.get("storyboard").render(
        brief=brief_block(state.brief),
        concept=concept_block(concept),
        duration=f"{duration:g}",
        min_scene=f"{min_s:g}",
        max_scene=f"{max_s:g}",
        max_scenes=str(max_scenes),
        aspect_ratio=state.brief.aspect_ratio,
        user_language=language_name(state.brief.language),
    )
    result = await ctx.llm.call_and_convert(LLMNode.STORYBOARD, prompt, StoryboardDraft, convert)
    sb = result.value
    overrides = enforce_scene_flags(sb.scenes)  # generic EN/AR terms; bible terms after continuity
    _, flag_events = flag_override_events(ctx.run_id, Stage.STORYBOARD, overrides)
    summary = " | ".join(
        f"{s.order}:{s.purpose.value} {s.shot.shot_type.value}/{s.shot.camera_movement.value} "
        f"{s.duration_s:g}s"
        for s in sb.scenes
    )
    return NodeResult(
        deltas=[SetStoryboard(storyboard=sb)],
        events=[*flag_events, decision(ctx.run_id, Stage.STORYBOARD, f"Storyboard: {summary}")],
        llm_calls=result.calls,
    )

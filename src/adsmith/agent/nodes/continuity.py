"""continuity: concept + storyboard → ContinuityBible (the single source of visual truth).
Then the bible-aware scene-flag check runs (ADR-0010): upgrade-only, every override logged."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import Delta, SetBible, UpgradeSceneFlags
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import (
    brief_block,
    concept_block,
    decision,
    flag_override_events,
    storyboard_block,
)
from adsmith.agent.nodes.photo import product_photo_parts
from adsmith.config import LLMNode
from adsmith.domain.continuity import (
    ContinuityBible,
    ContinuityBibleDraft,
    effect_feature_problems,
    wordless_label_problems,
)
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState
from adsmith.domain.scene_flags import enforce_scene_flags


async def continuity(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.brief and state.concepts and state.storyboard
    concept = state.concepts.selected
    assert concept is not None
    version = state.bible.version + 1 if state.bible else 1
    prompt = ctx.prompts.get("continuity").render(
        brief=brief_block(state.brief),
        concept=concept_block(concept),
        storyboard=storyboard_block(state.storyboard),
        wardrobe_lexicon=ctx.settings.culture.prompt_block(state.brief),
        product_photo="attached (the image above): describe this exact product"
        if state.product_photo is not None
        else "(none: design the product from the brief)",
    )
    images = await product_photo_parts(state, ctx)
    culture, brief = ctx.settings.culture, state.brief

    def convert(d: ContinuityBibleDraft) -> ContinuityBible:
        # → schema repair with the exact problems
        problems = wordless_label_problems(d.product) + effect_feature_problems(d.product)
        problems += culture.wardrobe_problems(brief, d.character)
        if problems:
            raise ValueError("; ".join(problems))
        return ContinuityBible(**d.model_dump(), version=version)

    result = await ctx.llm.call_and_convert(
        LLMNode.CONTINUITY, prompt, ContinuityBibleDraft, convert, images=images
    )
    bible = result.value
    # Check on a copy: the node must not mutate the snapshot it was given.
    scenes = [s.model_copy(deep=True) for s in state.storyboard.scenes]
    flags, flag_events = flag_override_events(
        ctx.run_id, Stage.CONTINUITY, enforce_scene_flags(scenes, bible)
    )
    deltas: list[Delta] = [SetBible(bible=bible)]
    if flags:
        deltas.append(UpgradeSceneFlags(flags=flags))
    who = bible.character.description if bible.character else "no character"
    return NodeResult(
        deltas=deltas,
        events=[
            *flag_events,
            decision(
                ctx.run_id,
                Stage.CONTINUITY,
                f"Bible v{version}: {who}; product {bible.product.form}; "
                f"palette {' '.join(bible.color_grade.palette)}",
            ),
        ],
        llm_calls=result.calls,
    )

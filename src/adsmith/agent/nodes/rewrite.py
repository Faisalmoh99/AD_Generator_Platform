"""Shared prompt-revision step for two recovery strategies (ADR-0004):
content-policy rejection → remove triggers, keep intent; QA rejection → apply the critic's fixes."""

from __future__ import annotations

import json
from typing import Literal

from adsmith.agent.context import NodeContext
from adsmith.agent.nodes.common import scene_block
from adsmith.agent.structured import StructuredResult
from adsmith.config import LLMNode
from adsmith.domain.continuity import ContinuityBible
from adsmith.domain.scene import Scene, ScenePrompt, ScenePromptDraft
from adsmith.domain.shot_lint import negative_contradictions

ReasonKind = Literal["content_policy", "qa_feedback"]


async def rewrite_prompt(
    ctx: NodeContext,
    scene: Scene,
    bible: ContinuityBible,
    *,
    reason: ReasonKind,
    detail: str,
) -> StructuredResult[ScenePrompt]:
    assert scene.prompt is not None
    template = ctx.prompts.get("prompt_rewrite")
    current = scene.prompt.model_dump(
        mode="json", include={"keyframe_prompt", "motion_prompt", "negative_prompt"}
    )
    rendered = template.render(
        reason_kind=reason,
        reason_detail=detail,
        scene=scene_block(scene),
        scene_bible=bible.prompt_block(scene.uses_bible_sections()),
        current_prompt=json.dumps(current, ensure_ascii=False, indent=2),
    )

    def convert(d: ScenePromptDraft) -> ScenePrompt:
        clashes = negative_contradictions(
            scene.shot.subject_action, scene.shot.composition, d.negative_prompt
        )
        if clashes:
            raise ValueError("negative_prompt contradicts this shot: " + "; ".join(clashes))
        return ScenePrompt(
            **d.model_dump(), template_version=template.template_id, bible_version=bible.version
        )

    node = LLMNode.POLICY_REWRITE if reason == "content_policy" else LLMNode.PROMPT_WRITER
    return await ctx.llm.call_and_convert(node, rendered, ScenePromptDraft, convert)

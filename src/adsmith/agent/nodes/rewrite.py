"""Shared prompt-revision step for two recovery strategies (ADR-0004):
content-policy rejection → remove triggers, keep intent; QA rejection → apply the critic's fixes.

Whether the keyframe is regenerated is decided by **code** (from the verdict's issue categories
and locations), passed to the model as an instruction, and enforced on its output: if the
keyframe is kept, a changed ``keyframe_prompt`` is a validation error (→ repair), because the
reused keyframe would no longer match its own prompt.
"""

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

KEEP_KEYFRAME = (
    "The approved keyframe is KEPT and reused. keyframe_prompt must stay byte-identical; fix "
    "every issue through motion_prompt (and negative_prompt)."
)
NEW_KEYFRAME = (
    "The keyframe WILL BE REGENERATED, because the critic found problems in the still image. "
    "Fix those in keyframe_prompt, and fix the motion issues in motion_prompt."
)
POLICY_EITHER = (
    "Change keyframe_prompt only if it likely triggered the rejection; changing it means the "
    "keyframe is regenerated."
)


async def rewrite_prompt(
    ctx: NodeContext,
    scene: Scene,
    bible: ContinuityBible,
    *,
    reason: ReasonKind,
    detail: str,
    render_s: float,
    keep_keyframe: bool | None = None,
) -> StructuredResult[ScenePrompt]:
    """``keep_keyframe``: True/False for a QA revision (decided by code), None for policy."""
    assert scene.prompt is not None
    template = ctx.prompts.get("prompt_rewrite")
    current = scene.prompt.model_dump(
        mode="json", include={"keyframe_prompt", "motion_prompt", "negative_prompt"}
    )
    decision = (
        POLICY_EITHER if keep_keyframe is None else KEEP_KEYFRAME if keep_keyframe else NEW_KEYFRAME
    )
    rendered = template.render(
        reason_kind=reason,
        reason_detail=detail,
        scene=scene_block(scene),
        scene_bible=bible.prompt_block(scene.uses_bible_sections()),
        current_prompt=json.dumps(current, ensure_ascii=False, indent=2),
        render_s=f"{render_s:g}",
        visible_s=f"{scene.duration_s:g}",
        keyframe_decision=decision,
    )
    original_keyframe = scene.prompt.keyframe_prompt

    def convert(d: ScenePromptDraft) -> ScenePrompt:
        problems = [
            "negative_prompt contradicts this shot: " + "; ".join(clashes)
            for clashes in [
                negative_contradictions(
                    scene.shot.subject_action, scene.shot.composition, d.negative_prompt
                )
            ]
            if clashes
        ]
        if keep_keyframe and d.keyframe_prompt != original_keyframe:
            problems.append(
                "keyframe_prompt changed, but the keyframe is kept: return the current "
                "keyframe_prompt byte-identical and fix the issues in motion_prompt"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return ScenePrompt(
            **d.model_dump(), template_version=template.template_id, bible_version=bible.version
        )

    node = LLMNode.POLICY_REWRITE if reason == "content_policy" else LLMNode.PROMPT_WRITER
    return await ctx.llm.call_and_convert(node, rendered, ScenePromptDraft, convert)

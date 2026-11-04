"""Template name → LLM output schema. The single source of truth used by nodes, by the prompt
template frontmatter check, and by the scripted-fixture validation test."""

from __future__ import annotations

from pydantic import BaseModel

from adsmith.domain.brief import BriefDraft
from adsmith.domain.concept import ConceptJudgementDraft, ConceptSetDraft
from adsmith.domain.continuity import ContinuityBibleDraft
from adsmith.domain.edit import ScenePatchDraft
from adsmith.domain.qa import QAVerdictDraft
from adsmith.domain.scene import ScenePromptDraft
from adsmith.domain.storyboard import StoryboardDraft

# Nodes added in later milestones (conflict_check, policy_rewrite) register
# their schemas here when they are built.
OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "understand": BriefDraft,
    "ideate": ConceptSetDraft,
    "select": ConceptJudgementDraft,
    "storyboard": StoryboardDraft,
    "continuity": ContinuityBibleDraft,
    "prompt_writer": ScenePromptDraft,
    "prompt_rewrite": ScenePromptDraft,
    "edit_parser": ScenePatchDraft,
    "qa_critic": QAVerdictDraft,
}

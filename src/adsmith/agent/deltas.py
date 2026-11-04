"""Typed state deltas: the only way nodes change state (CLAUDE.md rule 2).

A node computes deltas from a *snapshot* of the state; the orchestrator applies them inside one
atomic ``RunRepository.update`` (ADR-0011). Deltas are data, so they can be logged, asserted in
tests and reasoned about. Closures cannot.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from adsmith.domain.base import DomainModel
from adsmith.domain.brief import Brief
from adsmith.domain.concept import ConceptSet
from adsmith.domain.continuity import ContinuityBible
from adsmith.domain.run import RunState
from adsmith.domain.scene import ScenePrompt
from adsmith.domain.storyboard import Storyboard


class SetBrief(DomainModel):
    kind: Literal["set_brief"] = "set_brief"
    brief: Brief


class SetConcepts(DomainModel):
    kind: Literal["set_concepts"] = "set_concepts"
    concepts: ConceptSet


class SelectConcept(DomainModel):
    kind: Literal["select_concept"] = "select_concept"
    concept_id: str
    by: Literal["user", "agent"]
    rationale: str | None = None


class SetStoryboard(DomainModel):
    kind: Literal["set_storyboard"] = "set_storyboard"
    storyboard: Storyboard


class SetBible(DomainModel):
    kind: Literal["set_bible"] = "set_bible"
    bible: ContinuityBible


class SetScenePrompts(DomainModel):
    kind: Literal["set_scene_prompts"] = "set_scene_prompts"
    prompts: dict[str, ScenePrompt]  # scene_id → prompt
    for_versions: dict[str, int]  # scene_id → scene.version the prompt was written for


Delta = Annotated[
    SetBrief | SetConcepts | SelectConcept | SetStoryboard | SetBible | SetScenePrompts,
    Field(discriminator="kind"),
]


class StaleDeltaError(RuntimeError):
    """The state moved on since the node read it; the delta no longer applies."""


def apply_delta(state: RunState, delta: Delta) -> None:
    match delta:
        case SetBrief(brief=brief):
            state.brief = brief
        case SetConcepts(concepts=concepts):
            state.concepts = concepts
        case SelectConcept(concept_id=cid, by=by, rationale=rationale):
            if state.concepts is None:
                raise StaleDeltaError("no concepts to select from")
            state.concepts.select(cid, by, rationale)
        case SetStoryboard(storyboard=sb):
            state.storyboard = sb
        case SetBible(bible=bible):
            if state.bible is not None:
                state.bible_history.append(state.bible)
            state.bible = bible
        case SetScenePrompts(prompts=prompts, for_versions=versions):
            if state.storyboard is None:
                raise StaleDeltaError("no storyboard")
            for scene_id, prompt in prompts.items():
                scene = state.storyboard.by_id(scene_id)
                if scene.version != versions[scene_id]:
                    raise StaleDeltaError(f"scene {scene.order} was edited while prompting")
                scene.set_prompt(prompt)
                if scene.stale_from is not None:
                    scene.requeue()

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
from adsmith.domain.scene import SceneFailure, ScenePrompt
from adsmith.domain.scene_flags import Flag
from adsmith.domain.storyboard import Storyboard
from adsmith.storage.repo import StaleWriteError


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


class UpgradeSceneFlags(DomainModel):
    """Upgrade-only (ADR-0010): flags can be turned on, never off."""

    kind: Literal["upgrade_scene_flags"] = "upgrade_scene_flags"
    flags: dict[str, list[Flag]]  # scene_id → flags to set True


class FailScenes(DomainModel):
    """Isolate failures: one scene failing does not fail the stage for the others."""

    kind: Literal["fail_scenes"] = "fail_scenes"
    failures: dict[str, SceneFailure]


Delta = Annotated[
    SetBrief
    | SetConcepts
    | SelectConcept
    | SetStoryboard
    | SetBible
    | SetScenePrompts
    | UpgradeSceneFlags
    | FailScenes,
    Field(discriminator="kind"),
]


class StaleDeltaError(StaleWriteError):
    """The state moved on since the node read it; the delta no longer applies."""


def _storyboard(state: RunState) -> Storyboard:
    if state.storyboard is None:
        raise StaleDeltaError("no storyboard")
    return state.storyboard


def _set_prompts(state: RunState, delta: SetScenePrompts) -> None:
    sb = _storyboard(state)
    for scene_id, prompt in delta.prompts.items():
        scene = sb.by_id(scene_id)
        if scene.version != delta.for_versions[scene_id]:
            raise StaleDeltaError(f"scene {scene.order} was edited while prompting")
        scene.set_prompt(prompt)
        if scene.stale_from is not None:
            scene.requeue()


def _upgrade_flags(state: RunState, delta: UpgradeSceneFlags) -> None:
    sb = _storyboard(state)
    for scene_id, names in delta.flags.items():
        scene = sb.by_id(scene_id)
        for name in names:
            setattr(scene, name, True)


def _fail_scenes(state: RunState, delta: FailScenes) -> None:
    sb = _storyboard(state)
    for scene_id, failure in delta.failures.items():
        sb.by_id(scene_id).fail(failure)


def _set_bible(state: RunState, delta: SetBible) -> None:
    if state.bible is not None:
        state.bible_history.append(state.bible)
    state.bible = delta.bible


def _select(state: RunState, delta: SelectConcept) -> None:
    if state.concepts is None:
        raise StaleDeltaError("no concepts to select from")
    state.concepts.select(delta.concept_id, delta.by, delta.rationale)


def apply_delta(state: RunState, delta: Delta) -> None:
    match delta:
        case SetBrief():
            state.brief = delta.brief
        case SetConcepts():
            state.concepts = delta.concepts
        case SelectConcept():
            _select(state, delta)
        case SetStoryboard():
            state.storyboard = delta.storyboard
        case SetBible():
            _set_bible(state, delta)
        case SetScenePrompts():
            _set_prompts(state, delta)
        case UpgradeSceneFlags():
            _upgrade_flags(state, delta)
        case FailScenes():
            _fail_scenes(state, delta)

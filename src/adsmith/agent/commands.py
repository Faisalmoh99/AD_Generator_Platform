"""Human commands at the gates. Each is one atomic, validated state update.

Commands may carry ``expected_version``, the state version the human was looking at. If the run
moved on, ``ConcurrencyConflictError`` is raised and the client must re-read (ADR-0011).
"""

from __future__ import annotations

from pydantic import JsonValue

from adsmith.agent.transitions import GATE_EXITS
from adsmith.domain.enums import SceneStatus, Stage, StaleFrom
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.run import RunState
from adsmith.domain.scene import ShotSpec
from adsmith.domain.scene_flags import enforce_scene_flags
from adsmith.storage import Storage


class CommandRejectedError(ValueError):
    """The command does not apply to the run's current state."""


def choose_concept(
    storage: Storage, run_id: str, choice: str | int, *, expected_version: int | None = None
) -> RunState:
    """Pick a concept by number (1–3) or id at the concept gate."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.stage is not Stage.AWAITING_CONCEPT_CHOICE or s.concepts is None:
            raise CommandRejectedError(f"run is at {s.stage}, not waiting for a concept choice")
        concepts = s.concepts.concepts
        if isinstance(choice, int) or (isinstance(choice, str) and choice.isdigit()):
            n = int(choice)
            if not 1 <= n <= len(concepts):
                raise CommandRejectedError(f"choose a concept between 1 and {len(concepts)}")
            concept = concepts[n - 1]
        else:
            matches = [c for c in concepts if c.id == choice]
            if not matches:
                raise CommandRejectedError(f"unknown concept {choice!r}")
            concept = matches[0]
        s.concepts.select(concept.id, "user", None)
        s.stage = GATE_EXITS[Stage.AWAITING_CONCEPT_CHOICE]
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.AWAITING_CONCEPT_CHOICE,
                type=EventType.DECISION,
                summary=f"User selected concept '{concept.title}'.",
                payload={"concept_id": concept.id, "by": "user"},
            )
        ]

    return storage.runs.update(
        run_id, mutate, reason="concept chosen", expected_version=expected_version
    ).state


def approve_storyboard(
    storage: Storage, run_id: str, *, expected_version: int | None = None
) -> RunState:
    """Approve the keyframes at the storyboard gate; video generation (and spend) starts after."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.stage is not Stage.AWAITING_STORYBOARD_APPROVAL:
            raise CommandRejectedError(f"run is at {s.stage}, not waiting for storyboard approval")
        s.stage = GATE_EXITS[Stage.AWAITING_STORYBOARD_APPROVAL]
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.AWAITING_STORYBOARD_APPROVAL,
                type=EventType.DECISION,
                summary="User approved the storyboard keyframes; generating clips.",
            )
        ]

    return storage.runs.update(
        run_id, mutate, reason="storyboard approved", expected_version=expected_version
    ).state


def edit_scene_spec(
    storage: Storage,
    run_id: str,
    scene_order: int,
    *,
    reason: str,
    description: str | None = None,
    shot: dict[str, object] | None = None,
    expected_version: int | None = None,
) -> RunState:
    """Minimal scene edit (first real use: M11 S4 simplification; the full natural-language edit
    flow is M7). Changes the creative spec of ONE scene, bumps its version (archiving the old spec
    and prompt), marks it stale at prompt depth, and sends the run back to the ``prompt`` stage:
    the prompt node rewrites only this scene's prompt and requeues it. Other scenes are untouched.
    With no changes it is a pure re-prompt (e.g. after a prompt template upgrade)."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.storyboard is None or s.bible is None:
            raise CommandRejectedError("the run has no storyboard yet")
        matches = [sc for sc in s.storyboard.scenes if sc.order == scene_order]
        if not matches:
            raise CommandRejectedError(f"no scene {scene_order}")
        scene = matches[0]
        if scene.status is SceneStatus.GENERATING:
            raise CommandRejectedError(f"scene {scene_order} is generating; retry when it settles")
        unknown = set(shot or {}) - set(ShotSpec.model_fields)
        if unknown:
            raise CommandRejectedError(f"unknown shot fields: {sorted(unknown)}")
        new_shot = ShotSpec.model_validate({**scene.shot.model_dump(), **(shot or {})})
        scene.bump_version(f"edit: {reason}")
        scene.shot = new_shot
        if description is not None:
            scene.description = description
        scene.mark_stale(StaleFrom.PROMPT)
        overrides = enforce_scene_flags([scene], s.bible)  # upgrade-only (ADR-0010)
        s.stage = Stage.PROMPT
        changed = sorted((shot or {}).keys()) + (["description"] if description else [])
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.PROMPT,
                scene_id=scene.id,
                type=EventType.DECISION,
                summary=f"Scene {scene_order} edited ({reason}): "
                f"{', '.join(changed) or 're-prompt only'}; v{scene.version}, stale from prompt. "
                "Other scenes untouched.",
                payload={
                    "changed": list[JsonValue](changed),
                    "version": scene.version,
                    "flag_overrides": list[JsonValue]([o.flag for o in overrides]),
                },
            )
        ]

    return storage.runs.update(
        run_id, mutate, reason=f"scene {scene_order} edited", expected_version=expected_version
    ).state


def reassemble(storage: Storage, run_id: str, *, partial_mode: str | None = None) -> RunState:
    """Re-run only the assembly of a finished run (new end card / partial mode), no generation:
    ffmpeg plus at most one small LLM call for the end-card copy."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.stage not in {Stage.DONE, Stage.DONE_PARTIAL}:
            raise CommandRejectedError(f"run is at {s.stage}; reassemble needs a finished run")
        if partial_mode is not None:
            if partial_mode not in {"skip", "slate"}:
                raise CommandRejectedError("partial_mode must be 'skip' or 'slate'")
            s.config = s.config.model_copy(update={"partial_mode": partial_mode})
        s.stage = Stage.ASSEMBLE
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.ASSEMBLE,
                type=EventType.DECISION,
                summary=f"Re-assembling (partial mode: {s.config.partial_mode}).",
            )
        ]

    return storage.runs.update(run_id, mutate, reason="reassemble").state

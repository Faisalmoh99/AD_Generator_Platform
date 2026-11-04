"""Human commands at the gates. Each is one atomic, validated state update.

Commands may carry ``expected_version``, the state version the human was looking at. If the run
moved on, ``ConcurrencyConflictError`` is raised and the client must re-read (ADR-0011).
"""

from __future__ import annotations

from adsmith.agent.transitions import GATE_EXITS
from adsmith.domain.enums import Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.run import RunState
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

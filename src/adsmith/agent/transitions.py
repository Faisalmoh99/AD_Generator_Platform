"""The brain's control flow: a declared transition table of pure guard functions (ADR-0001).

``next_stage(state, completed)`` depends only on ``RunState``. No I/O, no LLM. Every branch is
unit-tested in ``tests/unit/test_transitions.py``.
"""

from __future__ import annotations

from collections.abc import Callable

from adsmith.domain.enums import AWAITING_STAGES, TERMINAL_STAGES, SceneStatus, Stage
from adsmith.domain.run import RunState

Guard = Callable[[RunState], Stage]


class IllegalStageError(RuntimeError):
    pass


def _after_ideate(s: RunState) -> Stage:
    return Stage.AWAITING_CONCEPT_CHOICE if s.config.interactive else Stage.SELECT


def _after_keyframe(s: RunState) -> Stage:
    return Stage.AWAITING_STORYBOARD_APPROVAL if s.config.interactive else Stage.GENERATE


def _scenes(s: RunState) -> list[SceneStatus]:
    return [sc.status for sc in s.storyboard.scenes] if s.storyboard else []


def _after_qa(s: RunState) -> Stage:
    """Loop back while any scene still has work; otherwise assemble."""
    unfinished = {SceneStatus.PENDING, SceneStatus.STALE, SceneStatus.QA_REVIEW}
    return Stage.GENERATE if any(st in unfinished for st in _scenes(s)) else Stage.ASSEMBLE


def _after_assemble(s: RunState) -> Stage:
    all_ok = all(st is SceneStatus.APPROVED for st in _scenes(s))
    return Stage.DONE if all_ok and s.final is not None else Stage.DONE_PARTIAL


TRANSITIONS: dict[Stage, Guard] = {
    Stage.UNDERSTAND: lambda s: Stage.IDEATE,
    Stage.IDEATE: _after_ideate,
    Stage.SELECT: lambda s: Stage.STORYBOARD,
    Stage.STORYBOARD: lambda s: Stage.CONTINUITY,
    Stage.CONTINUITY: lambda s: Stage.PROMPT,
    Stage.PROMPT: lambda s: Stage.KEYFRAME,
    Stage.KEYFRAME: _after_keyframe,
    Stage.GENERATE: lambda s: Stage.QA,
    Stage.QA: _after_qa,
    Stage.ASSEMBLE: _after_assemble,
}

# Exits from human gates, taken by commands (not by the run loop).
GATE_EXITS: dict[Stage, Stage] = {
    Stage.AWAITING_CONCEPT_CHOICE: Stage.STORYBOARD,
    Stage.AWAITING_STORYBOARD_APPROVAL: Stage.GENERATE,
}


def next_stage(state: RunState, completed: Stage) -> Stage:
    """Stage that follows ``completed``, given the state *after* its deltas were applied."""
    if completed in AWAITING_STAGES or completed in TERMINAL_STAGES:
        raise IllegalStageError(f"{completed} is not completed by the run loop")
    return TRANSITIONS[completed](state)

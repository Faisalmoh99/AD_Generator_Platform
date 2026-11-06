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


def _after_select(s: RunState) -> Stage:
    # Interactive: the judge only recommends; the human picks at the gate.
    return Stage.AWAITING_CONCEPT_CHOICE if s.config.interactive else Stage.STORYBOARD


def _after_keyframe(s: RunState) -> Stage:
    return Stage.AWAITING_STORYBOARD_APPROVAL if s.config.interactive else Stage.GENERATE


def whole_ad(s: RunState) -> bool:
    """The run renders its whole ad in ONE generation (multishot, ADR-0023)."""
    return s.generation is not None


def _after_prompt(s: RunState) -> Stage:
    return Stage.ESTIMATE if whole_ad(s) else Stage.KEYFRAME


def _after_estimate(s: RunState) -> Stage:
    """Real money always stops at the human gate, even in auto mode. Only an approval already
    recorded by code (mock media in auto mode, or a $0 cache hit) goes straight on."""
    g = s.generation
    if g is None or g.status == "failed":
        return Stage.ASSEMBLE  # nothing to generate: assemble reports it, the run ends partial
    return Stage.GENERATE if g.approved else Stage.AWAITING_GENERATION_APPROVAL


def _after_generate(s: RunState) -> Stage:
    if whole_ad(s):
        g = s.generation
        return Stage.QA if g is not None and g.status == "qa_review" else Stage.ASSEMBLE
    return Stage.QA


def _scenes(s: RunState) -> list[SceneStatus]:
    return [sc.status for sc in s.storyboard.scenes] if s.storyboard else []


def _after_qa(s: RunState) -> Stage:
    """Loop back while any scene still has work; otherwise assemble. A whole-ad generation is
    reviewed once and always assembled (warn only: a regeneration is a human decision)."""
    if whole_ad(s):
        return Stage.ASSEMBLE
    unfinished = {SceneStatus.PENDING, SceneStatus.STALE, SceneStatus.QA_REVIEW}
    return Stage.GENERATE if any(st in unfinished for st in _scenes(s)) else Stage.ASSEMBLE


def _after_assemble(s: RunState) -> Stage:
    if whole_ad(s):
        ok = s.generation is not None and s.generation.status == "approved"
        return Stage.DONE if ok and s.final is not None else Stage.DONE_PARTIAL
    all_ok = all(st is SceneStatus.APPROVED for st in _scenes(s))
    return Stage.DONE if all_ok and s.final is not None else Stage.DONE_PARTIAL


TRANSITIONS: dict[Stage, Guard] = {
    Stage.UNDERSTAND: lambda s: Stage.INSIGHT,
    Stage.INSIGHT: lambda s: Stage.IDEATE,
    Stage.IDEATE: lambda s: Stage.SELECT,
    Stage.SELECT: _after_select,
    Stage.STORYBOARD: lambda s: Stage.CONTINUITY,
    Stage.CONTINUITY: lambda s: Stage.PROMPT,
    Stage.PROMPT: _after_prompt,
    Stage.ESTIMATE: _after_estimate,
    Stage.KEYFRAME: _after_keyframe,
    Stage.GENERATE: _after_generate,
    Stage.QA: _after_qa,
    Stage.ASSEMBLE: _after_assemble,
}

# Exits from human gates, taken by commands (not by the run loop).
GATE_EXITS: dict[Stage, Stage] = {
    Stage.AWAITING_CONCEPT_CHOICE: Stage.STORYBOARD,
    Stage.AWAITING_STORYBOARD_APPROVAL: Stage.GENERATE,
    Stage.AWAITING_GENERATION_APPROVAL: Stage.GENERATE,
}


def next_stage(state: RunState, completed: Stage) -> Stage:
    """Stage that follows ``completed``, given the state *after* its deltas were applied."""
    if completed in AWAITING_STAGES or completed in TERMINAL_STAGES:
        raise IllegalStageError(f"{completed} is not completed by the run loop")
    return TRANSITIONS[completed](state)

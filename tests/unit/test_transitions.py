"""The transition table: every stage, every guard branch. Pure functions, no I/O."""

from __future__ import annotations

import pytest

from adsmith.agent.transitions import TRANSITIONS, IllegalStageError, next_stage
from adsmith.domain.enums import AWAITING_STAGES, TERMINAL_STAGES, FailureKind, SceneStatus, Stage
from adsmith.domain.run import RunConfig, RunState
from adsmith.domain.scene import SceneFailure
from tests.factories import make_full_run, make_video, sha


def run(interactive: bool = True) -> RunState:
    state = make_full_run(Stage.PROMPT)
    state.config = RunConfig(interactive=interactive)
    return state


@pytest.mark.parametrize(
    ("completed", "expected"),
    [
        (Stage.UNDERSTAND, Stage.IDEATE),
        (Stage.SELECT, Stage.STORYBOARD),
        (Stage.STORYBOARD, Stage.CONTINUITY),
        (Stage.CONTINUITY, Stage.PROMPT),
        (Stage.PROMPT, Stage.KEYFRAME),
        (Stage.GENERATE, Stage.QA),
    ],
)
def test_linear_edges(completed: Stage, expected: Stage) -> None:
    assert next_stage(run(), completed) is expected


@pytest.mark.parametrize(
    ("interactive", "expected"), [(True, Stage.AWAITING_CONCEPT_CHOICE), (False, Stage.SELECT)]
)
def test_after_ideate_depends_on_mode(interactive: bool, expected: Stage) -> None:
    assert next_stage(run(interactive), Stage.IDEATE) is expected


@pytest.mark.parametrize(
    ("interactive", "expected"),
    [(True, Stage.AWAITING_STORYBOARD_APPROVAL), (False, Stage.GENERATE)],
)
def test_after_keyframe_depends_on_mode(interactive: bool, expected: Stage) -> None:
    assert next_stage(run(interactive), Stage.KEYFRAME) is expected


def _set_statuses(state: RunState, statuses: list[SceneStatus]) -> None:
    assert state.storyboard is not None
    for scene, st in zip(state.storyboard.scenes, statuses, strict=True):
        scene.status = st
        if st is SceneStatus.FAILED:
            scene.failure = SceneFailure(kind=FailureKind.EXHAUSTED, reason_user="x")


def test_after_qa_loops_while_work_remains() -> None:
    s = run()
    _set_statuses(
        s, [SceneStatus.APPROVED, SceneStatus.PENDING, SceneStatus.APPROVED, SceneStatus.FAILED]
    )
    assert next_stage(s, Stage.QA) is Stage.GENERATE


def test_after_qa_assembles_when_all_settled_even_with_failures() -> None:
    s = run()
    _set_statuses(
        s, [SceneStatus.APPROVED, SceneStatus.FAILED, SceneStatus.APPROVED, SceneStatus.APPROVED]
    )
    assert next_stage(s, Stage.QA) is Stage.ASSEMBLE


def test_after_assemble_done_vs_partial() -> None:
    s = run()
    s.final = make_video("final").model_copy(update={"sha256": sha("final")})
    _set_statuses(s, [SceneStatus.APPROVED] * 4)
    assert next_stage(s, Stage.ASSEMBLE) is Stage.DONE
    _set_statuses(
        s, [SceneStatus.APPROVED, SceneStatus.FAILED, SceneStatus.APPROVED, SceneStatus.APPROVED]
    )
    assert next_stage(s, Stage.ASSEMBLE) is Stage.DONE_PARTIAL


@pytest.mark.parametrize("stage", sorted(AWAITING_STAGES | TERMINAL_STAGES))
def test_gates_and_terminals_are_not_run_loop_stages(stage: Stage) -> None:
    with pytest.raises(IllegalStageError):
        next_stage(run(), stage)


def test_every_non_gate_stage_has_a_transition() -> None:
    loop_stages = set(Stage) - AWAITING_STAGES - TERMINAL_STAGES
    assert set(TRANSITIONS) == loop_stages

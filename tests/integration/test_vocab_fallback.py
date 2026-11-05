"""STOP 2 rule: a montage shot whose keyframe fails the gate on every try is swapped by the
storyboard to its vocabulary fallback (once), logged as a decision; no hand re-roll."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent.qa import _ALL_OK, MockCritic
from adsmith.agent.scenes import fallback_for
from adsmith.domain.enums import SceneStatus, Stage
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAIssue, QAVerdict
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene
from tests.media_helpers import fast_settings, make_harness

REQUEST = "Make me a 12-second ad for a luxury men's perfume for young men, luxurious and bold."

Review = Callable[
    [MockCritic, RunState, Scene], Coroutine[Any, Any, tuple[QAVerdict, list[LLMCallRecord]]]
]


def _gate_failing(vocabs: set[str]) -> Review:
    async def review_keyframe(
        self: MockCritic, state: RunState, scene: Scene
    ) -> tuple[QAVerdict, list[LLMCallRecord]]:
        bad = scene.vocab in vocabs
        issues = (
            [
                QAIssue(
                    category="composition",
                    severity="major",
                    observation="no splash",
                    fix="show the splash",
                    frame_index=0,
                )
            ]
            if bad
            else []
        )
        checks = _ALL_OK.model_copy(update={"matches_shot_spec": not bad})
        verdict = QAVerdict(
            score=0.4 if bad else 0.9, checks=checks, frames_inspected=1, issues=issues,
            threshold=state.config.qa_threshold, composition_hard=bad,
        )  # fmt: skip
        return verdict, []

    return review_keyframe


async def test_a_shot_failing_the_gate_twice_falls_back_to_its_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MockCritic, "review_keyframe", _gate_failing({"bottle_into_water"}))
    h = make_harness(fast_settings(tmp_path, keyframe_max_tries=2), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE, state.error
    assert state.storyboard is not None
    s6 = state.storyboard.scenes[5]
    assert s6.status is SceneStatus.APPROVED
    assert (s6.vocab, s6.version) == ("water_ribbon", 2)
    assert s6.history[-1].vocab == "bottle_into_water"
    assert s6.history[-1].reason.startswith("fallback: bottle_into_water → water_ribbon")
    entry = h.rt.settings.vocabulary.shots["water_ribbon"]
    assert s6.shot.subject_action == entry.motion and s6.shows_product
    decisions = [
        e
        for e in h.rt.storage.events.tail(run.id, limit=10_000)
        if e.payload.get("strategy") == "vocab_fallback"
    ]
    assert len(decisions) == 1 and decisions[0].payload["to_vocab"] == "water_ribbon"


async def test_a_shot_falls_back_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MockCritic, "review_keyframe", _gate_failing({"bottle_into_water", "water_ribbon"})
    )
    h = make_harness(fast_settings(tmp_path, keyframe_max_tries=2), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE_PARTIAL
    assert state.storyboard is not None
    s6 = state.storyboard.scenes[5]
    assert s6.status is SceneStatus.FAILED and s6.vocab == "water_ribbon"
    others = [s for s in state.storyboard.scenes if s.id != s6.id]
    assert all(s.status is SceneStatus.APPROVED for s in others)


async def test_fallback_skips_entries_in_use_and_a_second_person(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.storyboard is not None
    s = h.rt.settings
    by_vocab = {sc.vocab: sc for sc in state.storyboard.scenes}
    # spray_from_nozzle → light_sweep (already S7) → rim_light_reveal
    assert fallback_for(s, state, by_vocab["spray_from_nozzle"]) == "rim_light_reveal"
    # hand_cuff → bottle_on_rock (unused)
    assert fallback_for(s, state, by_vocab["hand_cuff"]) == "bottle_on_rock"
    # water_drop_leaf → droplets_on_glass (S4) → glass_edge_macro (S3) → light_sweep (S7) →
    # rim_light_reveal
    assert fallback_for(s, state, by_vocab["water_drop_leaf"]) == "rim_light_reveal"

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


async def test_fallback_skips_entries_in_use_avoided_and_a_second_person(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.storyboard is not None
    assert state.config.video_model == "kling_v3_pro"  # light-only entries are avoided on Kling
    s = h.rt.settings
    by_vocab = {sc.vocab: sc for sc in state.storyboard.scenes}
    # spray_from_nozzle → light_sweep (avoided) → rim_light_reveal (avoided) → hero_push_in (S9)
    # → bottle_on_rock (unused)
    assert fallback_for(s, state, by_vocab["spray_from_nozzle"]) == "bottle_on_rock"
    assert fallback_for(s, state, by_vocab["hero_push_in"]) == "bottle_on_rock"
    assert fallback_for(s, state, by_vocab["hand_cuff"]) == "bottle_on_rock"

    # The whole chain in use: the first unused safe product shot of the vocabulary.
    hand = by_vocab["hand_cuff"]
    hand.vocab, hand.shows_character = "bottle_on_rock", False
    assert fallback_for(s, state, by_vocab["spray_from_nozzle"]) == "cap_lift"
    # Nothing unused left: a repeated shot beats a hole (first usable chain entry).
    by_vocab["water_drop_leaf"].vocab = "cap_lift"
    assert fallback_for(s, state, by_vocab["spray_from_nozzle"]) == "hero_push_in"


def test_storyboard_vocabulary_drops_entries_the_model_renders_badly() -> None:
    from adsmith.config import Settings  # noqa: PLC0415

    vocab = Settings().vocabulary
    kling = vocab.for_model("kling_v3_standard")
    assert "light_sweep" not in kling.shots and "rim_light_reveal" not in kling.shots
    assert "light_sweep" in vocab.for_model("ltx_23_fast").shots


async def test_concurrent_fallbacks_never_pick_the_same_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # hand_cuff and hero_push_in both fall back to bottle_on_rock first; decided one at a time,
    # the second takes the next unused safe shot (montage run 1 had two bottle_on_rock shots).
    monkeypatch.setattr(MockCritic, "review_keyframe", _gate_failing({"hand_cuff", "hero_push_in"}))
    settings = fast_settings(tmp_path, keyframe_max_tries=2, scene_concurrency=9)
    h = make_harness(settings, scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.storyboard is not None
    swapped = [s.vocab or "" for s in state.storyboard.scenes if s.version == 2]
    assert sorted(swapped) == ["bottle_on_rock", "cap_lift"]
    vocabs = [s.vocab for s in state.storyboard.scenes]
    assert len(vocabs) == len(set(vocabs))

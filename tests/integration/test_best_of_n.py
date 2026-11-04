"""Best-of-N clips (hero mode, ADR-0003 addendum 2): N candidates from the same approved keyframe,
reserved up front, scored by the critic; the best one with no hard failure wins. If none passes,
the scene fails (no extra rescue)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from adsmith.agent.qa import MockCritic
from adsmith.domain.enums import ScenePhase, SceneStatus, Stage
from adsmith.domain.events import EventType
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAChecks, QAVerdict
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene
from tests.media_helpers import REQUEST, Harness, fast_settings, make_harness

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

OK = QAChecks(
    product_visible=True, matches_shot_spec=True, lighting_matches=True,
    continuity_ok=True, artifacts_free=True, brand_safe=True,
)  # fmt: skip


class ScoreByCandidate(MockCritic):
    """Scores each clip by its candidate index; optionally fails a hard check on some."""

    def __init__(self, scores: dict[int, float], alcohol: set[int] | None = None) -> None:
        self.scores, self.alcohol, self.seen = scores, alcohol or set(), 0

    async def review(self, state: RunState, scene: Scene) -> tuple[QAVerdict, list[LLMCallRecord]]:
        att = next(a for a in scene.attempts if a.artifact == scene.clip)
        k = att.params["candidate"]
        assert isinstance(k, int)
        self.seen += 1
        checks = OK.model_copy(update={"alcohol_free": k not in self.alcohol})
        v = QAVerdict(score=self.scores[k], checks=checks, frames_inspected=3, threshold=0.7)
        return v, []


def hero(h: Harness, critic: MockCritic) -> None:
    original = h.rt.scene_runner

    def runner(run: RunState):  # type: ignore[no-untyped-def]
        r = original(run)
        r.critic = critic
        return r

    h.rt.scene_runner = runner  # type: ignore[method-assign]


async def run_hero(h: Harness) -> RunState:
    run = h.orch.create_run(REQUEST, interactive=False, hero=True)
    assert run.config.clip_candidates == 2
    return await h.orch.run_until_blocked(run.id)


async def test_best_candidate_wins_and_the_other_is_kept(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    critic = ScoreByCandidate({0: 0.78, 1: 0.88})
    hero(h, critic)
    state = await run_hero(h)
    assert state.stage is Stage.DONE and state.storyboard is not None
    n = len(state.storyboard.scenes)
    assert h.video.calls == 2 * n and critic.seen == 2 * n
    for scene in state.storyboard.scenes:
        clips = [a for a in scene.attempts if a.phase is ScenePhase.CLIP]
        assert len(clips) == 2 and len({a.artifact.sha256 for a in clips if a.artifact}) == 2
        winner = next(a for a in clips if a.artifact == scene.clip)
        assert winner.params["candidate"] == 1 and scene.qa and scene.qa.score == 0.88
        loser = next(a for a in clips if a is not winner)
        assert loser.artifact is not None and loser.qa_score == 0.78  # kept and recorded
    ev = [e for e in h.rt.storage.events.tail(state.id) if e.payload.get("candidates")]
    decided = [e for e in ev if "winner" in e.payload]
    assert len(decided) == n and all(e.payload["winner"] == 2 for e in decided)
    assert state.budget.reserved_usd == 0


async def test_hard_failure_loses_even_with_a_higher_score(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    hero(h, ScoreByCandidate({0: 0.80, 1: 0.92}, alcohol={1}))
    state = await run_hero(h)
    assert state.stage is Stage.DONE and state.storyboard is not None
    for scene in state.storyboard.scenes:
        winner = next(a for a in scene.attempts if a.artifact == scene.clip)
        assert winner.params["candidate"] == 0


async def test_no_passing_candidate_fails_the_scene_without_rescue(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    hero(h, ScoreByCandidate({0: 0.5, 1: 0.6}))
    state = await run_hero(h)
    assert state.storyboard is not None
    assert all(s.status is SceneStatus.FAILED for s in state.storyboard.scenes)
    assert h.video.calls == 2 * len(state.storyboard.scenes)  # no third clip
    errs = [e for e in h.rt.storage.events.tail(state.id) if e.type is EventType.SCENE_STATUS]
    assert any("no candidate passed" in e.summary for e in errs)


async def test_both_candidates_are_reserved_up_front(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    hero(h, ScoreByCandidate({0: 0.9, 1: 0.8}))
    state = await run_hero(h)
    started = [
        e
        for e in h.rt.storage.events.tail(state.id)
        if e.type is EventType.ATTEMPT_STARTED and "best-of-2" in e.summary
    ]
    cands = [e.payload["candidates"] for e in started]
    assert started and all(isinstance(c, list) and len(c) == 2 for c in cands)
    assert all("reserved $" in e.summary for e in started)


def test_normal_runs_default_to_one_candidate(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    assert h.orch.create_run(REQUEST, interactive=False).config.clip_candidates == 1

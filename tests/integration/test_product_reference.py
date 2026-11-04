"""Locked product reference (ADR-0003 addendum 3): one gated packshot of the bible's product
conditions every keyframe that shows the product; scenes without the product use plain
text-to-image. A rejected packshot is retried with the critic's hint; if none passes, product
keyframes fall back to plain text-to-image."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from adsmith.agent.qa import MockCritic
from adsmith.domain.enums import ScenePhase, Stage
from adsmith.domain.generation import Artifact
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAChecks, QAIssue, QAVerdict
from adsmith.domain.run import RunState
from tests.media_helpers import REQUEST, Harness, fast_settings, make_harness

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

OK = QAChecks(
    product_visible=True, matches_shot_spec=True, lighting_matches=True,
    continuity_ok=True, artifacts_free=True, brand_safe=True,
)  # fmt: skip


class RejectFirstPackshots(MockCritic):
    def __init__(self, rejections: int) -> None:
        self.rejections, self.seen = rejections, 0

    async def review_product_reference(
        self, state: RunState, image: Artifact, prompt: str
    ) -> tuple[QAVerdict, list[LLMCallRecord]]:
        self.seen += 1
        if self.seen <= self.rejections:
            issue = QAIssue(
                category="product", severity="major",
                observation="The cap is black, not brushed gold.",
                fix="Make the cap brushed gold.",
            )  # fmt: skip
            v = QAVerdict(
                score=0.4, checks=OK.model_copy(update={"continuity_ok": False}),
                issues=[issue], revised_prompt_hint="The cap is brushed gold metal.",
                frames_inspected=1, threshold=0.7,
            )  # fmt: skip
            return v, []
        return QAVerdict(score=0.9, checks=OK, frames_inspected=1, threshold=0.7), []


def use_critic(h: Harness, critic: MockCritic) -> None:
    original = h.rt.scene_runner

    def runner(run: RunState):  # type: ignore[no-untyped-def]
        r = original(run)
        r.critic = critic
        return r

    h.rt.scene_runner = runner  # type: ignore[method-assign]


async def run(h: Harness, *, ref: bool = True) -> RunState:
    state = h.orch.create_run(REQUEST, interactive=False, product_reference=ref)
    return await h.orch.run_until_blocked(state.id)


async def test_product_keyframes_are_conditioned_on_the_locked_reference(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    state = await run(h)
    assert state.stage is Stage.DONE and state.storyboard is not None
    pr = state.product_reference
    assert pr is not None and pr.approved and pr.artifact is not None and pr.tries == 1
    product_scenes = [s for s in state.storyboard.scenes if s.shows_product]
    other_scenes = [s for s in state.storyboard.scenes if not s.shows_product]
    assert product_scenes and other_scenes
    assert h.reference is not None and h.reference.calls == len(product_scenes)
    for sc in product_scenes:
        kf = next(a for a in sc.attempts if a.phase is ScenePhase.KEYFRAME)
        assert kf.model == "mock-reference"
        assert "exactly as it is" in kf.prompt_used  # the keyframe_reference wrapper
    for sc in other_scenes:
        kf = next(a for a in sc.attempts if a.phase is ScenePhase.KEYFRAME)
        assert kf.model == "mock-keyframe"
    assert state.budget.reserved_usd == 0


async def test_rejected_packshot_is_retried_with_the_critic_hint(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    use_critic(h, RejectFirstPackshots(1))
    state = await run(h)
    pr = state.product_reference
    assert pr is not None and pr.approved and pr.tries == 2 and len(pr.rejected) == 1
    assert "brushed gold metal" in pr.prompt
    assert pr.cost_usd > 0


async def test_no_passing_packshot_falls_back_to_plain_keyframes(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    use_critic(h, RejectFirstPackshots(99))
    state = await run(h)
    assert state.stage is Stage.DONE and state.storyboard is not None
    pr = state.product_reference
    assert pr is not None and not pr.approved and pr.tries == 3
    assert h.reference is not None and h.reference.calls == 0
    events = h.rt.storage.events.tail(state.id, limit=10_000)
    assert any("fall back to plain text-to-image" in e.summary for e in events)


async def test_off_by_default(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    state = await run(h, ref=False)
    assert state.product_reference is None
    assert h.reference is None or h.reference.calls == 0


async def test_a_regated_stored_packshot_can_be_adopted(tmp_path: Path) -> None:
    from adsmith.agent import commands  # noqa: PLC0415
    from adsmith.agent.commands import CommandRejectedError  # noqa: PLC0415

    h = make_harness(fast_settings(tmp_path))
    use_critic(h, RejectFirstPackshots(99))
    state = await run(h)
    pr = state.product_reference
    assert pr is not None and not pr.approved and pr.rejected
    passing = QAVerdict(score=0.85, checks=OK, frames_inspected=1, threshold=0.7)
    failing = QAVerdict(score=0.4, checks=OK, frames_inspected=1, threshold=0.7)
    with pytest.raises(CommandRejectedError):
        commands.adopt_product_reference(
            h.rt.storage, state.id, pr.rejected[1], failing, reason="test"
        )
    foreign = pr.rejected[1].model_copy(update={"sha256": "0" * 64})
    with pytest.raises(CommandRejectedError):
        commands.adopt_product_reference(h.rt.storage, state.id, foreign, passing, reason="test")
    state = commands.adopt_product_reference(
        h.rt.storage, state.id, pr.rejected[1], passing, reason="re-gated"
    )
    assert state.product_reference is not None and state.product_reference.approved
    assert state.product_reference.artifact == pr.rejected[1]


async def test_composite_mode_makes_the_scene_first_then_swaps_the_product(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path, product_reference_mode="composite"))
    state = await run(h)
    assert state.stage is Stage.DONE and state.storyboard is not None
    product_scenes = [s for s in state.storyboard.scenes if s.shows_product]
    assert h.composite is not None and h.composite.calls == len(product_scenes)
    assert h.reference is not None and h.reference.calls == 0  # direct mode not used
    for sc in product_scenes:
        kf = next(a for a in sc.attempts if a.phase is ScenePhase.KEYFRAME)
        assert kf.model == "mock-keyframe+mock-composite"
        assert "small in the hand" in kf.prompt_used and kf.cost_usd > 0.04  # plain + swap

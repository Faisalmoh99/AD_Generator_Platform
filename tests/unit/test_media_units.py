"""Units under the scene pipeline: chaos, fail plan, duration snapping, adapters, budget ledger."""

from __future__ import annotations

import pytest

from adsmith.agent.recovery.errors import (
    ContentPolicyError,
    PermanentProviderError,
    TransientProviderError,
)
from adsmith.config import ChaosConfig
from adsmith.domain.budget import BudgetLedger, BudgetRefusedError
from adsmith.domain.enums import FailureKind, ScenePhase
from adsmith.providers.adapters.base import ADAPTERS
from adsmith.providers.media.base import MediaLabel, VideoCapabilities
from adsmith.providers.media.chaos import ChaosPolicy
from adsmith.providers.media.mock import FailPlan, parse_fail_plan
from tests.factories import make_prompt


def label(order: int = 1, phase: ScenePhase = ScenePhase.CLIP, attempt: int = 1) -> MediaLabel:
    return MediaLabel(scene_order=order, scene_version=1, phase=phase, attempt_no=attempt)


# ----------------------------------------------------------------------------- chaos


def test_chaos_is_deterministic_and_order_independent() -> None:
    policy = ChaosPolicy(ChaosConfig(seed=7), enabled=True)
    a = [policy.decide(label(o, p)) for o in range(1, 5) for p in ScenePhase]
    b = [policy.decide(label(o, p)) for o in reversed(range(1, 5)) for p in ScenePhase]
    b = [b[i] for i in [6, 7, 4, 5, 2, 3, 0, 1]]  # same keys, different evaluation order
    assert a == b


def test_chaos_disabled_or_retry_never_faults() -> None:
    cfg = ChaosConfig(seed=1, transient=1.0, policy=0, quality=0)
    assert ChaosPolicy(cfg, enabled=False).decide(label()) is None
    on = ChaosPolicy(cfg, enabled=True)
    assert on.decide(label()) is FailureKind.TRANSIENT
    assert on.decide(label(), transport_try=2) is None  # first_attempt_only
    assert on.decide(label(attempt=2)) is None


def test_quality_faults_only_on_clips() -> None:
    cfg = ChaosConfig(seed=1, transient=0, policy=0, quality=1.0)
    on = ChaosPolicy(cfg, enabled=True)
    assert on.decide(label(phase=ScenePhase.KEYFRAME)) is None
    assert on.decide(label(phase=ScenePhase.CLIP)) is FailureKind.QUALITY_REJECTED


# ----------------------------------------------------------------------------- fail plan


def test_fail_plan_parsing_and_counts() -> None:
    plan = FailPlan(parse_fail_plan("2:clip:permanent, 1:keyframe:transient*2,3:clip:policy*1"))
    kf1 = label(1, ScenePhase.KEYFRAME)
    for _ in range(2):
        with pytest.raises(TransientProviderError):
            plan.check(kf1)
    plan.check(kf1)  # third call passes
    with pytest.raises(ContentPolicyError):
        plan.check(label(3))
    plan.check(label(3))
    for _ in range(3):
        with pytest.raises(PermanentProviderError):
            plan.check(label(2))


def test_fail_plan_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown fail kind"):
        parse_fail_plan("1:clip:explode")


# ----------------------------------------------------------------------------- durations/adapters

KLING = VideoCapabilities(min_duration_s=3, max_duration_s=15)
VEO = VideoCapabilities(min_duration_s=4, max_duration_s=8, allowed_durations_s=(4, 6, 8))


@pytest.mark.parametrize(
    ("caps", "scene", "rendered"),
    [(KLING, 3.5, 4), (KLING, 2.0, 3), (KLING, 20, 15), (VEO, 3.5, 4), (VEO, 4.5, 6), (VEO, 9, 8)],
)
def test_duration_snapping(caps: VideoCapabilities, scene: float, rendered: float) -> None:
    assert caps.snap(scene) == rendered


def test_adapters_map_one_prompt_onto_model_rules() -> None:
    p = make_prompt(motion_prompt="slow dolly in", negative_prompt="text, watermark")
    kling = ADAPTERS["kling"].adapt(p, 3.5, "9:16", KLING)
    assert (kling.prompt, kling.negative_prompt, kling.duration_s) == (
        "slow dolly in",
        "text, watermark",
        4,
    )
    assert kling.params["cfg_scale"] == 0.5
    seed = ADAPTERS["seedance"].adapt(p, 3.5, "9:16", KLING)
    assert seed.negative_prompt == "" and "Avoid: text, watermark" in seed.prompt
    veo = ADAPTERS["veo"].adapt(p, 4.5, "16:9", VEO)
    assert veo.duration_s == 6 and veo.params["aspect_ratio"] == "16:9"


def test_adapter_truncates_long_prompts_on_a_word_boundary() -> None:
    caps = VideoCapabilities(min_duration_s=3, max_duration_s=15, max_prompt_chars=40)
    out = ADAPTERS["kling"].adapt(make_prompt(motion_prompt="word " * 30), 4, "9:16", caps)
    assert len(out.prompt) <= 40 and out.prompt.endswith("…")


# ----------------------------------------------------------------------------- budget ledger


def ledger(limit: float = 1.0, attempts: int = 10) -> BudgetLedger:
    return BudgetLedger(limit_usd=limit, max_attempts_per_scene=6, max_total_attempts=attempts)


def test_reserve_settle_release_accounting() -> None:
    b = ledger()
    b.reserve("a1", "video", 0.45, "s1")
    b.reserve("a2", "image", 0.03, "s2")
    assert b.reserved_usd == pytest.approx(0.48) and b.remaining_usd == pytest.approx(0.52)
    b.settle("a1", 0.448)
    b.release("a2")
    assert b.reserved_usd == pytest.approx(0) and b.spent_usd == pytest.approx(0.448)
    assert b.by_category == {"video": pytest.approx(0.448), "image": 0.0}
    with pytest.raises(KeyError):
        b.settle("a1", 0.1)  # cannot settle twice


def test_reservations_refuse_over_budget_and_over_attempts() -> None:
    b = ledger(limit=0.5, attempts=2)
    b.reserve("a1", "video", 0.45, None)
    with pytest.raises(BudgetRefusedError, match="exceeds the remaining"):
        b.reserve(
            "a2", "video", 0.45, None
        )  # reserved money counts, so concurrency can't overshoot
    b.reserve("a3", "image", 0.03, None)
    with pytest.raises(BudgetRefusedError, match="attempt limit"):
        b.reserve("a4", "image", 0.01, None)

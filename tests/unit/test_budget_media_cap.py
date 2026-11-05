"""The media cap: fal spend is bounded by the wallet headroom; LLM spend does not consume it."""

from __future__ import annotations

import pytest

from adsmith.domain.budget import BudgetLedger, BudgetRefusedError


def ledger(media: float | None = 0.10) -> BudgetLedger:
    return BudgetLedger(
        limit_usd=2.0, media_limit_usd=media, max_attempts_per_scene=6, max_total_attempts=40
    )


def test_llm_spend_does_not_consume_the_media_cap() -> None:
    b = ledger()
    b.record_actual("llm", 1.5, ref="llm1")
    b.reserve("kf1", "image", 0.04, "s1")
    b.settle("kf1", 0.04)
    b.reserve("kf2", "image", 0.04, "s2")  # 0.08 of 0.10 committed: fine
    assert b.media_remaining_usd == pytest.approx(0.02)


def test_media_beyond_the_cap_is_refused_before_any_call() -> None:
    b = ledger()
    b.reserve("kf1", "image", 0.065, "s1")
    with pytest.raises(BudgetRefusedError, match="media cap"):
        b.reserve("kf2", "image", 0.04, "s2")
    b.release("kf1")  # a failed call frees its share
    b.reserve("kf2", "image", 0.04, "s2")


def test_no_media_cap_means_only_the_total_applies() -> None:
    b = ledger(None)
    b.reserve("v1", "video", 1.9, "s1")
    assert b.media_remaining_usd is None
    with pytest.raises(BudgetRefusedError, match="of the \\$2 budget"):
        b.reserve("v2", "video", 0.2, "s2")

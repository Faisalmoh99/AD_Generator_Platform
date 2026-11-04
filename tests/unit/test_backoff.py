from __future__ import annotations

import random

import pytest

from adsmith.agent.recovery.backoff import BackoffPolicy, retry_transient
from adsmith.agent.recovery.errors import (
    ContentPolicyError,
    RateLimitedError,
    TransientProviderError,
)


class Flaky:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


async def run(fn: Flaky, seed: int = 0) -> tuple[str, list[float]]:
    delays: list[float] = []

    async def sleep(d: float) -> None:
        delays.append(d)

    return await retry_transient(fn, sleep=sleep, rng=random.Random(seed)), delays


def test_full_jitter_bounds() -> None:
    p = BackoffPolicy(base_s=1.0, cap_s=20.0)
    rng = random.Random(1)
    for n in range(1, 10):
        for _ in range(50):
            assert 0.0 <= p.delay(n, rng) <= min(20.0, 2 ** (n - 1))


async def test_deterministic_with_seed() -> None:
    a = await run(Flaky(TransientProviderError("x"), TransientProviderError("x")), seed=3)
    b = await run(Flaky(TransientProviderError("x"), TransientProviderError("x")), seed=3)
    assert a == b


async def test_retry_after_is_honoured_and_capped() -> None:
    _, delays = await run(
        Flaky(RateLimitedError("429", retry_after_s=7), RateLimitedError("429", retry_after_s=999))
    )
    assert delays == [7.0, 60.0]


async def test_kinds_have_independent_budgets() -> None:
    errors = [TransientProviderError("t")] * 3 + [RateLimitedError("r")] * 4
    result, delays = await run(Flaky(*errors))
    assert result == "ok" and len(delays) == 7


async def test_exhaustion_reraises_last_error() -> None:
    fn = Flaky(*[TransientProviderError(f"t{i}") for i in range(10)])
    with pytest.raises(TransientProviderError, match="t3"):
        await run(fn)
    assert fn.calls == 4


async def test_non_retryable_passes_through_immediately() -> None:
    fn = Flaky(ContentPolicyError("nope"))
    with pytest.raises(ContentPolicyError):
        await run(fn)
    assert fn.calls == 1

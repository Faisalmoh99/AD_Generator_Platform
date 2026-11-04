"""Bounded exponential backoff with full jitter for transient and rate-limit failures.

Full jitter (``uniform(0, min(cap, base * 2**n))``) spreads retries from concurrent scene tasks so
they don't hit a recovering provider in lockstep. ``sleep`` and ``rng`` are injected, so tests
are instant and deterministic.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from adsmith.agent.recovery.errors import AgentError, RateLimitedError, TransientProviderError

Sleep = Callable[[float], Awaitable[None]]
OnRetry = Callable[[AgentError, int, float], None]  # (error, retry number, delay)


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    max_attempts: int = 4  # total tries, including the first
    base_s: float = 1.0
    cap_s: float = 20.0
    max_retry_after_s: float = 60.0  # never trust a server to park us longer than this

    def delay(self, retry_no: int, rng: random.Random) -> float:
        """Delay before retry number ``retry_no`` (1-based)."""
        return rng.uniform(0.0, min(self.cap_s, self.base_s * 2 ** (retry_no - 1)))


TRANSIENT_POLICY = BackoffPolicy(max_attempts=4)
RATE_LIMIT_POLICY = BackoffPolicy(max_attempts=5, base_s=2.0, cap_s=30.0)


async def retry_transient[T](
    fn: Callable[[], Awaitable[T]],
    *,
    sleep: Sleep,
    rng: random.Random,
    on_retry: OnRetry | None = None,
    transient: BackoffPolicy = TRANSIENT_POLICY,
    rate_limited: BackoffPolicy = RATE_LIMIT_POLICY,
) -> T:
    """Run ``fn``. Retry only retryable errors, each kind under its own bounded policy.
    Non-retryable errors propagate immediately; the last retryable error propagates when its
    policy is exhausted."""
    tries: dict[type[AgentError], int] = {}
    err: AgentError
    while True:
        try:
            return await fn()
        except RateLimitedError as exc:
            policy, err = rate_limited, exc
            n = tries[RateLimitedError] = tries.get(RateLimitedError, 0) + 1
            if n >= policy.max_attempts:
                raise
            delay = (
                min(exc.retry_after_s, policy.max_retry_after_s)
                if exc.retry_after_s is not None
                else policy.delay(n, rng)
            )
        except TransientProviderError as exc:
            policy, err = transient, exc
            n = tries[TransientProviderError] = tries.get(TransientProviderError, 0) + 1
            if n >= policy.max_attempts:
                raise
            delay = policy.delay(n, rng)
        if on_retry is not None:
            on_retry(err, n, delay)
        await sleep(delay)

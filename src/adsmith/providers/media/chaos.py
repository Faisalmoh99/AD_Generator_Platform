"""Chaos Mode: deterministic, seeded fault injection for live recovery demos.

Each decision is a pure function of ``(seed, scene order, scene version, phase, attempt)``, so a
demo replays identically regardless of task scheduling, and every injected fault is labelled in
the trace (``chaos_injected`` / ``injected_fault``). With ``first_attempt_only`` only an attempt's
first try can be faulted, so recovery always gets a fair chance.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from adsmith.config import ChaosConfig
from adsmith.domain.enums import FailureKind, ScenePhase
from adsmith.providers.media.base import MediaLabel


@dataclass(frozen=True, slots=True)
class ChaosPolicy:
    config: ChaosConfig
    enabled: bool

    def decide(self, label: MediaLabel, *, transport_try: int = 1) -> FailureKind | None:
        """Fault for this call, if any. ``transport_try`` counts backoff retries in an attempt."""
        if not self.enabled:
            return None
        if self.config.first_attempt_only and (label.attempt_no > 1 or transport_try > 1):
            return None
        rng = random.Random(
            f"{self.config.seed}:{label.scene_order}:{label.scene_version}:{label.phase}:"
            f"{label.attempt_no}:{transport_try}"
        )
        roll = rng.random()
        c = self.config
        if roll < c.transient:
            return FailureKind.TRANSIENT
        if roll < c.transient + c.policy:
            return FailureKind.CONTENT_POLICY
        if label.phase is ScenePhase.CLIP and roll < c.transient + c.policy + c.quality:
            return FailureKind.QUALITY_REJECTED
        return None

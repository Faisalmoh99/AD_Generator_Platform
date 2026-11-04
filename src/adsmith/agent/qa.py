"""QA critics. ``MockCritic`` (default with mock media) is deterministic: it passes clips unless
Chaos Mode injected a quality fault into the attempt that produced them, in which case it returns
concrete, prompt-actionable issues, exactly like the LLM vision critic (M6) will."""

from __future__ import annotations

from adsmith.domain.enums import FailureKind, ScenePhase
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAChecks, QAIssue, QAVerdict
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene

_ALL_OK = QAChecks(
    product_visible=True,
    matches_shot_spec=True,
    lighting_matches=True,
    continuity_ok=True,
    artifacts_free=True,
    brand_safe=True,
)


class MockCritic:
    async def review(self, state: RunState, scene: Scene) -> tuple[QAVerdict, list[LLMCallRecord]]:
        clip = scene.last_attempt(ScenePhase.CLIP)
        threshold = state.config.qa_threshold
        faulty = clip is not None and clip.injected_fault is FailureKind.QUALITY_REJECTED
        if not faulty:
            return QAVerdict(score=0.9, checks=_ALL_OK, frames_inspected=3, threshold=threshold), []
        issue = QAIssue(
            category="motion",
            severity="major",
            observation="Camera movement is too fast and the subject smears in the middle frames.",
            fix="Slow the camera move to a gentle, steady pace and hold the subject sharp.",
            frame_index=1,
        )
        verdict = QAVerdict(
            score=0.45,
            checks=_ALL_OK.model_copy(update={"artifacts_free": False}),
            issues=[issue],
            revised_prompt_hint="Halve the camera speed; keep the final second still.",
            frames_inspected=3,
            threshold=threshold,
        )
        return verdict, []

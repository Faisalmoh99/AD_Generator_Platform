"""Scene stages (keyframe, generate, qa): each scene is an independent unit of work (ADR-0004).

Per scene, concurrently (bounded by ``scene_concurrency``):

    keyframe/clip attempt
      ├─ cache hit ───────────────────────────────▶ done, $0
      ├─ reserve budget (atomic with "start")  ──▶ refused → scene failed(budget), no call made
      ├─ provider call with backoff (transient / 429 retried inside the attempt)
      ├─ content policy ─▶ LLM rewrite (≤2) ───▶ new attempt
      ├─ exhausted / permanent ──────────────────▶ scene failed with a user-language reason
      └─ success ─▶ settle budget, store artifact ─▶ next phase
    qa: critic verdict ─ pass ─▶ approved
                       └ fail ─▶ LLM revision with the critic's fixes (≤2) ─▶ back to generate
                                 (motion-only fixes keep the keyframe)

Isolation: every state change is its own atomic commit through ``RunRepository.update`` with its
events, and each commit checks the scene version it was computed for (an edit in between
discards the result, never misapplies it). A failing scene, even through an unexpected bug, is
marked failed on its own; the others carry on.

This module (like the orchestrator) is allowed to commit state; nodes are not.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from adsmith.agent.context import NodeContext, render_window
from adsmith.agent.i18n import failure_message
from adsmith.agent.media import GatewayResult, MediaGateway, PreparedJob
from adsmith.agent.nodes.rewrite import ReasonKind, rewrite_prompt
from adsmith.agent.recovery.backoff import Sleep, retry_transient
from adsmith.agent.recovery.errors import AgentError, ContentPolicyError
from adsmith.domain.base import utcnow
from adsmith.domain.budget import BudgetRefusedError, CostCategory
from adsmith.domain.enums import FailureKind, RecoveryAction, ScenePhase, SceneStatus, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.generation import GenerationAttempt
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAVerdict
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene, SceneFailure, ScenePrompt
from adsmith.logging import get_logger
from adsmith.storage import StaleWriteError, Storage
from adsmith.storage.repo import Mutation

log = get_logger(__name__)

MAX_POLICY_REWRITES = 2
MAX_QA_REGENERATIONS = 2


class Critic(Protocol):
    async def review(
        self, state: RunState, scene: Scene
    ) -> tuple[QAVerdict, list[LLMCallRecord]]: ...


def _ev(
    run_id: str,
    stage: Stage,
    scene: Scene | None,
    type_: EventType,
    summary: str,
    level: Literal["info", "warn", "error"] = "info",
    **payload: object,
) -> AgentEvent:
    return AgentEvent.model_validate(
        {
            "run_id": run_id,
            "stage": stage,
            "scene_id": scene.id if scene else None,
            "type": type_,
            "summary": summary[:500],
            "level": level,
            "payload": payload,
        }
    )


@dataclass
class SceneStageRunner:
    storage: Storage
    gateway: MediaGateway
    node_ctx: NodeContext
    critic: Critic
    concurrency: int
    sleep: Sleep
    rng: random.Random

    # ------------------------------------------------------------------------ stage entry
    async def run(self, run_id: str, stage: Stage) -> None:
        state = self.storage.runs.load(run_id)
        assert state.storyboard is not None
        targets = [s.id for s in state.storyboard.scenes if self._wants(s, stage)]
        sem = asyncio.Semaphore(self.concurrency)

        async def guarded(scene_id: str) -> None:
            async with sem:
                await self._scene(run_id, scene_id, stage)

        results = await asyncio.gather(*(guarded(sid) for sid in targets), return_exceptions=True)
        for scene_id, outcome in zip(targets, results, strict=True):
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                log.error(
                    "scene worker crashed", run_id=run_id, scene_id=scene_id, exc_info=outcome
                )
                self._fail_scene(
                    run_id,
                    stage,
                    scene_id,
                    FailureKind.PERMANENT,
                    f"{type(outcome).__name__}: {outcome}",
                )

    @staticmethod
    def _wants(scene: Scene, stage: Stage) -> bool:
        if stage is Stage.QA:
            return scene.status is SceneStatus.QA_REVIEW
        if scene.status is not SceneStatus.PENDING or scene.prompt is None:
            return False
        return stage is Stage.GENERATE or scene.phase is ScenePhase.KEYFRAME

    async def _scene(self, run_id: str, scene_id: str, stage: Stage) -> None:
        if stage is Stage.QA:
            await self._qa(run_id, scene_id)
            return
        scene = self._load(run_id, scene_id)
        if scene.phase is ScenePhase.KEYFRAME and not await self._phase(
            run_id, scene_id, ScenePhase.KEYFRAME, stage
        ):
            return
        if stage is Stage.GENERATE:
            await self._phase(run_id, scene_id, ScenePhase.CLIP, stage)

    # ------------------------------------------------------------------------ one phase
    async def _phase(self, run_id: str, scene_id: str, phase: ScenePhase, stage: Stage) -> bool:  # noqa: PLR0911
        rewrites = 0
        while True:
            state = self.storage.runs.load(run_id)
            scene = self._find(state, scene_id)
            if scene.status is not SceneStatus.PENDING or scene.prompt is None:
                return False
            if scene.phase is not phase:
                return True  # already past this phase (e.g. keyframe kept after a QA fix)
            if scene.attempts_for_current_version() >= state.budget.max_attempts_per_scene:
                self._fail_scene(
                    run_id,
                    stage,
                    scene_id,
                    FailureKind.EXHAUSTED,
                    f"{state.budget.max_attempts_per_scene} attempts used",
                )
                return False

            prepared = self._prepare(state, scene, phase)
            attempt = GenerationAttempt(
                scene_id=scene.id,
                scene_version=scene.version,
                phase=phase,
                attempt_no=scene.attempts_for_current_version() + 1,
                provider=prepared.request.provider,
                model=prepared.request.model,
                request_hash=prepared.request.cache_key(),
                prompt_used=prepared.request.prompt,
                params=prepared.request.params,
            )
            if self.gateway.cached(prepared) is not None:
                result = await self.gateway.run(prepared, self._scratch(run_id))
                return self._succeed(run_id, stage, scene, attempt, result, reserved=False)

            if not self._start(run_id, stage, scene, attempt, prepared):
                return False
            tries = 0

            async def call(p: PreparedJob = prepared) -> GatewayResult:
                nonlocal tries
                tries += 1
                return await self.gateway.run(p, self._scratch(run_id), transport_try=tries)

            def on_retry(err: AgentError, n: int, delay: float, s: Scene = scene) -> None:
                self.storage.events.append(
                    run_id,
                    [
                        _ev(
                            run_id,
                            stage,
                            s,
                            EventType.RETRY_SCHEDULED,
                            level="warn",
                            summary=f"Scene {s.order} {phase}: {err.kind} ({err.message}); "
                            f"retry {n} in {delay:.2f}s (full-jitter backoff)",
                            kind=err.kind.value,
                            delay_s=round(delay, 3),
                            chaos_injected=err.injected,
                        )
                    ],
                )

            try:
                result = await retry_transient(
                    call, sleep=self.sleep, rng=self.rng, on_retry=on_retry
                )
            except ContentPolicyError as exc:
                give_up = rewrites >= MAX_POLICY_REWRITES
                if not self._attempt_failed(run_id, stage, scene, attempt, exc, give_up=give_up):
                    return False
                if give_up:
                    return False
                rewrites += 1
                if not await self._revise(run_id, stage, scene_id, "content_policy", exc.message):
                    return False
                continue
            except AgentError as exc:  # transient/429 exhausted, or permanent
                self._attempt_failed(run_id, stage, scene, attempt, exc, give_up=True)
                return False
            return self._succeed(run_id, stage, scene, attempt, result, reserved=True)

    # ------------------------------------------------------------------------ commits
    def _start(
        self,
        run_id: str,
        stage: Stage,
        scene: Scene,
        attempt: GenerationAttempt,
        prepared: PreparedJob,
    ) -> bool:
        category: CostCategory = "image" if attempt.phase is ScenePhase.KEYFRAME else "video"

        def mutate(s: RunState) -> list[AgentEvent]:
            sc = self._same_version(s, scene)
            s.budget.reserve(attempt.id, category, prepared.est_usd, sc.id)
            sc.start()
            sc.attempts.append(attempt)
            return [
                _ev(
                    run_id,
                    stage,
                    sc,
                    EventType.ATTEMPT_STARTED,
                    f"Scene {sc.order} {attempt.phase} attempt {prepared.job.label.attempt_no} "
                    f"(reserved ${prepared.est_usd:.3f})",
                    attempt_id=attempt.id,
                    est_usd=prepared.est_usd,
                )
            ]

        try:
            self.storage.runs.update(
                run_id, mutate, reason=f"scene {scene.order} {attempt.phase} start"
            )
        except BudgetRefusedError as exc:
            self._fail_scene(run_id, stage, scene.id, FailureKind.BUDGET_EXCEEDED, str(exc))
            return False
        except StaleWriteError:
            return False
        return True

    def _succeed(
        self,
        run_id: str,
        stage: Stage,
        scene: Scene,
        attempt: GenerationAttempt,
        result: GatewayResult,
        *,
        reserved: bool,
    ) -> bool:
        def mutate(s: RunState) -> list[AgentEvent]:
            sc = self._same_version(s, scene)
            if not reserved:
                sc.start()
                sc.attempts.append(attempt)
            att = self._attempt(sc, attempt.id)
            att.outcome = "cache_hit" if result.cache_hit else "success"
            att.finished_at, att.artifact, att.cost_usd = utcnow(), result.artifact, result.cost_usd
            att.injected_fault = result.injected_fault
            att.chaos_injected = result.injected_fault is not None
            if reserved:
                s.budget.settle(attempt.id, result.cost_usd)
            if attempt.phase is ScenePhase.KEYFRAME:
                sc.complete_keyframe(result.artifact)
            else:
                sc.complete_clip(result.artifact)
            if result.cache_hit:
                return [
                    _ev(
                        run_id,
                        stage,
                        sc,
                        EventType.CACHE_HIT,
                        f"Scene {sc.order} {attempt.phase}: cache hit, $0 (no provider call)",
                        request_hash=attempt.request_hash,
                    )
                ]
            note = " [chaos: quality fault injected]" if result.injected_fault else ""
            return [
                _ev(
                    run_id,
                    stage,
                    sc,
                    EventType.ATTEMPT_FINISHED,
                    f"Scene {sc.order} {attempt.phase} ready (${result.cost_usd:.3f}){note}",
                    attempt_id=attempt.id,
                    cost_usd=result.cost_usd,
                    injected_fault=result.injected_fault.value if result.injected_fault else None,
                )
            ]

        try:
            self.storage.runs.update(
                run_id, mutate, reason=f"scene {scene.order} {attempt.phase} done"
            )
        except StaleWriteError:
            return False  # edited meanwhile: discarded on purpose; the artifact stays cached
        return True

    def _attempt_failed(
        self,
        run_id: str,
        stage: Stage,
        scene: Scene,
        attempt: GenerationAttempt,
        exc: AgentError,
        *,
        give_up: bool,
    ) -> bool:
        def mutate(s: RunState) -> list[AgentEvent]:
            sc = self._same_version(s, scene)
            att = self._attempt(sc, attempt.id)
            att.outcome, att.finished_at = "failed", utcnow()
            att.failure_kind, att.error_message = exc.kind, exc.message[:2000]
            att.chaos_injected = exc.injected
            att.injected_fault = exc.kind if exc.injected else None
            s.budget.release(attempt.id)
            if give_up:
                att.recovery_action = RecoveryAction.GIVE_UP
                sc.fail(self._failure(s, exc.kind, stage, exc.message))
                return [
                    _ev(
                        run_id,
                        stage,
                        sc,
                        EventType.SCENE_STATUS,
                        level="error",
                        summary=f"Scene {sc.order} failed: {exc.kind} ({exc.message}). "
                        "Other scenes continue.",
                        kind=exc.kind.value,
                        chaos_injected=exc.injected,
                    )
                ]
            att.recovery_action = RecoveryAction.POLICY_REWRITE
            sc.release_lease()
            return [
                _ev(
                    run_id,
                    stage,
                    sc,
                    EventType.RECOVERY,
                    level="warn",
                    summary=f"Scene {sc.order} {attempt.phase}: content policy rejection "
                    f"({exc.message}); rewriting the prompt",
                    strategy="policy_rewrite",
                    chaos_injected=exc.injected,
                )
            ]

        try:
            self.storage.runs.update(run_id, mutate, reason=f"scene {scene.order} attempt failed")
        except StaleWriteError:
            return False
        return True

    async def _revise(
        self,
        run_id: str,
        stage: Stage,
        scene_id: str,
        reason: ReasonKind,
        detail: str,
        verdict: QAVerdict | None = None,
    ) -> bool:
        state = self.storage.runs.load(run_id)
        scene, bible = self._find(state, scene_id), state.bible
        assert bible is not None
        try:
            render_s, _ = render_window(self.node_ctx.settings, state, scene.duration_s)
            res = await rewrite_prompt(
                self.node_ctx,
                scene,
                bible,
                reason=reason,
                detail=detail,
                render_s=render_s,
                keep_keyframe=None if verdict is None else not verdict.needs_new_keyframe,
            )
        except AgentError as exc:
            self._fail_scene(
                run_id,
                stage,
                scene_id,
                exc.kind,
                f"rewrite failed: {exc.message}",
                calls=exc.llm_calls,
            )
            return False
        return self._apply_revision(run_id, stage, scene, res.value, res.calls, reason, verdict)

    def _apply_revision(
        self,
        run_id: str,
        stage: Stage,
        scene: Scene,
        prompt: ScenePrompt,
        calls: Sequence[LLMCallRecord],
        reason: ReasonKind,
        verdict: QAVerdict | None,
    ) -> bool:
        def mutate(s: RunState) -> list[AgentEvent]:
            sc = self._same_version(s, scene)
            events = self._book(s, calls, stage, sc)
            if verdict is not None:
                sc.reject(verdict)
            sc.set_prompt(prompt)
            kept = "kept the keyframe" if sc.keyframe is not None else "new keyframe needed"
            label = "Policy rewrite" if reason == "content_policy" else "QA regeneration"
            events.append(
                _ev(
                    run_id,
                    stage,
                    sc,
                    EventType.RECOVERY,
                    f"{label} for scene {sc.order}: prompt revised ({kept})",
                    strategy="policy_rewrite" if reason == "content_policy" else "qa_regenerate",
                    keyframe_kept=sc.keyframe is not None,
                )
            )
            return events

        try:
            self.storage.runs.update(
                run_id, mutate, reason=f"scene {scene.order} revised ({reason})"
            )
        except StaleWriteError:
            return False
        return True

    def _fail_scene(
        self,
        run_id: str,
        stage: Stage,
        scene_id: str,
        kind: FailureKind,
        debug: str,
        calls: Sequence[LLMCallRecord] = (),
    ) -> None:
        def mutate(s: RunState) -> list[AgentEvent]:
            sc = self._find(s, scene_id)
            events = self._book(s, calls, stage, sc)
            if sc.status in {SceneStatus.FAILED, SceneStatus.APPROVED, SceneStatus.STALE}:
                return events
            for att in sc.attempts:  # release any reservation still open for this scene
                if att.outcome is None:
                    att.outcome, att.finished_at = "failed", utcnow()
                    att.failure_kind, att.error_message = kind, debug[:2000]
                    with contextlib.suppress(KeyError):
                        s.budget.release(att.id)
            sc.fail(self._failure(s, kind, stage, debug))
            events.append(
                _ev(
                    run_id,
                    stage,
                    sc,
                    EventType.SCENE_STATUS,
                    level="error",
                    summary=f"Scene {sc.order} failed: {kind} ({debug[:200]}). "
                    "Other scenes continue.",
                    kind=kind.value,
                )
            )
            return events

        with contextlib.suppress(StaleWriteError):
            self.storage.runs.update(run_id, mutate, reason=f"scene failed ({kind})")

    # ------------------------------------------------------------------------ QA
    async def _qa(self, run_id: str, scene_id: str) -> None:
        state = self.storage.runs.load(run_id)
        scene = self._find(state, scene_id)
        try:
            verdict, calls = await self.critic.review(state, scene)
        except AgentError as exc:
            self._fail_scene(
                run_id, Stage.QA, scene_id, exc.kind, f"critic: {exc.message}", exc.llm_calls
            )
            return
        clip = scene.last_attempt(ScenePhase.CLIP)
        rejections = sum(
            1
            for a in scene.attempts
            if a.scene_version == scene.version
            and a.phase is ScenePhase.CLIP
            and (
                a.qa_passed is False
                or (
                    a.qa_passed is None
                    and a.qa_score is not None
                    and a.qa_score < verdict.threshold
                )
            )
        )

        def record(s: RunState) -> Scene:
            sc = self._same_version(s, scene)
            if clip is not None:
                att = self._attempt(sc, clip.id)
                att.qa_score, att.qa_passed = verdict.score, verdict.passed
            return sc

        if verdict.passed:

            def approve(s: RunState) -> list[AgentEvent]:
                sc = record(s)
                events = self._book(s, calls, Stage.QA, sc)
                sc.approve(verdict)
                events.append(
                    _ev(
                        run_id,
                        Stage.QA,
                        sc,
                        EventType.SCENE_STATUS,
                        f"Scene {sc.order} approved by QA (score {verdict.score:.2f})",
                        score=verdict.score,
                    )
                )
                return events

            self._update(run_id, approve, "qa approved")
            return

        if rejections >= MAX_QA_REGENERATIONS:

            def reject_final(s: RunState) -> list[AgentEvent]:
                sc = record(s)
                events = self._book(s, calls, Stage.QA, sc)
                sc.fail(
                    self._failure(
                        s,
                        FailureKind.QUALITY_REJECTED,
                        Stage.QA,
                        f"QA rejected {rejections + 1} times",
                    )
                )
                events.append(
                    _ev(
                        run_id,
                        Stage.QA,
                        sc,
                        EventType.SCENE_STATUS,
                        level="error",
                        summary=f"Scene {sc.order} failed QA {rejections + 1}×; "
                        "marked failed, other scenes continue",
                    )
                )
                return events

            self._update(run_id, reject_final, "qa exhausted")
            return

        def note(s: RunState) -> list[AgentEvent]:
            sc = record(s)
            issues = "; ".join(f"{i.category}: {i.fix}" for i in verdict.issues) or "low score"
            return [
                *self._book(s, calls, Stage.QA, sc),
                _ev(
                    run_id,
                    Stage.QA,
                    sc,
                    EventType.RECOVERY,
                    level="warn",
                    summary=f"Scene {sc.order} rejected by QA "
                    f"({'; '.join(verdict.fail_reasons)}): {issues}",
                    strategy="qa_regenerate",
                    score=verdict.score,
                    fail_reasons=verdict.fail_reasons,
                ),
            ]

        if not self._update(run_id, note, "qa rejected"):
            return
        self._evict_rejected(scene, verdict)
        detail = "Rejected because: " + "; ".join(verdict.fail_reasons) + "\n"
        detail += "\n".join(
            f"- [{i.severity}] {i.category} (image {i.frame_index}): {i.observation} → fix: {i.fix}"
            for i in verdict.issues
        ) + (f"\nHint: {verdict.revised_prompt_hint}" if verdict.revised_prompt_hint else "")
        await self._revise(run_id, Stage.QA, scene_id, "qa_feedback", detail, verdict=verdict)

    # ------------------------------------------------------------------------ helpers
    def _evict_rejected(self, scene: Scene, verdict: QAVerdict) -> None:
        """Known-bad outputs are never served from cache (ADR-0015): forget the rejected clip,
        and the keyframe too when the look itself was rejected."""
        doomed = [scene.last_attempt(ScenePhase.CLIP)]
        if verdict.needs_new_keyframe:
            doomed += [
                a
                for a in scene.attempts
                if a.phase is ScenePhase.KEYFRAME
                and scene.keyframe is not None
                and a.artifact is not None
                and a.artifact.sha256 == scene.keyframe.sha256
            ]
        for att in doomed:
            if att is not None:
                self.storage.cache.evict(att.request_hash)

    def _prepare(self, state: RunState, scene: Scene, phase: ScenePhase) -> PreparedJob:
        assert state.brief is not None and state.bible is not None
        phase_attempts = sum(
            1 for a in scene.attempts if a.scene_version == scene.version and a.phase is phase
        )
        return self.gateway.prepare(
            scene,
            phase,
            aspect_ratio=state.brief.aspect_ratio,
            palette=tuple(state.bible.color_grade.palette),
            attempt_no=phase_attempts + 1,
        )

    def _update(self, run_id: str, mutate: Mutation, reason: str) -> bool:
        try:
            self.storage.runs.update(run_id, mutate, reason=reason)
        except StaleWriteError:
            return False
        return True

    def _book(
        self, s: RunState, calls: Sequence[LLMCallRecord], stage: Stage, sc: Scene
    ) -> list[AgentEvent]:
        events = []
        for c in calls:
            s.budget.record_actual("llm", c.cost_usd, ref=c.id, scene_id=sc.id)
            events.append(c.as_event(s.id, stage, sc.id))
        return events

    @staticmethod
    def _failure(s: RunState, kind: FailureKind, stage: Stage, debug: str) -> SceneFailure:
        return SceneFailure(
            kind=kind,
            reason_user=failure_message(kind, stage, s.language),
            reason_debug=debug[:4000],
        )

    def _load(self, run_id: str, scene_id: str) -> Scene:
        return self._find(self.storage.runs.load(run_id), scene_id)

    @staticmethod
    def _find(s: RunState, scene_id: str) -> Scene:
        assert s.storyboard is not None
        return s.storyboard.by_id(scene_id)

    def _same_version(self, s: RunState, scene: Scene) -> Scene:
        sc = self._find(s, scene.id)
        if sc.version != scene.version:
            raise StaleWriteError(f"scene {sc.order} changed (v{scene.version}→v{sc.version})")
        return sc

    @staticmethod
    def _attempt(sc: Scene, attempt_id: str) -> GenerationAttempt:
        return next(a for a in sc.attempts if a.id == attempt_id)

    def _scratch(self, run_id: str) -> Path:
        return self.storage.artifacts.scratch_dir(run_id)

"""The whole-ad generation stages of a multishot run (ADR-0023): estimate → (human gate) →
generate → review. Like the scene runner, this module commits state itself (atomic updates
with events); nodes cannot.

    ESTIMATE  photo → public url (provider upload) → the provider's own estimate for exactly the
              request → refused here if it exceeds the run's caps (no spend) → recorded. A $0
              cache hit, or mock media in auto mode, is approved by code; real money always waits
              for a human at AWAITING_GENERATION_APPROVAL.
    GENERATE  reserve exactly the approved estimate (atomic with "start") → the request id is
              persisted at submit → poll → download → settle → the video is cached and stored.
              An interrupted generation is polled again on resume, never paid twice.
    QA        the critic reviews the whole video once. Pass → approved; hard fail → flagged.
              Warn only: nothing regenerates automatically (a new generation is a full ad's
              cost, so it goes back through the estimate gate by a human command).

Failures never crash the run: a refused or failed generation is recorded on the generation and
the run ends partial with the reason; an account-level refusal pauses the run (resumable).
"""

from __future__ import annotations

import contextlib
import functools
import random
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from adsmith.agent.i18n import failure_message
from adsmith.agent.recovery.backoff import Sleep, retry_transient
from adsmith.agent.recovery.errors import AccountLockedError, AgentError
from adsmith.domain.ad import AdGeneration
from adsmith.domain.base import utcnow
from adsmith.domain.budget import BudgetRefusedError
from adsmith.domain.enums import FailureKind, ScenePhase, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.generation import (
    Artifact,
    GenerationAttempt,
    GenerationRequest,
    RemoteRequest,
)
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAVerdict
from adsmith.domain.run import RunState
from adsmith.domain.scene import SceneFailure
from adsmith.logging import get_logger
from adsmith.providers.media.base import (
    MediaLabel,
    ReferenceVideoJob,
    ReferenceVideoProvider,
    RemoteControl,
)
from adsmith.storage import StaleWriteError, Storage
from adsmith.storage.repo import Mutation

log = get_logger(__name__)
AD_SCENE_ID = "ad"  # GenerationAttempt.scene_id of the whole-ad generation


class AdCritic(Protocol):
    async def review_ad(self, state: RunState) -> tuple[QAVerdict, list[LLMCallRecord]]: ...


def _ev(
    run_id: str,
    stage: Stage,
    type_: EventType,
    summary: str,
    level: Literal["info", "warn", "error"] = "info",
    **payload: object,
) -> AgentEvent:
    return AgentEvent.model_validate(
        {"run_id": run_id, "stage": stage, "type": type_, "summary": summary[:500],
         "level": level, "payload": payload}
    )  # fmt: skip


@dataclass
class AdGenerationRunner:
    storage: Storage
    provider: ReferenceVideoProvider
    critic: AdCritic
    sleep: Sleep
    rng: random.Random

    async def run(self, run_id: str, stage: Stage) -> None:
        if stage is Stage.ESTIMATE:
            await self._estimate(run_id)
        elif stage is Stage.GENERATE:
            await self._generate(run_id)
        elif stage is Stage.QA:
            await self._review(run_id)

    # ------------------------------------------------------------------------ the request
    def _request(self, gen: AdGeneration, photo_sha: str) -> GenerationRequest:
        """Everything that determines the video: its hash is the cache key. The version is part
        of it, so a human-requested new generation is a new request (no seed in the schema)."""
        return GenerationRequest(
            provider=self.provider.name,
            model=self.provider.model,
            phase=ScenePhase.CLIP,
            prompt=gen.prompt.text,
            params={
                "duration": gen.duration_s, "resolution": gen.resolution,
                "aspect_ratio": gen.aspect_ratio, "generate_audio": gen.generate_audio,
                "version": gen.version, "kind": "whole_ad",
            },
            input_image_sha256=photo_sha,
        )  # fmt: skip

    async def _job(self, state: RunState, gen: AdGeneration, attempt_no: int) -> ReferenceVideoJob:
        assert state.product_photo is not None
        path = self.storage.artifacts.resolve(state.product_photo)
        url = await retry_transient(
            functools.partial(self.provider.reference_url, path), sleep=self.sleep, rng=self.rng
        )
        return ReferenceVideoJob(
            prompt=gen.prompt.text,
            image_urls=(url,),
            duration_s=gen.duration_s,
            resolution=gen.resolution,
            aspect_ratio=gen.aspect_ratio,
            generate_audio=gen.generate_audio,
            label=MediaLabel(
                scene_order=0, scene_version=gen.version, phase=ScenePhase.CLIP,
                attempt_no=attempt_no, title="whole ad",
            ),
            variant=gen.version - 1,
        )  # fmt: skip

    # ------------------------------------------------------------------------ ESTIMATE
    async def _estimate(self, run_id: str) -> None:
        state = self.storage.runs.load(run_id)
        gen = state.generation
        if gen is None or gen.status not in {"pending", "estimated"}:
            return
        if state.product_photo is None:
            why = "a multishot ad is made from a product photo, and this run has none"
            self._fail(run_id, Stage.ESTIMATE, FailureKind.PERMANENT, why)
            return
        key = self._request(gen, state.product_photo.sha256).cache_key()
        if self.storage.cache.get(key) is not None:
            self._record_estimate(run_id, gen.version, 0.0, cached=True)
            return
        try:
            job = await self._job(state, gen, 1)
            usd = await retry_transient(
                functools.partial(self.provider.estimate_usd, job), sleep=self.sleep, rng=self.rng
            )
        except AccountLockedError:
            raise
        except AgentError as exc:
            self._fail(run_id, Stage.ESTIMATE, exc.kind, f"estimate: {exc.message}")
            return
        b = state.budget
        caps = [("run budget", b.remaining_usd)]
        if b.media_remaining_usd is not None:
            caps.append(("media cap (prepaid wallet)", b.media_remaining_usd))
        for name, left in caps:
            if usd > left + 1e-9:
                self._fail(
                    run_id, Stage.ESTIMATE, FailureKind.BUDGET_EXCEEDED,
                    f"the estimate ${usd:.3f} exceeds the remaining ${left:.3f} of the {name}; "
                    "nothing was generated",
                )  # fmt: skip
                return
        self._record_estimate(run_id, gen.version, usd, cached=False)

    def _record_estimate(self, run_id: str, version: int, usd: float, *, cached: bool) -> None:
        def mutate(s: RunState) -> list[AgentEvent]:
            g = self._same(s, version)
            g.set_estimate(usd)
            what = (
                f"{g.duration_s}s {g.resolution} {g.aspect_ratio}, audio "
                f"{'on' if g.generate_audio else 'off'}, {self.provider.model}"
            )
            events = [
                _ev(
                    run_id, Stage.ESTIMATE, EventType.DECISION,
                    f"Estimate: ${usd:.3f} for one generation ({what})"
                    + (" — cache hit, $0" if cached else ""),
                    est_usd=usd, model=self.provider.model, cached=cached,
                )
            ]  # fmt: skip
            auto = s.config.ad_provider == "mock" and not s.config.interactive
            if cached or auto:
                g.approve("cache" if cached else "auto_mock")
                why = "a cache hit costs nothing" if cached else "mock media in auto mode"
                events.append(
                    _ev(run_id, Stage.ESTIMATE, EventType.DECISION,
                        f"Approved by code ({why}); no human gate needed.", approved_usd=usd)
                )  # fmt: skip
            else:
                events.append(
                    _ev(run_id, Stage.ESTIMATE, EventType.DECISION,
                        f"Waiting for a human to approve ${usd:.3f} before any money is spent.",
                        est_usd=usd)
                )  # fmt: skip
            return events

        self._update(run_id, mutate, "ad estimate")

    # ------------------------------------------------------------------------ GENERATE
    async def _generate(self, run_id: str) -> None:
        state = self.storage.runs.load(run_id)
        gen = state.generation
        if gen is None or gen.status != "estimated" or state.product_photo is None:
            return
        if not gen.approved or gen.approved_usd is None:
            self._fail(run_id, Stage.GENERATE, FailureKind.PERMANENT,
                       "refusing to generate: the estimate was not approved")  # fmt: skip
            return
        request = self._request(gen, state.product_photo.sha256)
        key = request.cache_key()
        hit = self.storage.cache.get(key)
        attempt_no = sum(1 for a in gen.attempts if a.scene_version == gen.version) + 1
        attempt = GenerationAttempt(
            scene_id=AD_SCENE_ID, scene_version=gen.version, phase=ScenePhase.CLIP,
            attempt_no=attempt_no, provider=request.provider, model=request.model,
            request_hash=key, prompt_used=request.prompt, params=request.params,
        )  # fmt: skip
        if hit is not None:
            self._succeed(run_id, gen.version, attempt, hit, 0.0, reserved=False, cached=True)
            return
        est = gen.approved_usd
        resume = next(
            (
                a.remote_request
                for a in reversed(gen.attempts)
                if a.remote_request is not None and a.request_hash == key
                and a.outcome == "failed" and (a.error_message or "").startswith("interrupted")
            ),
            None,
        )  # fmt: skip

        def start(s: RunState) -> list[AgentEvent]:
            g = self._same(s, gen.version)
            s.budget.reserve(attempt.id, "video", est, None)
            g.start(attempt)
            return [
                _ev(
                    run_id, Stage.GENERATE, EventType.ATTEMPT_STARTED,
                    f"Whole ad: generation attempt {attempt_no} on {self.provider.model} "
                    f"(reserved ${est:.3f})" + (" — resuming the submitted job" if resume else ""),
                    attempt_id=attempt.id, est_usd=est, phase="clip",
                )
            ]  # fmt: skip

        try:
            self.storage.runs.update(run_id, start, reason="ad generation start")
        except BudgetRefusedError as exc:
            self._fail(run_id, Stage.GENERATE, FailureKind.BUDGET_EXCEEDED, str(exc))
            return
        except StaleWriteError:
            return

        def on_submitted(req: RemoteRequest) -> None:
            def mutate(s: RunState) -> list[AgentEvent]:
                g = self._same(s, gen.version)
                next(a for a in g.attempts if a.id == attempt.id).remote_request = req
                return []

            with contextlib.suppress(StaleWriteError):
                self.storage.runs.update(run_id, mutate, reason="ad request submitted")

        try:
            job = await self._job(state, gen, attempt_no)
            job = replace(job, est_usd=est, remote=RemoteControl(resume, on_submitted))
            out = await retry_transient(
                functools.partial(
                    self.provider.reference_to_video,
                    job,
                    self.storage.artifacts.scratch_dir(run_id),
                ),
                sleep=self.sleep,
                rng=self.rng,
                on_retry=lambda err, n, delay: self._retry_event(run_id, err, n, delay),
            )
        except AccountLockedError as exc:
            self._paused(run_id, gen.version, attempt.id, exc)
            raise
        except AgentError as exc:
            self._attempt_failed(run_id, gen.version, attempt.id, exc)
            return
        art = self.storage.artifacts.ingest(
            out.path, kind="video", provider=self.provider.name, width=out.width,
            height=out.height, duration_s=out.duration_s, remote_url=out.remote_url, move=True,
        )  # fmt: skip
        self.storage.cache.put(key, art)
        self._succeed(run_id, gen.version, attempt, art, out.cost_usd, reserved=True, cached=False)

    def _succeed(
        self,
        run_id: str,
        version: int,
        attempt: GenerationAttempt,
        artifact: Artifact,
        cost: float,
        *,
        reserved: bool,
        cached: bool,
    ) -> None:
        def mutate(s: RunState) -> list[AgentEvent]:
            g = self._same(s, version)
            if not reserved:
                g.attempts.append(attempt)
            att = next(a for a in g.attempts if a.id == attempt.id)
            att.outcome = "cache_hit" if cached else "success"
            att.finished_at, att.artifact, att.cost_usd = utcnow(), artifact, cost
            if reserved:
                s.budget.settle(attempt.id, cost)
            g.complete(artifact)
            dur = f"{artifact.duration_s:.2f}s" if artifact.duration_s else "?"
            return [
                _ev(
                    run_id, Stage.GENERATE,
                    EventType.CACHE_HIT if cached else EventType.ATTEMPT_FINISHED,
                    f"Whole ad ready ({dur}, ${cost:.3f})" + (": cache hit, $0" if cached else ""),
                    attempt_id=attempt.id, cost_usd=cost, phase="clip",
                )
            ]  # fmt: skip

        self._update(run_id, mutate, "ad generated")

    def _attempt_failed(self, run_id: str, version: int, attempt_id: str, exc: AgentError) -> None:
        """A failed generation is not retried with new money: the reason is recorded (failed and
        nsfw are not charged at Higgsfield) and the run ends partial; a human may renew it."""

        def mutate(s: RunState) -> list[AgentEvent]:
            g = self._same(s, version)
            att = next(a for a in g.attempts if a.id == attempt_id)
            att.outcome, att.finished_at = "failed", utcnow()
            att.failure_kind, att.error_message = exc.kind, exc.message[:2000]
            with contextlib.suppress(KeyError):
                s.budget.release(attempt_id)
            g.fail(self._failure(s, exc.kind, Stage.GENERATE, exc.message))
            return [
                _ev(
                    run_id, Stage.GENERATE, EventType.SCENE_STATUS, level="error",
                    summary=f"Whole ad generation failed: {exc.kind} ({exc.message[:300]}). "
                    "Nothing is regenerated automatically.",
                    kind=exc.kind.value,
                )
            ]  # fmt: skip

        self._update(run_id, mutate, "ad generation failed")

    def _paused(self, run_id: str, version: int, attempt_id: str, exc: AccountLockedError) -> None:
        def mutate(s: RunState) -> list[AgentEvent]:
            g = self._same(s, version)
            att = next(a for a in g.attempts if a.id == attempt_id)
            att.outcome, att.finished_at = "failed", utcnow()
            att.failure_kind, att.error_message = exc.kind, exc.message[:2000]
            with contextlib.suppress(KeyError):
                s.budget.release(attempt_id)
            g.release()
            return [
                _ev(
                    run_id, Stage.GENERATE, EventType.RECOVERY, level="error",
                    summary=f"The provider account refused work ({exc.message[:200]}); pausing "
                    "the run (resumable after a top-up)",
                    strategy="pause_run",
                )
            ]  # fmt: skip

        with contextlib.suppress(StaleWriteError):
            self.storage.runs.update(run_id, mutate, reason="ad generation paused")

    def _retry_event(self, run_id: str, err: AgentError, n: int, delay: float) -> None:
        self.storage.events.append(
            run_id,
            [
                _ev(
                    run_id, Stage.GENERATE, EventType.RETRY_SCHEDULED, level="warn",
                    summary=f"Whole ad: {err.kind} ({err.message[:200]}); retry {n} in "
                    f"{delay:.2f}s (the submitted job is polled, not resubmitted)",
                    kind=err.kind.value, delay_s=round(delay, 3),
                )
            ],
        )  # fmt: skip

    # ------------------------------------------------------------------------ QA
    async def _review(self, run_id: str) -> None:
        state = self.storage.runs.load(run_id)
        gen = state.generation
        if gen is None or gen.status != "qa_review":
            return
        try:
            verdict, calls = await self.critic.review_ad(state)
        except AgentError as exc:
            self._fail(run_id, Stage.QA, exc.kind, f"ad critic: {exc.message}", exc.llm_calls)
            return

        def mutate(s: RunState) -> list[AgentEvent]:
            g = self._same(s, gen.version)
            events = []
            for c in calls:
                s.budget.record_actual("llm", c.cost_usd, ref=c.id)
                events.append(c.as_event(s.id, Stage.QA))
            g.review(verdict)
            if verdict.passed:
                summary = f"Whole ad passed the hard gate (score {verdict.score:.2f})"
                level: Literal["info", "warn", "error"] = "info"
            else:
                summary = (
                    f"Whole ad FAILED the hard gate ({'; '.join(verdict.fail_reasons)}). Warn "
                    "only: it is assembled anyway; decide with `adsmith accept-ad` or "
                    "`adsmith regenerate-ad` (back to the estimate gate)."
                )
                level = "warn"
            events.append(
                _ev(
                    run_id, Stage.QA, EventType.SCENE_STATUS, summary, level=level,
                    gate="ad", score=verdict.score, passed=verdict.passed,
                    fail_reasons=list(verdict.fail_reasons),
                    issues=[f"[{i.severity}] {i.category}: {i.observation}"
                            for i in verdict.issues],
                )
            )  # fmt: skip
            return events

        self._update(run_id, mutate, "ad reviewed")

    # ------------------------------------------------------------------------ helpers
    def _fail(
        self,
        run_id: str,
        stage: Stage,
        kind: FailureKind,
        debug: str,
        calls: list[LLMCallRecord] | None = None,
    ) -> None:
        def mutate(s: RunState) -> list[AgentEvent]:
            events = []
            for c in calls or []:
                s.budget.record_actual("llm", c.cost_usd, ref=c.id)
                events.append(c.as_event(s.id, stage))
            g = s.generation
            if g is None or g.status in {"failed", "approved", "flagged"}:
                return events
            for att in g.attempts:  # release any reservation still open
                if att.outcome is None:
                    att.outcome, att.finished_at = "failed", utcnow()
                    att.failure_kind, att.error_message = kind, debug[:2000]
                    with contextlib.suppress(KeyError):
                        s.budget.release(att.id)
            g.fail(self._failure(s, kind, stage, debug))
            events.append(
                _ev(run_id, stage, EventType.SCENE_STATUS, level="error",
                    summary=f"Whole ad: {kind} ({debug[:300]})", kind=kind.value)
            )  # fmt: skip
            return events

        with contextlib.suppress(StaleWriteError):
            self.storage.runs.update(run_id, mutate, reason=f"ad failed ({kind})")

    @staticmethod
    def _failure(s: RunState, kind: FailureKind, stage: Stage, debug: str) -> SceneFailure:
        return SceneFailure(
            kind=kind,
            reason_user=failure_message(kind, stage, s.language),
            reason_debug=debug[:4000],
        )

    @staticmethod
    def _same(s: RunState, version: int) -> AdGeneration:
        g = s.generation
        if g is None or g.version != version:
            raise StaleWriteError("the ad generation changed while it was being worked on")
        return g

    def _update(self, run_id: str, mutate: Mutation, reason: str) -> bool:
        try:
            self.storage.runs.update(run_id, mutate, reason=reason)
        except StaleWriteError:
            return False
        return True

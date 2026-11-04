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
import functools
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from adsmith.agent.context import NodeContext, render_window
from adsmith.agent.i18n import failure_message
from adsmith.agent.media import GatewayResult, MediaGateway, PreparedJob
from adsmith.agent.nodes.rewrite import ReasonKind, rewrite_prompt
from adsmith.agent.recovery.backoff import Sleep, retry_transient
from adsmith.agent.recovery.errors import AccountLockedError, AgentError, ContentPolicyError
from adsmith.domain.base import utcnow
from adsmith.domain.budget import BudgetRefusedError, CostCategory
from adsmith.domain.enums import FailureKind, RecoveryAction, ScenePhase, SceneStatus, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.generation import Artifact, GenerationAttempt, RemoteRequest
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAVerdict
from adsmith.domain.run import ProductReference, RunState
from adsmith.domain.scene import Scene, SceneFailure, ScenePrompt
from adsmith.logging import get_logger
from adsmith.providers.media.base import RemoteControl, VideoJob
from adsmith.storage import StaleWriteError, Storage
from adsmith.storage.repo import Mutation

log = get_logger(__name__)

MAX_POLICY_REWRITES = 2


class Critic(Protocol):
    async def review(
        self, state: RunState, scene: Scene
    ) -> tuple[QAVerdict, list[LLMCallRecord]]: ...

    async def review_keyframe(
        self, state: RunState, scene: Scene
    ) -> tuple[QAVerdict, list[LLMCallRecord]]: ...

    async def review_product_reference(
        self, state: RunState, image: Artifact, prompt: str
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


_Review = tuple[GenerationAttempt, GatewayResult, QAVerdict | None, list[LLMCallRecord], str]


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
        if targets and stage in {Stage.KEYFRAME, Stage.GENERATE}:
            await self._product_reference(run_id, stage)
        sem = asyncio.Semaphore(self.concurrency)

        async def guarded(scene_id: str) -> None:
            async with sem:
                await self._scene(run_id, scene_id, stage)

        results = await asyncio.gather(*(guarded(sid) for sid in targets), return_exceptions=True)
        locked = next((r for r in results if isinstance(r, AccountLockedError)), None)
        for scene_id, outcome in zip(targets, results, strict=True):
            if isinstance(outcome, AccountLockedError):
                continue  # the scene was put back to pending; the run pauses below
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
        if locked is not None:
            # Account-level refusal: pause the whole run (orchestrator → errored, resumable).
            raise locked

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
        # Keyframe-first with a gate (ADR-0003): keyframe → keyframe QA → (retry keyframe | clip).
        # Only an approved keyframe is ever animated, so a bad still never costs a clip.
        while True:
            scene = self._load(run_id, scene_id)
            if scene.status is not SceneStatus.PENDING or scene.prompt is None:
                return
            if scene.phase is ScenePhase.KEYFRAME:
                if not await self._phase(run_id, scene_id, ScenePhase.KEYFRAME, stage):
                    return
                continue
            if scene.keyframe_approved:
                break
            outcome = await self._keyframe_gate(run_id, scene_id, stage)
            if outcome == "stop":
                return
            if outcome == "approved":
                break
        if stage is Stage.GENERATE:
            n = self.storage.runs.load(run_id).config.clip_candidates
            if n > 1:
                await self._best_of_n(run_id, scene_id, stage, n)
            else:
                await self._phase(run_id, scene_id, ScenePhase.CLIP, stage)

    # ------------------------------------------------------------------------ product reference
    async def _product_reference(self, run_id: str, stage: Stage) -> None:
        """ADR-0003 addendum 3: before any product keyframe, make ONE packshot of the bible's
        product and pass it through the gate. It then conditions every keyframe that shows the
        product (reference model), so the product is identical everywhere. If no still passes in
        ``keyframe_max_tries``, product keyframes fall back to plain text-to-image (logged)."""
        state = self.storage.runs.load(run_id)
        if not state.config.product_reference or state.bible is None or state.brief is None:
            return
        assert state.storyboard is not None
        if not any(sc.shows_product for sc in state.storyboard.scenes):
            return
        bible_v, max_tries = state.bible.version, self.node_ctx.settings.keyframe_max_tries
        current = state.product_reference
        same = current is not None and current.bible_version == bible_v
        if same and current is not None and (current.approved or current.tries >= max_tries):
            return
        product = state.bible.product
        base = self.node_ctx.prompts.get("product_reference").render(
            name=product.name, form=product.form, materials=product.materials,
            colors=product.colors, features=product.distinctive_features,
        ).user  # fmt: skip
        tries = current.tries if same and current is not None else 0
        prompt = current.prompt if tries and current is not None else base
        while tries < max_tries:
            tries += 1
            outcome = await self._product_reference_try(
                run_id, stage, prompt, tries, bible_v, max_tries, state.brief.aspect_ratio
            )
            if outcome == "stop" or (isinstance(outcome, QAVerdict) and outcome.passed):
                return
            if isinstance(outcome, QAVerdict) and outcome.revised_prompt_hint:
                prompt = f"{base} {outcome.revised_prompt_hint}"
        self._ref_event(
            run_id, stage,
            f"No product reference passed in {max_tries} tries: product keyframes fall back to "
            "plain text-to-image",
        )  # fmt: skip

    async def _product_reference_try(
        self,
        run_id: str,
        stage: Stage,
        prompt: str,
        n: int,
        bible_v: int,
        max_tries: int,
        aspect: str,
    ) -> QAVerdict | Literal["failed", "stop"] | None:
        prepared = self.gateway.prepare_product_reference(prompt, aspect, n)
        ref = f"product_ref_v{bible_v}_t{n}"

        def reserve(s: RunState) -> list[AgentEvent]:
            s.budget.reserve(ref, "image", prepared.est_usd, None)
            pr = s.product_reference
            if pr is None or pr.bible_version != bible_v:
                pr = ProductReference(bible_version=bible_v, prompt=prompt)
            s.product_reference = pr.model_copy(update={"tries": n, "prompt": prompt})
            return [
                _ev(
                    run_id, stage, None, EventType.ATTEMPT_STARTED,
                    f"Product reference: generating the locked packshot (try {n}/{max_tries}, "
                    f"${prepared.est_usd:.3f})",
                    est_usd=prepared.est_usd,
                )
            ]  # fmt: skip

        def release(s: RunState) -> list[AgentEvent]:
            s.budget.release(ref)
            return []

        try:
            self.storage.runs.update(run_id, reserve, reason="product reference")
        except BudgetRefusedError as exc:
            self._ref_event(run_id, stage, f"Product reference refused by the budget: {exc}")
            return "stop"
        try:
            res = await retry_transient(
                functools.partial(self.gateway.run, prepared, self._scratch(run_id)),
                sleep=self.sleep, rng=self.rng,
            )  # fmt: skip
        except AccountLockedError:
            self.storage.runs.update(run_id, release, reason="product reference paused")
            raise
        except AgentError as exc:
            self.storage.runs.update(run_id, release, reason="product reference failed")
            self._ref_event(run_id, stage, f"Product reference try {n} failed: {exc.message}")
            return "failed"
        state = self.storage.runs.load(run_id)
        verdict: QAVerdict | None
        try:
            verdict, calls = await self.critic.review_product_reference(state, res.artifact, prompt)
        except AgentError as exc:
            verdict, calls = None, exc.llm_calls

        def record(s: RunState) -> list[AgentEvent]:
            s.budget.settle(ref, res.cost_usd)
            events = []
            for c in calls:
                s.budget.record_actual("llm", c.cost_usd, ref=c.id, scene_id=None)
                events.append(c.as_event(s.id, stage, None))
            assert s.product_reference is not None and s.storyboard is not None
            pr = s.product_reference
            cost = round(pr.cost_usd + res.cost_usd, 6)
            if verdict is not None and verdict.passed:
                s.product_reference = pr.model_copy(
                    update={"artifact": res.artifact, "verdict": verdict, "cost_usd": cost}
                )
                shows = [sc.order for sc in s.storyboard.scenes if sc.shows_product]
                msg = (
                    f"Product reference approved by the gate (score {verdict.score:.2f}); it "
                    f"now conditions the product keyframes of scenes {shows}"
                )
                events.append(
                    _ev(run_id, stage, None, EventType.DECISION, msg, score=verdict.score)
                )
                return events
            s.product_reference = pr.model_copy(
                update={
                    "rejected": [*pr.rejected, res.artifact],
                    "verdict": verdict,
                    "cost_usd": cost,
                }
            )
            why = "; ".join(verdict.fail_reasons) if verdict else "critic error"
            events.append(
                _ev(
                    run_id, stage, None, EventType.RECOVERY, level="warn",
                    summary=f"Product reference try {n} rejected by the gate ({why})",
                    issues=[i.observation for i in verdict.issues] if verdict else [],
                )
            )  # fmt: skip
            return events

        self.storage.runs.update(run_id, record, reason="product reference reviewed")
        if verdict is None or not verdict.passed:
            self.storage.cache.evict(prepared.request.cache_key())
        return verdict

    def _ref_event(self, run_id: str, stage: Stage, summary: str) -> None:
        ev = _ev(run_id, stage, None, EventType.RECOVERY, summary, level="error")
        self.storage.runs.update(run_id, lambda s: [ev], reason="product reference event")

    # ------------------------------------------------------------------------ best-of-N clips
    async def _best_of_n(self, run_id: str, scene_id: str, stage: Stage, n: int) -> None:  # noqa: PLR0915
        """Hero mode (ADR-0003 addendum 2): N clips from the same approved keyframe, reserved up
        front and generated in parallel; the critic scores each and the best candidate with no hard
        failure is approved. The others stay in the CAS as recorded candidates. If none passes,
        the scene fails (no extra rescue)."""
        state = self.storage.runs.load(run_id)
        scene = self._find(state, scene_id)
        if scene.status is not SceneStatus.PENDING or scene.phase is not ScenePhase.CLIP:
            return
        base = self._prepare(state, scene, ScenePhase.CLIP)
        first_no = scene.attempts_for_current_version() + 1
        cands: list[tuple[GenerationAttempt, PreparedJob]] = []
        for k in range(n):
            req = base.request.model_copy(
                update={"params": {**base.request.params, "candidate": k}}
            )
            att = GenerationAttempt(
                scene_id=scene.id, scene_version=scene.version, phase=ScenePhase.CLIP,
                attempt_no=first_no + k, provider=req.provider, model=req.model,
                request_hash=req.cache_key(), prompt_used=req.prompt, params=req.params,
            )  # fmt: skip
            assert isinstance(base.job, VideoJob)
            label = replace(base.job.label, attempt_no=first_no + k)
            job = replace(base.job, variant=k, label=label)
            prepared = replace(base, request=req, job=job)
            cands.append((att, self._with_remote(run_id, scene, att, prepared)))

        def reserve(s: RunState) -> list[AgentEvent]:
            sc = self._same_version(s, scene)
            for att, p in cands:
                s.budget.reserve(att.id, "video", p.est_usd, sc.id)
            sc.start()
            sc.attempts.extend(att for att, _ in cands)
            total = sum(p.est_usd for _, p in cands)
            return [
                _ev(
                    run_id, stage, sc, EventType.ATTEMPT_STARTED,
                    f"Scene {sc.order}: best-of-{n} clips from the approved keyframe "
                    f"(reserved ${total:.3f} up front)",
                    candidates=[a.id for a, _ in cands], est_usd=total,
                )
            ]  # fmt: skip

        try:
            self.storage.runs.update(run_id, reserve, reason=f"scene {scene.order} best-of-{n}")
        except BudgetRefusedError as exc:
            self._fail_scene(run_id, stage, scene_id, FailureKind.BUDGET_EXCEEDED, str(exc))
            return
        except StaleWriteError:
            return

        async def one(
            att: GenerationAttempt, p: PreparedJob
        ) -> tuple[GenerationAttempt, GatewayResult | None, AgentError | None]:
            try:
                res = await retry_transient(
                    functools.partial(self.gateway.run, p, self._scratch(run_id)),
                    sleep=self.sleep, rng=self.rng,
                )  # fmt: skip
            except AccountLockedError:
                raise
            except AgentError as exc:
                return att, None, exc
            return att, res, None

        try:
            results = await asyncio.gather(*(one(a, p) for a, p in cands))
        except AccountLockedError as exc:
            for att, _ in cands:
                self._attempt_paused(run_id, stage, scene, att, exc)
            raise

        def record(s: RunState) -> list[AgentEvent]:
            sc = self._same_version(s, scene)
            for att, res, err in results:
                a = self._attempt(sc, att.id)
                a.finished_at = utcnow()
                if res is not None:
                    a.outcome = "cache_hit" if res.cache_hit else "success"
                    a.artifact, a.cost_usd = res.artifact, res.cost_usd
                    a.injected_fault = res.injected_fault
                    s.budget.settle(att.id, res.cost_usd)
                else:
                    assert err is not None
                    a.outcome, a.failure_kind, a.error_message = (
                        "failed",
                        err.kind,
                        err.message[:2000],
                    )
                    s.budget.release(att.id)
            return []

        if not self._update(run_id, record, f"scene {scene.order} candidates ready"):
            return
        made = [(att, res) for att, res, _ in results if res is not None]
        if not made:
            err = next(e for _, _, e in results if e is not None)
            self._fail_scene(
                run_id, stage, scene_id, err.kind, f"all {n} candidates failed: {err.message}"
            )
            return

        state = self.storage.runs.load(run_id)
        current = self._find(state, scene_id)
        reviews: list[_Review] = []
        for att, res in made:
            subject = current.model_copy(update={"clip": res.artifact})
            try:
                verdict, calls = await self.critic.review(state, subject)
                reviews.append((att, res, verdict, calls, ""))
            except AgentError as exc:
                reviews.append((att, res, None, exc.llm_calls, exc.message))
        passing = [r for r in reviews if r[2] is not None and r[2].passed]
        winner = max(passing, key=lambda r: r[2].score if r[2] else 0.0) if passing else None

        def decide(s: RunState) -> list[AgentEvent]:
            sc = self._same_version(s, scene)
            events: list[AgentEvent] = []
            lines = []
            for k, (att, _res, v, calls, err) in enumerate(reviews, start=1):
                events += self._book(s, calls, Stage.QA, sc)
                a = self._attempt(sc, att.id)
                if v is not None:
                    a.qa_score, a.qa_passed = v.score, v.passed
                    why = "pass" if v.passed else "; ".join(v.fail_reasons)
                    lines.append(f"candidate {k}: {v.score:.2f} ({why})")
                else:
                    lines.append(f"candidate {k}: critic error ({err[:80]})")
            payload = [
                {
                    "attempt_id": att.id,
                    "candidate": k,
                    "score": v.score if v else None,
                    "passed": bool(v and v.passed),
                    "fail_reasons": v.fail_reasons if v else [err],
                    "clip": res.artifact.path,
                }
                for k, (att, res, v, _c, err) in enumerate(reviews, start=1)
            ]
            if winner is not None and winner[2] is not None:
                k = reviews.index(winner) + 1
                sc.complete_clip(winner[1].artifact)
                sc.approve(winner[2])
                events.append(
                    _ev(
                        run_id, Stage.QA, sc, EventType.SCENE_STATUS,
                        f"Scene {sc.order} best-of-{n}: " + " · ".join(lines)
                        + f" → candidate {k} wins (score {winner[2].score:.2f})",
                        candidates=payload, winner=k, score=winner[2].score,
                    )
                )  # fmt: skip
            else:
                sc.fail(
                    self._failure(
                        s, FailureKind.QUALITY_REJECTED, Stage.QA,
                        f"none of {n} candidates passed QA: " + " · ".join(lines),
                    )
                )  # fmt: skip
                events.append(
                    _ev(
                        run_id, Stage.QA, sc, EventType.SCENE_STATUS, level="error",
                        summary=f"Scene {sc.order} best-of-{n}: " + " · ".join(lines)
                        + " → no candidate passed; scene failed (no extra rescue)",
                        candidates=payload,
                    )
                )  # fmt: skip
            return events

        self._update(run_id, decide, f"scene {scene.order} best-of-{n} decided")

    # ------------------------------------------------------------------------ keyframe gate
    async def _keyframe_gate(
        self, run_id: str, scene_id: str, stage: Stage
    ) -> Literal["approved", "retry", "stop"]:
        state = self.storage.runs.load(run_id)
        scene = self._find(state, scene_id)
        assert scene.keyframe is not None
        try:
            verdict, calls = await self.critic.review_keyframe(state, scene)
        except AgentError as exc:
            self._fail_scene(
                run_id, stage, scene_id, exc.kind, f"keyframe critic: {exc.message}", exc.llm_calls
            )
            return "stop"
        kf = scene.keyframe
        kf_att = next(
            (
                a
                for a in reversed(scene.attempts)
                if a.phase is ScenePhase.KEYFRAME and a.artifact and a.artifact.sha256 == kf.sha256
            ),
            None,
        )
        rejected = sum(
            1
            for a in scene.attempts
            if a.scene_version == scene.version
            and a.phase is ScenePhase.KEYFRAME
            and a.qa_passed is False
        )
        max_tries = self.node_ctx.settings.keyframe_max_tries
        render_s, _ = render_window(self.node_ctx.settings, state, scene.duration_s)
        clip_usd = round(self.gateway.video.usd_per_second * render_s, 4)

        def record(s: RunState) -> Scene:
            sc = self._same_version(s, scene)
            if kf_att is not None:
                att = self._attempt(sc, kf_att.id)
                att.qa_score, att.qa_passed = verdict.score, verdict.passed
            return sc

        if verdict.passed:

            def approve(s: RunState) -> list[AgentEvent]:
                sc = record(s)
                events = self._book(s, calls, stage, sc)
                sc.approve_keyframe(verdict)
                events.append(
                    _ev(
                        run_id,
                        stage,
                        sc,
                        EventType.SCENE_STATUS,
                        f"Scene {sc.order} keyframe approved by the gate "
                        f"(score {verdict.score:.2f}); video may start",
                        gate="keyframe",
                        score=verdict.score,
                    )
                )
                return events

            return "approved" if self._update(run_id, approve, "keyframe approved") else "stop"

        reasons = "; ".join(verdict.fail_reasons)
        issues = "; ".join(f"{i.category}: {i.observation}" for i in verdict.issues)
        if rejected + 1 >= max_tries:

            def give_up(s: RunState) -> list[AgentEvent]:
                sc = record(s)
                events = self._book(s, calls, stage, sc)
                sc.keyframe_qa = verdict
                sc.fail(
                    self._failure(
                        s,
                        FailureKind.QUALITY_REJECTED,
                        stage,
                        f"keyframe rejected {rejected + 1}× by the gate ({reasons})",
                    )
                )
                events.append(
                    _ev(
                        run_id,
                        stage,
                        sc,
                        EventType.SCENE_STATUS,
                        level="error",
                        summary=f"Scene {sc.order} keyframe rejected {rejected + 1}×; scene "
                        f"failed without spending on video (saved ~${clip_usd:.2f}). "
                        "Other scenes continue.",
                        gate="keyframe",
                        saved_usd=clip_usd,
                    )
                )
                return events

            self._update(run_id, give_up, "keyframe gate exhausted")
            self._evict_keyframe(scene)
            return "stop"

        def reject(s: RunState) -> list[AgentEvent]:
            sc = record(s)
            events = self._book(s, calls, stage, sc)
            sc.reject_keyframe(verdict)
            events.append(
                _ev(
                    run_id,
                    stage,
                    sc,
                    EventType.RECOVERY,
                    level="warn",
                    summary=f"Scene {sc.order} keyframe rejected by the gate ({reasons}): "
                    f"{issues[:300]}. No video spent (saved ~${clip_usd:.2f}); regenerating "
                    f"the keyframe ({rejected + 2}/{max_tries})",
                    strategy="keyframe_regenerate",
                    gate="keyframe",
                    score=verdict.score,
                    saved_usd=clip_usd,
                    fail_reasons=verdict.fail_reasons,
                )
            )
            return events

        if not self._update(run_id, reject, "keyframe rejected"):
            return "stop"
        self._evict_keyframe(scene)
        detail = "Keyframe rejected because: " + reasons + "\n"
        detail += "\n".join(
            f"- [{i.severity}] {i.category}: {i.observation} → fix: {i.fix}" for i in verdict.issues
        ) + (f"\nHint: {verdict.revised_prompt_hint}" if verdict.revised_prompt_hint else "")
        if not await self._revise(run_id, stage, scene_id, "keyframe_feedback", detail):
            return "stop"
        return "retry"

    def _evict_keyframe(self, scene: Scene) -> None:
        for a in scene.attempts:
            if (
                a.phase is ScenePhase.KEYFRAME
                and a.artifact is not None
                and scene.keyframe is not None
                and a.artifact.sha256 == scene.keyframe.sha256
            ):
                self.storage.cache.evict(a.request_hash)

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
            prepared = self._with_remote(run_id, scene, attempt, prepared)
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
            except AccountLockedError as exc:
                self._attempt_paused(run_id, stage, scene, attempt, exc)
                raise
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

    def _with_remote(
        self, run_id: str, scene: Scene, attempt: GenerationAttempt, prepared: PreparedJob
    ) -> PreparedJob:
        """Crash safety for paid remote jobs: resume the request an interrupted attempt of this
        exact request left behind, and persist any new submission atomically before polling."""
        resume = next(
            (
                a.remote_request
                for a in reversed(scene.attempts)
                if a.remote_request is not None
                and a.request_hash == attempt.request_hash
                and a.outcome == "failed"
                and (a.error_message or "").startswith("interrupted")
            ),
            None,
        )

        def on_submitted(req: RemoteRequest) -> None:
            def mutate(s: RunState) -> list[AgentEvent]:
                sc = self._same_version(s, scene)
                self._attempt(sc, attempt.id).remote_request = req
                return []

            with contextlib.suppress(StaleWriteError):
                self.storage.runs.update(run_id, mutate, reason="remote request submitted")

        ctl = RemoteControl(resume=resume, on_submitted=on_submitted)
        return replace(prepared, job=replace(prepared.job, remote=ctl))

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

    def _attempt_paused(
        self,
        run_id: str,
        stage: Stage,
        scene: Scene,
        attempt: GenerationAttempt,
        exc: AccountLockedError,
    ) -> None:
        """Account-level refusal: close the attempt, release its money, and put the scene back
        to pending (not failed), so resuming the run after a top-up continues it."""

        def mutate(s: RunState) -> list[AgentEvent]:
            sc = self._same_version(s, scene)
            att = self._attempt(sc, attempt.id)
            att.outcome, att.finished_at = "failed", utcnow()
            att.failure_kind, att.error_message = exc.kind, exc.message[:2000]
            s.budget.release(attempt.id)
            sc.release_lease()
            return [
                _ev(
                    run_id,
                    stage,
                    sc,
                    EventType.RECOVERY,
                    level="error",
                    summary=f"Scene {sc.order}: the provider account refused work "
                    f"({exc.message[:200]}); pausing the run (resumable after a top-up)",
                    strategy="pause_run",
                )
            ]

        with contextlib.suppress(StaleWriteError):
            self.storage.runs.update(run_id, mutate, reason=f"scene {scene.order} paused")

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
            label, strategy = {
                "content_policy": ("Policy rewrite", "policy_rewrite"),
                "qa_feedback": ("QA regeneration", "qa_regenerate"),
                "keyframe_feedback": ("Keyframe gate revision", "keyframe_regenerate"),
            }[reason]
            events.append(
                _ev(
                    run_id,
                    stage,
                    sc,
                    EventType.RECOVERY,
                    f"{label} for scene {sc.order}: prompt revised ({kept})",
                    strategy=strategy,
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

        if rejections >= self.node_ctx.settings.qa_max_regenerations:

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
        # Interrupted attempts (a crashed process) do not count: the resumed attempt must build the
        # same request (same keyframe seed, same hash) to find and poll the job it left behind.
        phase_attempts = sum(
            1
            for a in scene.attempts
            if a.scene_version == scene.version
            and a.phase is phase
            and not (a.error_message or "").startswith("interrupted")
        )
        ref = state.product_reference
        locked = (
            ref.artifact
            if ref is not None and ref.approved and ref.bible_version == state.bible.version
            else None
        )
        return self.gateway.prepare(
            scene,
            phase,
            aspect_ratio=state.brief.aspect_ratio,
            palette=tuple(state.bible.color_grade.palette),
            attempt_no=phase_attempts + 1,
            product_reference=locked,
            product_name=state.bible.product.name,
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

"""The orchestrator: a deterministic run loop over the transition table (ADR-0001).

    step(run):  load snapshot → run the node for the current stage (LLM reasoning happens here)
                → one atomic commit: deltas + LLM cost booking + events + next_stage()

The LLM never chooses the next stage; ``next_stage`` is a pure function of the committed state.
A node failure never crashes the run: typed ``AgentError``s (and unexpected exceptions) move the
run to ``errored`` with a user-language reason, after booking the LLM calls already spent.
``resume`` re-enters at the failed stage, or the current one after a crash. Nodes are
idempotent with respect to committed state, so re-running a stage is always safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import traceback
from collections.abc import Sequence
from pathlib import Path

from adsmith.agent.context import AgentRuntime
from adsmith.agent.deltas import apply_delta
from adsmith.agent.export import write_production_pack
from adsmith.agent.i18n import failure_message, stage_label
from adsmith.agent.nodes.assemble import assemble
from adsmith.agent.nodes.base import Node, NodeResult
from adsmith.agent.nodes.continuity import continuity
from adsmith.agent.nodes.ideate import ideate
from adsmith.agent.nodes.prompt import prompt
from adsmith.agent.nodes.select import select
from adsmith.agent.nodes.storyboard import storyboard
from adsmith.agent.nodes.understand import understand
from adsmith.agent.recovery.errors import AgentError
from adsmith.agent.transitions import next_stage
from adsmith.config import Settings
from adsmith.domain.base import utcnow
from adsmith.domain.budget import BudgetLedger
from adsmith.domain.enums import AWAITING_STAGES, TERMINAL_STAGES, FailureKind, SceneStatus, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.run import RunConfig, RunError, RunState
from adsmith.domain.similarity import detect_language
from adsmith.logging import get_logger
from adsmith.providers.llm.scripted import FIXTURES_DIR
from adsmith.storage.repo import StaleWriteError

log = get_logger(__name__)

# Stages whose node exists. The run loop stops at a stage with no registered node.
NODES: dict[Stage, Node] = {
    Stage.UNDERSTAND: understand,
    Stage.IDEATE: ideate,
    Stage.SELECT: select,
    Stage.STORYBOARD: storyboard,
    Stage.CONTINUITY: continuity,
    Stage.PROMPT: prompt,
    Stage.ASSEMBLE: assemble,
}
# Per-scene stages: run by the SceneStageRunner, which commits each scene's progress itself.
SCENE_STAGES: frozenset[Stage] = frozenset({Stage.KEYFRAME, Stage.GENERATE, Stage.QA})

# Side effects that run after a stage's commit (files, not state). Failures are logged, not fatal.
EXPORT_AFTER: frozenset[Stage] = frozenset({Stage.PROMPT, Stage.ASSEMBLE})


def run_config_from_settings(settings: Settings, request: str, *, interactive: bool) -> RunConfig:
    scenario = settings.scripted_scenario
    if settings.llm_provider == "scripted" and scenario is None:
        guess = f"perfume_{detect_language(request)}"
        scenario = guess if (FIXTURES_DIR / guess).is_dir() else "perfume_en"
    return RunConfig(
        interactive=interactive,
        chaos=settings.chaos,
        seed=settings.chaos_config.seed,
        llm_provider=settings.llm_provider,
        media_provider=settings.media_provider,
        model_profile=settings.active_profile,
        qa_mode=settings.resolved_qa_mode(),
        qa_threshold=settings.qa_threshold,
        aspect_ratio=settings.aspect_ratio,
        target_duration_s=settings.target_duration_s,
        scripted_scenario=scenario,
        image_model=settings.fal_image_model or settings.catalog.fal.image.default,
        video_model=settings.fal_video_model or settings.catalog.fal.video.default,
        media_long_side=settings.media_long_side,
        endcard_s=settings.endcard_s,
        partial_mode=settings.partial_mode,
        clip_candidates=settings.clip_candidates,
        product_reference=settings.product_reference,
        music_path=str(settings.music_path.resolve()) if settings.music_path else None,
    )


def _event(
    run_id: str, stage: Stage, type_: EventType, summary: str, **payload: object
) -> AgentEvent:
    return AgentEvent.model_validate(
        {"run_id": run_id, "stage": stage, "type": type_, "summary": summary, "payload": payload}
    )


def _book_llm_calls(
    state: RunState, calls: Sequence[LLMCallRecord], stage: Stage
) -> list[AgentEvent]:
    events = []
    for call in calls:
        state.budget.record_actual("llm", call.cost_usd, ref=call.id)
        events.append(call.as_event(state.id, stage))
    return events


class Orchestrator:
    def __init__(self, runtime: AgentRuntime) -> None:
        self.rt = runtime
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------------ lifecycle
    def create_run(
        self,
        request: str,
        *,
        interactive: bool = True,
        chaos: bool | None = None,
        hero: bool = False,
        product_reference: bool | None = None,
    ) -> RunState:
        s = self.rt.settings
        config = run_config_from_settings(s, request, interactive=interactive)
        if chaos is not None:
            config = config.model_copy(update={"chaos": chaos})
        if product_reference is not None:  # locked product still (ADR-0003 addendum 3)
            config = config.model_copy(update={"product_reference": product_reference})
        if hero:  # best-of-2 clips from the same approved keyframe (ADR-0003 addendum 2)
            config = config.model_copy(update={"clip_candidates": max(2, config.clip_candidates)})
        limit = s.budget_usd
        media_limit: float | None = None
        if config.media_provider == "fal":
            # Never let one run reserve more media than what is left of the prepaid wallet. LLM
            # calls are billed by another provider, so only image + video count against it.
            media_limit = min(limit, self.rt.fal_account().summary().remaining_usd)
            if media_limit <= 0:
                raise RuntimeError("the fal account balance is exhausted (see the account ledger)")
        state = RunState(
            config=config,
            request=request,
            budget=BudgetLedger(
                limit_usd=limit,
                media_limit_usd=media_limit,
                max_attempts_per_scene=s.max_attempts_per_scene,
                max_total_attempts=s.max_total_attempts,
            ),
        )
        created = _event(
            state.id,
            Stage.UNDERSTAND,
            EventType.DECISION,
            f"Run created ({'interactive' if interactive else 'auto'}, "
            f"{state.config.llm_provider}/{state.config.model_profile}, "
            f"budget ${limit:.2f}"
            + (f", media cap ${media_limit:.2f}" if media_limit is not None else "")
            + ").",
            config=state.config.model_dump(mode="json"),
        )
        return self.rt.storage.runs.create(state, events=[created]).state

    async def run_until_blocked(self, run_id: str, *, stop_before: Stage | None = None) -> RunState:
        """Advance until a human gate, a terminal stage, ``stop_before``, or an unbuilt stage."""
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:  # one loop per run per process
            state = self.rt.storage.runs.load(run_id)
            while (
                state.stage not in AWAITING_STAGES
                and state.stage not in TERMINAL_STAGES
                and state.stage is not stop_before
                and (state.stage in NODES or state.stage in SCENE_STAGES)
            ):
                state = await self.step(state.id)
            return state

    async def resume(self, run_id: str, *, stop_before: Stage | None = None) -> RunState:
        """Re-enter a run after a failure or crash. Safe to call at any time."""

        def prepare(s: RunState) -> list[AgentEvent]:
            events = []
            if s.stage is Stage.ERRORED and s.error is not None:
                events.append(
                    _event(
                        s.id,
                        s.error.stage,
                        EventType.DECISION,
                        f"Resuming at {s.error.stage} after: {s.error.reason_debug[:200]}",
                    )
                )
                s.stage, s.error = s.error.stage, None
            released = []
            for scene in s.storyboard.scenes if s.storyboard else []:
                if scene.status is SceneStatus.GENERATING:  # lease from a crashed process
                    # Close the interrupted attempt and free its budget reservation; finished
                    # generations are in the cache, so the retry costs nothing for them.
                    for att in scene.attempts:
                        if att.outcome is None:
                            att.outcome, att.finished_at = "failed", utcnow()
                            att.failure_kind = FailureKind.TRANSIENT
                            att.error_message = "interrupted (process stopped); resumed"
                            with contextlib.suppress(KeyError):
                                s.budget.release(att.id)
                    scene.release_lease()
                    released.append(scene.order)
            if released:
                events.append(
                    _event(
                        s.id,
                        s.stage,
                        EventType.DECISION,
                        f"Released in-flight scenes {released} after interruption; "
                        "finished generations will be cache hits.",
                    )
                )
            return events

        self.rt.storage.runs.update(run_id, prepare, reason="resume")
        return await self.run_until_blocked(run_id, stop_before=stop_before)

    # ------------------------------------------------------------------------ one step
    async def step(self, run_id: str) -> RunState:
        state = self.rt.storage.runs.load(run_id)
        stage = state.stage
        if stage in SCENE_STAGES:
            return await self._scene_step(state)
        node = NODES[stage]
        lang = state.language
        self.rt.storage.events.append(
            run_id,
            [
                _event(
                    run_id, stage, EventType.STAGE_STARTED, f"Started: {stage_label(stage, 'en')}"
                )
            ],
        )
        started = time.monotonic()
        try:
            result = await node(state, self.rt.node_context(state))
        except AgentError as exc:
            return self._fail(
                run_id,
                stage=stage,
                kind=exc.kind,
                debug=exc.message,
                calls=exc.llm_calls,
                lang=lang,
                started=started,
            )
        except Exception as exc:
            log.exception("node crashed", run_id=run_id, stage=stage.value)
            debug = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
            return self._fail(
                run_id,
                stage=stage,
                kind=FailureKind.PERMANENT,
                debug=debug,
                calls=[],
                lang=lang,
                started=started,
            )
        try:
            committed = self._commit(run_id, stage, result, started)
        except StaleWriteError as exc:
            # Someone (a user command, another process) moved the run while the node ran. The
            # result is discarded on purpose, not misapplied; the run continues from its new state.
            self.rt.storage.events.append(
                run_id,
                [
                    AgentEvent(
                        run_id=run_id,
                        stage=stage,
                        type=EventType.DECISION,
                        level="warn",
                        summary=f"Discarded the {stage} result: {exc}",
                        payload={"discarded_llm_calls": len(result.llm_calls)},
                    )
                ],
            )
            return self.rt.storage.runs.load(run_id)
        except ValueError as exc:  # deltas would produce an invalid state: rejected atomically
            log.exception("commit rejected", run_id=run_id, stage=stage.value)
            return self._fail(
                run_id,
                stage=stage,
                kind=FailureKind.PERMANENT,
                debug=f"commit rejected: {exc}",
                calls=result.llm_calls,
                lang=lang,
                started=started,
            )
        if stage in EXPORT_AFTER:
            self.export(committed)
        return committed

    def export(self, state: RunState, out_dir: Path | None = None) -> Path | None:
        """Write the Production Pack. Never fails the run: problems become a warn event."""
        target = out_dir or self.rt.storage.artifacts.run_dir(state.id) / "production_pack"
        try:
            path = write_production_pack(state, target)
        except OSError as exc:
            self.rt.storage.events.append(
                state.id,
                [_event(state.id, state.stage, EventType.EXPORT, f"Production Pack failed: {exc}")],
            )
            return None
        self.rt.storage.events.append(
            state.id,
            [
                _event(
                    state.id,
                    state.stage,
                    EventType.EXPORT,
                    f"Production Pack written to {path}",
                    path=str(path),
                )
            ],
        )
        return path

    async def _scene_step(self, state: RunState) -> RunState:
        run_id, stage = state.id, state.stage
        self.rt.storage.events.append(
            run_id,
            [
                _event(
                    run_id, stage, EventType.STAGE_STARTED, f"Started: {stage_label(stage, 'en')}"
                )
            ],
        )
        started = time.monotonic()
        spent_before = state.budget.spent_usd
        try:
            await self.rt.scene_runner(state).run(run_id, stage)
        except Exception as exc:
            log.exception("scene stage stopped", run_id=run_id, stage=stage.value)
            return self._fail(
                run_id,
                stage=stage,
                kind=exc.kind if isinstance(exc, AgentError) else FailureKind.PERMANENT,
                debug=f"{type(exc).__name__}: {exc}",
                calls=[],
                lang=state.language,
                started=started,
            )
        spent = self.rt.storage.runs.load(run_id).budget.spent_usd - spent_before
        return self._commit(run_id, stage, NodeResult(), started, stage_cost=spent)

    def _commit(
        self,
        run_id: str,
        stage: Stage,
        result: NodeResult,
        started: float,
        *,
        stage_cost: float | None = None,
    ) -> RunState:
        duration_ms = int((time.monotonic() - started) * 1000)

        llm_cost = sum(c.cost_usd for c in result.llm_calls)
        # Scene stages book media + rewrite costs per scene; report the stage's whole spend.
        cost = llm_cost if stage_cost is None else stage_cost

        def mutate(s: RunState) -> list[AgentEvent]:
            if s.stage is not stage:
                raise StaleWriteError(f"run moved from {stage} to {s.stage} while the node ran")
            for delta in result.deltas:
                apply_delta(s, delta)
            events = _book_llm_calls(s, result.llm_calls, stage) + list(result.events)
            s.stage = next_stage(s, stage)
            events.append(
                _event(
                    run_id,
                    stage,
                    EventType.STAGE_COMPLETED,
                    f"Completed {stage_label(stage, 'en')} in {duration_ms / 1000:.1f}s "
                    f"(${cost:.4f}) → {s.stage}",
                    duration_ms=duration_ms,
                    cost_usd=round(cost, 6),
                    llm_calls=len(result.llm_calls),
                    repairs=sum(1 for c in result.llm_calls if c.kind == "repair"),
                    next_stage=s.stage.value,
                )
            )
            if s.stage in AWAITING_STAGES:
                events.append(
                    _event(
                        run_id,
                        s.stage,
                        EventType.AWAITING_INPUT,
                        f"Waiting for the user: {s.stage}",
                    )
                )
            if s.stage in TERMINAL_STAGES and s.storyboard is not None:
                statuses = [sc.status.value for sc in s.storyboard.scenes]
                events.append(
                    _event(
                        run_id,
                        s.stage,
                        EventType.RUN_COMPLETED,
                        f"Run {s.stage}: {statuses.count('approved')} approved, "
                        f"{statuses.count('failed')} failed; spent ${s.budget.spent_usd:.3f}",
                        statuses=statuses,
                        spent_usd=s.budget.spent_usd,
                    )
                )
            return events

        return self.rt.storage.runs.update(run_id, mutate, reason=f"{stage} completed").state

    def _fail(
        self,
        run_id: str,
        *,
        stage: Stage,
        kind: FailureKind,
        debug: str,
        calls: Sequence[LLMCallRecord],
        lang: str,
        started: float,
    ) -> RunState:
        duration_ms = int((time.monotonic() - started) * 1000)

        def mutate(s: RunState) -> list[AgentEvent]:
            events = _book_llm_calls(s, calls, stage)
            s.error = RunError(
                stage=stage,
                kind=kind,
                reason_user=failure_message(kind, stage, lang),
                reason_debug=debug[:4000],
            )
            s.stage = Stage.ERRORED
            events.append(
                AgentEvent(
                    run_id=run_id,
                    stage=stage,
                    type=EventType.ERROR,
                    level="error",
                    summary=f"{stage} failed ({kind}): {debug[:300]}",
                    payload={"kind": kind.value, "duration_ms": duration_ms, "resumable": True},
                )
            )
            return events

        return self.rt.storage.runs.update(run_id, mutate, reason=f"{stage} failed").state

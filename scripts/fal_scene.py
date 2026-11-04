"""Generate ONE scene of an existing planned run on real fal media (keyframe + clip), through the
real scene pipeline (budget reservation, wallet ledger, recovery, cache), and optionally review
it with the vision critic.

    uv run python scripts/fal_scene.py --source-data <dir> --run <run_id> --scene 4 \
        --video ltx_23_fast [--qa]

The source run is copied into the project data dir as a new run (the source is never modified),
with media switched to fal and the requested models frozen into its RunConfig.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from adsmith.agent import commands
from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
from adsmith.agent.scenes import Critic
from adsmith.domain.base import new_id
from adsmith.domain.budget import BudgetLedger
from adsmith.domain.enums import SceneStatus, Stage
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAVerdict
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene
from adsmith.storage import Storage


def copy_run(
    rt: AgentRuntime, source: RunState, *, video: str, image: str, long_side: int
) -> RunState:
    remaining = rt.fal_account().summary().remaining_usd
    config = source.config.model_copy(
        update={
            "media_provider": "fal",
            "qa_mode": "llm",
            "video_model": video,
            "image_model": image,
            "media_long_side": long_side,
        }
    )
    budget = BudgetLedger(
        limit_usd=min(rt.settings.budget_usd, remaining),
        max_attempts_per_scene=source.budget.max_attempts_per_scene,
        max_total_attempts=source.budget.max_total_attempts,
    )
    state = source.model_copy(
        update={
            "id": new_id("run"),
            "config": config,
            "budget": budget,
            "stage": Stage.KEYFRAME,
            "state_version": 0,
        }
    )
    state = RunState.model_validate(state.model_dump())
    return rt.storage.runs.create(state, events=[]).state


class RecordingCritic:
    """Prints every verdict in full (the scene keeps only the latest one)."""

    def __init__(self, inner: Critic, rt: AgentRuntime) -> None:
        self.inner, self.rt = inner, rt

    async def review(self, state: RunState, scene: Scene) -> tuple[QAVerdict, list[LLMCallRecord]]:
        t = time.monotonic()
        verdict, calls = await self.inner.review(state, scene)
        cost = sum(c.cost_usd for c in calls)
        clip = scene.clip
        print(f"\n=== critic verdict ({time.monotonic() - t:.1f}s, ${cost:.4f} Anthropic) on")
        print(f"    clip     {self.rt.storage.artifacts.resolve(clip) if clip else None}")
        kf = scene.keyframe
        print(f"    keyframe {self.rt.storage.artifacts.resolve(kf) if kf else None}")
        print(
            f"    passed={verdict.passed} score={verdict.score} threshold={verdict.threshold} "
            f"needs_new_keyframe={verdict.needs_new_keyframe}"
        )
        print(f"    checks: {verdict.checks.model_dump()}")
        for i in verdict.issues:
            print(f"    [{i.severity}] {i.category} (image {i.frame_index}): {i.observation}")
            print(f"        fix: {i.fix}")
        print(f"    hint: {verdict.revised_prompt_hint or '-'}")
        return verdict, calls


def show_prompt(title: str, scene: Scene) -> None:
    assert scene.prompt is not None
    print(f"\n--- {title} ({scene.prompt.template_version})")
    print(f"KEYFRAME: {scene.prompt.keyframe_prompt}")
    print(f"MOTION:   {scene.prompt.motion_prompt}")
    print(f"NEGATIVE: {scene.prompt.negative_prompt}")


def show_wallet(rt: AgentRuntime, since: int) -> int:
    """Print every account-ledger row added since ``since`` with the running remaining."""
    rows = rt.fal_account().entries()
    s = rt.fal_account().summary()
    for r in rows[since:]:
        print(
            f"    $ fal {r['state']}: {r['endpoint']} [{r['ref']}] "
            f"${float(str(r['actual_usd'] or r['est_usd'])):.3f}"
        )
    if len(rows) > since:
        print(f"    $ wallet remaining (ledger estimate): ${s.remaining_usd:.3f}")
    return len(rows)


async def main() -> None:  # noqa: PLR0912, PLR0915 - linear driver script
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-data", type=Path)
    ap.add_argument("--run", required=True)
    ap.add_argument("--scene", type=int, required=True)
    ap.add_argument("--video", default="ltx_23_fast")
    ap.add_argument("--image", default="flux_dev")
    ap.add_argument("--long-side", type=int, default=1280)
    ap.add_argument("--loop", action="store_true", help="generate/QA until approved or failed")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--reprompt", action="store_true", help="re-write the scene prompt first")
    ap.add_argument("--edit-json", type=Path, help="scene spec edit: {reason, description, shot}")
    ap.add_argument("--stop-on-reject", action="store_true", help="no regeneration after a reject")
    args = ap.parse_args()

    from adsmith.config import Settings  # noqa: PLC0415

    rt = AgentRuntime.from_settings(Settings())
    if args.source_data:
        source = Storage.open(args.source_data).runs.load(args.run)
        state = copy_run(rt, source, video=args.video, image=args.image, long_side=args.long_side)
        print(f"copied {args.run} → {state.id} (budget ${state.budget.limit_usd:.2f})")
    else:
        state = rt.storage.runs.load(args.run)
    assert state.storyboard is not None
    scene_id = next(s for s in state.storyboard.scenes if s.order == args.scene).id
    if args.reprompt or args.edit_json:
        edit = json.loads(args.edit_json.read_text()) if args.edit_json else {}
        commands.edit_scene_spec(
            rt.storage,
            state.id,
            args.scene,
            reason=edit.get("reason", "re-prompt with the current templates"),
            description=edit.get("description"),
            shot=edit.get("shot"),
        )
        t = time.monotonic()
        state = await Orchestrator(rt).step(state.id)  # the prompt node rewrites this scene only
        print(f"prompt node: {time.monotonic() - t:.1f}s → stage {state.stage}")
        assert state.storyboard is not None
        show_prompt("new prompt", state.storyboard.by_id(scene_id))
    runner = rt.scene_runner(state)
    runner.critic = RecordingCritic(runner.critic, rt)
    mark = len(rt.fal_account().entries())
    print(f"wallet before: ${rt.fal_account().summary().remaining_usd:.3f} remaining")

    def load() -> Scene:
        st = rt.storage.runs.load(state.id)
        assert st.storyboard is not None
        return st.storyboard.by_id(scene_id)

    for step in range(args.max_steps):
        scene = load()
        if scene.status is SceneStatus.QA_REVIEW:
            prompt_before = scene.prompt
            await runner._scene(state.id, scene_id, Stage.QA)
            after = load()
            if args.stop_on_reject and after.status is not SceneStatus.APPROVED:
                print("    stop-on-reject: not regenerating (rewrite shown below for reference)")
                if after.prompt != prompt_before:
                    show_prompt("rewrite the loop WOULD use next", after)
                mark = show_wallet(rt, mark)
                break
            if after.prompt != prompt_before:
                show_prompt("revised prompt after QA rejection", after)
                kept = after.keyframe is not None
                print(f"    keyframe {'KEPT' if kept else 'DISCARDED (will be regenerated)'}")
        elif scene.status is SceneStatus.PENDING:
            t = time.monotonic()
            await runner._scene(state.id, scene_id, Stage.GENERATE)
            print(f"\n>>> generate step {step} took {time.monotonic() - t:.1f}s")
        else:
            break
        mark = show_wallet(rt, mark)
        if not args.loop:
            break

    scene = load()
    final = rt.storage.runs.load(state.id)
    print(f"\nrun {state.id}  scene {scene.order}  status {scene.status}")
    for a in scene.attempts:
        path = rt.storage.artifacts.resolve(a.artifact) if a.artifact else None
        took = (a.finished_at - a.started_at).total_seconds() if a.finished_at else 0.0
        print(
            f"  {a.phase:<8} attempt {a.attempt_no}: {a.outcome} ${a.cost_usd:.3f} {took:.1f}s "
            f"{a.model} qa={a.qa_score}\n      {path}"
            + (f"\n      error: {a.error_message}" if a.error_message else "")
        )
    if scene.failure is not None:
        print(f"  failure: {scene.failure.kind}: {scene.failure.reason_debug}")
    llm = sum(e.actual_usd or 0 for e in final.budget.entries if e.category == "llm")
    s = rt.fal_account().summary()
    print(f"\nLLM (Anthropic, not fal) booked in this run: ${llm:.4f}")
    print(
        f"fal wallet (ledger estimate): balance ${s.balance_usd:.2f}, spent ${s.settled_usd:.3f}, "
        f"reserved ${s.reserved_usd:.3f}, remaining ${s.remaining_usd:.3f}"
    )


if __name__ == "__main__":
    asyncio.run(main())

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
import time
from pathlib import Path

from adsmith.agent.context import AgentRuntime
from adsmith.domain.base import new_id
from adsmith.domain.budget import BudgetLedger
from adsmith.domain.enums import SceneStatus, Stage
from adsmith.domain.run import RunState
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


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-data", type=Path)
    ap.add_argument("--run", required=True)
    ap.add_argument("--scene", type=int, required=True)
    ap.add_argument("--video", default="ltx_23_fast")
    ap.add_argument("--image", default="flux_dev")
    ap.add_argument("--long-side", type=int, default=1280)
    ap.add_argument("--qa", action="store_true", help="also run the vision critic")
    args = ap.parse_args()

    from adsmith.config import Settings  # noqa: PLC0415

    settings = Settings()
    rt = AgentRuntime.from_settings(settings)
    if args.source_data:
        source = Storage.open(args.source_data).runs.load(args.run)
        state = copy_run(rt, source, video=args.video, image=args.image, long_side=args.long_side)
        print(f"copied {args.run} → {state.id} (budget ${state.budget.limit_usd:.2f})")
    else:
        state = rt.storage.runs.load(args.run)
    assert state.storyboard is not None
    scene = next(s for s in state.storyboard.scenes if s.order == args.scene)
    before = rt.fal_account().summary()
    runner = rt.scene_runner(state)
    t0 = time.monotonic()
    await runner._scene(state.id, scene.id, Stage.GENERATE)
    t_gen = time.monotonic() - t0
    state = rt.storage.runs.load(state.id)
    assert state.storyboard is not None
    scene = state.storyboard.by_id(scene.id)
    if args.qa and scene.status is SceneStatus.QA_REVIEW:
        t1 = time.monotonic()
        await runner._scene(state.id, scene.id, Stage.QA)
        print(f"QA took {time.monotonic() - t1:.1f}s")
        state = rt.storage.runs.load(state.id)
        assert state.storyboard is not None
        scene = state.storyboard.by_id(scene.id)

    after = rt.fal_account().summary()
    print(f"\nrun {state.id}  scene {scene.order}  status {scene.status}  ({t_gen:.1f}s generate)")
    for a in scene.attempts:
        path = rt.storage.artifacts.resolve(a.artifact) if a.artifact else None
        took = (a.finished_at - a.started_at).total_seconds() if a.finished_at else None
        print(
            f"  {a.phase:<8} attempt {a.attempt_no}: {a.outcome} ${a.cost_usd:.3f} "
            f"{f'{took:.1f}s' if took else ''} {a.model}\n      {path}"
            + (f"\n      error: {a.error_message}" if a.error_message else "")
            + (f"\n      qa_score: {a.qa_score}" if a.qa_score is not None else "")
        )
    if scene.qa is not None:
        print(f"  verdict: passed={scene.qa.passed} score={scene.qa.score}")
        for i in scene.qa.issues:
            print(f"    [{i.severity}] {i.category} (img {i.frame_index}): {i.observation}")
            print(f"        fix: {i.fix}")
    if scene.failure is not None:
        print(f"  failure: {scene.failure.kind}: {scene.failure.reason_debug}")
    media = sum(a.cost_usd for a in scene.attempts)
    llm = sum(e.actual_usd or 0 for e in state.budget.entries if e.category == "llm")
    print(
        f"\nfal spend this run: ${after.settled_usd - before.settled_usd:.3f} "
        f"(scene media ${media:.3f}); LLM (Anthropic, not fal) booked in this run ${llm:.4f}"
    )
    print(
        f"fal wallet (ledger estimate): balance ${after.balance_usd:.2f}, "
        f"spent ${after.settled_usd:.3f}, reserved ${after.reserved_usd:.3f}, "
        f"remaining ${after.remaining_usd:.3f}"
    )


if __name__ == "__main__":
    asyncio.run(main())

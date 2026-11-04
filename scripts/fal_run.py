"""Full real ad from an existing planned run: copy → scene edits / re-prompts → the normal
orchestrated pipeline (prompt → keyframe + gate → generate → qa → assemble) on fal media.

    ADSMITH_BUDGET_USD=4.85 uv run python scripts/fal_run.py --source-data <dir> --run <planned> \
        --video kling_v3_pro --image flux_dev --edit 4:s4.json --reprompt-all
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
from adsmith.config import Settings
from adsmith.domain.base import new_id
from adsmith.domain.budget import BudgetLedger
from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from adsmith.domain.run import RunState
from adsmith.storage import Storage


def report(rt: AgentRuntime, run_id: str, mark: int) -> None:  # noqa: PLR0912
    st = rt.storage.runs.load(run_id)
    assert st.storyboard is not None
    print(f"\n######## run {st.id}  stage {st.stage}")
    if st.final is not None:
        print(f"FINAL VIDEO: {rt.storage.artifacts.run_dir(st.id) / 'final.mp4'}")
        print(f"   (CAS: {rt.storage.artifacts.resolve(st.final)}, {st.final.duration_s}s)")
    for sc in st.storyboard.scenes:
        print(f"\n--- scene {sc.order} v{sc.version}: {sc.status}  ({sc.duration_s}s)")
        if sc.prompt:
            print(f"  motion: {sc.prompt.motion_prompt}")
        for a in sc.attempts:
            if a.scene_version != sc.version:
                continue
            path = rt.storage.artifacts.resolve(a.artifact) if a.artifact else None
            gate = {True: "PASS", False: "FAIL", None: "-"}[a.qa_passed]
            took = (a.finished_at - a.started_at).total_seconds() if a.finished_at else 0.0
            print(
                f"  {a.phase:<8} a{a.attempt_no} {a.outcome} ${a.cost_usd:.3f} {took:.0f}s "
                f"qa={gate}({a.qa_score}) seed={a.params.get('seed', '-')}\n      {path}"
                + (f"\n      error: {a.error_message[:300]}" if a.error_message else "")
            )
        for label, v in (("keyframe gate", sc.keyframe_qa), ("clip QA", sc.qa)):
            if v is None:
                continue
            print(f"  {label}: passed={v.passed} score={v.score} reasons={v.fail_reasons}")
            for i in v.issues:
                print(f"    [{i.severity}] {i.category} (img {i.frame_index}): {i.observation}")
        if sc.failure:
            print(f"  FAILURE: {sc.failure.kind}: {sc.failure.reason_debug[:400]}")
    print("\n--- recoveries / retries / gate events")
    for e in rt.storage.events.tail(st.id, limit=100_000):
        if e.type in {EventType.RECOVERY, EventType.RETRY_SCHEDULED} or (
            e.type is EventType.SCENE_STATUS and e.level != "info"
        ):
            print(f"  [{e.stage}] {e.summary[:400]}")
    print("\n--- fal ledger (paid calls this run, running balance)")
    acct = rt.fal_account()
    balance = acct.balance_usd
    spent_before = sum(
        float(str(r["actual_usd"])) for r in acct.entries()[:mark] if r["state"] == "settled"
    )
    running = balance - spent_before
    for r in acct.entries()[mark:]:
        usd = float(str(r["actual_usd"] or r["est_usd"]))
        if r["state"] == "settled":
            running -= usd
        print(f"  {r['state']:<8} {r['endpoint']:<44} [{r['ref']}] ${usd:.3f} → ${running:.3f}")
    s = acct.summary()
    by = st.budget.by_category
    print(
        f"\nrun cost: image ${by.get('image', 0):.3f}, video ${by.get('video', 0):.3f}, "
        f"LLM ${by.get('llm', 0):.3f} (Anthropic) · run budget ${st.budget.limit_usd:.2f}"
    )
    print(
        f"fal wallet (ledger estimate): spent ${s.settled_usd:.3f}, "
        f"remaining ${s.remaining_usd:.3f}"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-data", type=Path, required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--video", default="kling_v3_pro")
    ap.add_argument("--image", default="flux_dev")
    ap.add_argument("--long-side", type=int, default=1280)
    ap.add_argument("--edit", action="append", default=[], help="ORDER:path.json scene edit")
    ap.add_argument("--reprompt-all", action="store_true")
    args = ap.parse_args()

    rt = AgentRuntime.from_settings(Settings())
    source = Storage.open(args.source_data).runs.load(args.run)
    remaining = rt.fal_account().summary().remaining_usd
    config = source.config.model_copy(
        update={
            "media_provider": "fal",
            "qa_mode": "llm",
            "video_model": args.video,
            "image_model": args.image,
            "media_long_side": args.long_side,
            "interactive": False,
        }
    )
    budget = BudgetLedger(
        limit_usd=min(rt.settings.budget_usd, remaining),
        max_attempts_per_scene=rt.settings.max_attempts_per_scene,
        max_total_attempts=rt.settings.max_total_attempts,
    )
    state = RunState.model_validate(
        source.model_copy(
            update={
                "id": new_id("run"),
                "config": config,
                "budget": budget,
                "stage": Stage.KEYFRAME,
                "state_version": 0,
            }
        ).model_dump()
    )
    state = rt.storage.runs.create(state, events=[]).state
    print(f"run {state.id}: budget ${budget.limit_usd:.2f}, wallet ${remaining:.3f}", flush=True)
    edited: set[int] = set()
    for spec in args.edit:
        order, path = spec.split(":", 1)
        edit = json.loads(Path(path).read_text())  # noqa: ASYNC240 - tiny local file
        commands.edit_scene_spec(
            rt.storage,
            state.id,
            int(order),
            reason=edit["reason"],
            description=edit.get("description"),
            shot=edit.get("shot"),
        )
        edited.add(int(order))
    if args.reprompt_all:
        assert state.storyboard is not None
        for sc in state.storyboard.scenes:
            if sc.order not in edited:
                commands.edit_scene_spec(
                    rt.storage, state.id, sc.order, reason="re-prompt with current templates"
                )
    mark = len(rt.fal_account().entries())
    t = time.monotonic()
    final = await Orchestrator(rt).run_until_blocked(state.id)
    print(f"pipeline finished in {time.monotonic() - t:.0f}s at stage {final.stage}", flush=True)
    report(rt, state.id, mark)


if __name__ == "__main__":
    asyncio.run(main())

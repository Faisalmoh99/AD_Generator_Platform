"""A/B the keyframe model through the real keyframe gate (no video spend).

One copy of the planned run gets a scene edit/re-prompt (ONE prompt for both models), then it is
forked once per image model and the scene runs keyframe → gate (→ revise → keyframe …) only.

    ADSMITH_KEYFRAME_MAX_TRIES=2 uv run python scripts/keyframe_ab.py --source-data <dir> \
        --run <planned_run> --scene 4 --edit-json s4.json --models flux_dev flux_2_flex
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
from adsmith.domain.enums import ScenePhase, Stage
from adsmith.domain.events import AgentEvent
from adsmith.domain.run import RunState
from adsmith.storage import Storage


async def main() -> None:  # noqa: PLR0915 - linear experiment driver
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-data", type=Path, required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--scene", type=int, required=True)
    ap.add_argument("--edit-json", type=Path, required=True)
    ap.add_argument("--video", default="kling_v3_pro")
    ap.add_argument("--models", nargs="+", default=["flux_dev", "flux_2_flex"])
    ap.add_argument("--long-side", type=int, default=1280)
    args = ap.parse_args()

    rt = AgentRuntime.from_settings(Settings())
    source = Storage.open(args.source_data).runs.load(args.run)
    remaining = rt.fal_account().summary().remaining_usd
    config = source.config.model_copy(
        update={
            "media_provider": "fal",
            "qa_mode": "llm",
            "video_model": args.video,
            "image_model": args.models[0],
            "media_long_side": args.long_side,
        }
    )
    budget = BudgetLedger(
        limit_usd=min(rt.settings.budget_usd, remaining),
        max_attempts_per_scene=rt.settings.max_attempts_per_scene,
        max_total_attempts=source.budget.max_total_attempts,
    )
    base = RunState.model_validate(
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
    base = rt.storage.runs.create(base, events=[]).state
    edit = json.loads(args.edit_json.read_text())
    commands.edit_scene_spec(
        rt.storage,
        base.id,
        args.scene,
        reason=edit["reason"],
        description=edit.get("description"),
        shot=edit.get("shot"),
    )
    base = await Orchestrator(rt).step(base.id)
    assert base.storyboard is not None
    scene_id = next(s for s in base.storyboard.scenes if s.order == args.scene).id
    prompt = base.storyboard.by_id(scene_id).prompt
    assert prompt is not None
    print(f"base run {base.id} @v{base.state_version} ({prompt.template_version})")
    print(f"KEYFRAME PROMPT: {prompt.keyframe_prompt}\n")

    for model in args.models:
        fork = rt.storage.runs.fork(base.id, base.state_version).state

        def set_model(s: RunState, m: str = model) -> list[AgentEvent]:
            s.config = s.config.model_copy(update={"image_model": m})
            return []

        rt.storage.runs.update(fork.id, set_model, reason=f"image model {model}")
        fork = rt.storage.runs.load(fork.id)
        mark = len(rt.fal_account().entries())
        t = time.monotonic()
        await rt.scene_runner(fork)._scene(fork.id, scene_id, Stage.KEYFRAME)
        took = time.monotonic() - t
        st = rt.storage.runs.load(fork.id)
        assert st.storyboard is not None
        sc = st.storyboard.by_id(scene_id)
        print(
            f"=== {model}  run {fork.id}  ({took:.1f}s)  status={sc.status} "
            f"approved={sc.keyframe_approved}"
        )
        for a in sc.attempts:
            if a.phase is not ScenePhase.KEYFRAME:
                continue
            path = rt.storage.artifacts.resolve(a.artifact) if a.artifact else None
            gate = {True: "PASS", False: "FAIL", None: "-"}[a.qa_passed]
            print(
                f"  keyframe attempt {a.attempt_no}: {a.outcome} ${a.cost_usd:.3f} "
                f"seed={a.params.get('seed')} gate={gate} score={a.qa_score}"
            )
            print(f"      {path}")
        for e in rt.storage.events.tail(fork.id, limit=10_000):
            if (
                e.payload.get("gate") == "keyframe"
                or e.payload.get("strategy") == "keyframe_regenerate"
            ):
                print(f"  event: {e.summary}")
        v = sc.keyframe_qa
        if v is not None:
            print(
                f"  last gate verdict: passed={v.passed} score={v.score} reasons={v.fail_reasons}"
            )
            for i in v.issues:
                print(f"    [{i.severity}] {i.category}: {i.observation}\n        fix: {i.fix}")
        for r in rt.fal_account().entries()[mark:]:
            print(
                f"  $ fal {r['state']} {r['endpoint']} "
                f"${float(str(r['actual_usd'] or r['est_usd'])):.3f}"
            )
        print(
            f"  $ wallet remaining (ledger estimate): "
            f"${rt.fal_account().summary().remaining_usd:.3f}\n"
        )


if __name__ == "__main__":
    asyncio.run(main())

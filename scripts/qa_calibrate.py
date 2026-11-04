"""Run critic template versions side by side on the SAME stored inputs (no media spend).

    uv run python scripts/qa_calibrate.py --run <run_id> --scene 4 --source-data <dir> \
        --source-run <planned_run_id> --versions 1 2

Inputs: every clip attempt of the scene (with the keyframe it was animated from). The prompt
judged with each clip is the one that produced it: the original planned prompt for the first
clip (from the source run), the current scene prompt for later clips.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from adsmith.agent.context import AgentRuntime
from adsmith.agent.qa import LLMCritic
from adsmith.config import Settings
from adsmith.domain.enums import ScenePhase
from adsmith.storage import Storage


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--scene", type=int, required=True)
    ap.add_argument("--source-data", type=Path, required=True)
    ap.add_argument("--source-run", required=True)
    ap.add_argument("--versions", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    rt = AgentRuntime.from_settings(Settings())
    state = rt.storage.runs.load(args.run)
    assert state.storyboard is not None
    scene = next(s for s in state.storyboard.scenes if s.order == args.scene)
    source = Storage.open(args.source_data).runs.load(args.source_run)
    assert source.storyboard is not None
    original_prompt = next(s for s in source.storyboard.scenes if s.order == args.scene).prompt
    keyframe = next(a.artifact for a in scene.attempts if a.phase is ScenePhase.KEYFRAME)
    clips = [a for a in scene.attempts if a.phase is ScenePhase.CLIP and a.artifact]

    results = []
    ctx = rt.node_context(state)
    for n, clip in enumerate(clips):
        prompt = original_prompt if n == 0 else scene.prompt
        subject = scene.model_copy(
            update={"clip": clip.artifact, "keyframe": keyframe, "prompt": prompt}
        )
        for v in args.versions:
            verdict, calls = await LLMCritic(ctx, template_version=v).review(state, subject)
            cost = sum(c.cost_usd for c in calls)
            print(f"\n=== clip attempt {clip.attempt_no} · qa_critic v{v} · ${cost:.4f}")
            print(
                f"    score={verdict.score} passed(new rule)={verdict.passed}\n"
                f"    reasons={verdict.fail_reasons}"
            )
            print(
                f"    needs_new_keyframe={verdict.needs_new_keyframe}\n"
                f"    checks={verdict.checks.model_dump()}"
            )
            for i in verdict.issues:
                print(f"    [{i.severity}] {i.category} (image {i.frame_index}): {i.observation}")
                print(f"        fix: {i.fix}")
            results.append(
                {
                    "clip_attempt": clip.attempt_no,
                    "version": v,
                    "cost_usd": cost,
                    "passed": verdict.passed,
                    "fail_reasons": verdict.fail_reasons,
                    "needs_new_keyframe": verdict.needs_new_keyframe,
                    "verdict": verdict.model_dump(mode="json"),
                }
            )
    if args.out:
        args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

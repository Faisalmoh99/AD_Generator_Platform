"""Re-judge stored keyframes with the current keyframe gate (no media spend).

uv run python scripts/gate_recalibrate.py --runs <run> <run> ... --sheet out.png --out out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from adsmith.agent.context import AgentRuntime
from adsmith.agent.qa import LLMCritic
from adsmith.config import Settings
from adsmith.domain.enums import ScenePhase
from adsmith.domain.scene import ScenePrompt


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--sheet", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rt = AgentRuntime.from_settings(Settings())
    rows: list[dict[str, object]] = []
    thumbs: list[tuple[int, Path]] = []
    for run_id in args.runs:
        state = rt.storage.runs.load(run_id)
        assert state.storyboard is not None
        critic = LLMCritic(rt.node_context(state))
        model = state.config.image_model
        for scene in state.storyboard.scenes:
            for a in scene.attempts:
                if a.phase is not ScenePhase.KEYFRAME or a.artifact is None or a.qa_passed is None:
                    continue
                prompt = ScenePrompt(
                    keyframe_prompt=a.prompt_used,
                    motion_prompt=(scene.prompt.motion_prompt if scene.prompt else "static"),
                    negative_prompt="",
                    template_version="recalibration",
                    bible_version=1,
                )
                subject = scene.model_copy(update={"keyframe": a.artifact, "prompt": prompt})
                verdict, calls = await critic.review_keyframe(state, subject)
                n = len(rows) + 1
                rows.append(
                    {
                        "n": n,
                        "run": run_id,
                        "model": model,
                        "scene": scene.order,
                        "attempt": a.attempt_no,
                        "path": str(rt.storage.artifacts.resolve(a.artifact)),
                        "old_passed": a.qa_passed,
                        "old_score": a.qa_score,
                        "new_passed": verdict.passed,
                        "new_score": verdict.score,
                        "new_reasons": verdict.fail_reasons,
                        "issues": [
                            f"[{i.severity}] {i.category}: {i.observation}" for i in verdict.issues
                        ],
                        "cost_usd": sum(c.cost_usd for c in calls),
                    }
                )
                print(
                    f"#{n:>2} {model} S{scene.order} a{a.attempt_no}: "
                    f"old {'PASS' if a.qa_passed else 'FAIL'} {a.qa_score} → "
                    f"new {'PASS' if verdict.passed else 'FAIL'} {verdict.score} "
                    f"{verdict.fail_reasons}",
                    flush=True,
                )
                for i in verdict.issues:
                    print(f"      [{i.severity}] {i.category}: {i.observation[:220]}")
                thumbs.append((n, rt.storage.artifacts.resolve(a.artifact)))
    args.out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    w, h, cols = 240, 427, 7
    sheet = Image.new("RGB", (w * cols, (h + 28) * ((len(thumbs) + cols - 1) // cols)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=22)
    for k, (n, path) in enumerate(thumbs):
        x, y = (k % cols) * w, (k // cols) * (h + 28)
        with Image.open(path) as im:
            sheet.paste(im.convert("RGB").resize((w, h)), (x, y + 28))
        draw.text((x + 6, y + 2), f"#{n}", fill="black", font=font)
    sheet.save(args.sheet)
    total = sum(float(str(r["cost_usd"])) for r in rows)
    print(f"\n{len(rows)} keyframes re-judged, ${total:.4f} Anthropic")


if __name__ == "__main__":
    asyncio.run(main())

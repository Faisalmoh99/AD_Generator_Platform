"""Calibrate the whole-ad critic's rubric on an ad made by the OLD pipeline (montage v2).

Calls Anthropic (one vision call, ~15 images). Run it only when the user has approved it.

Expected result for montage v2 (a well-lit sequence of product shots with no human truth, no
turn and no question in the opening): the HARD gate passes, and the CREATIVE score is LOW,
with insight, story_turn and hook weakest. If the creative score comes out high, the rubric is
broken: report it before tuning anything.

    ADSMITH_LLM_PROVIDER=anthropic uv run python scripts/calibrate_ad_critic.py
    ADSMITH_LLM_PROVIDER=anthropic uv run python scripts/calibrate_ad_critic.py \\
        --run run_01M6MVHD6CE5V22VM38GXNRN09 --video docs/media/montage_v2.mp4

Nothing is written to the run: the verdict is printed (and saved as JSON next to the video under
data/calibration/).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from adsmith.agent.context import AgentRuntime
from adsmith.agent.qa import LLMCritic, ShotWindow
from adsmith.config import Settings
from adsmith.domain.enums import SceneStatus, Stage
from adsmith.domain.events import EventType
from adsmith.domain.run import RunState

MONTAGE_V2 = "run_01M6MVHD6CE5V22VM38GXNRN09"


def shot_windows(rt: AgentRuntime, state: RunState) -> list[ShotWindow]:
    """The assembled shots on the final's timeline: the beat-cut durations when the assembly
    logged them, else each approved shot's kept length (the end card is not a shot)."""
    assert state.storyboard is not None
    shots = [s for s in state.storyboard.scenes if s.status is SceneStatus.APPROVED and s.clip]
    lengths = [s.kept_window[1] for s in shots]
    for e in reversed(rt.storage.events.tail(state.id, limit=100_000)):
        cut = e.payload.get("durations")
        if e.type is EventType.DECISION and e.stage is Stage.ASSEMBLE and isinstance(cut, list):
            if len(cut) == len(shots):
                lengths = [float(str(x)) for x in cut]
            break
    out, t = [], 0.0
    for n, (s, d) in enumerate(zip(shots, lengths, strict=True), start=1):
        plan = f"{s.vocab or s.purpose.value}: {s.shot.subject_action}"
        out.append(ShotWindow(n, round(t, 3), round(t + d, 3), plan))
        t += d
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=MONTAGE_V2)
    ap.add_argument("--video", default="docs/media/montage_v2.mp4")
    args = ap.parse_args()
    s = Settings()
    if s.llm_provider != "anthropic":
        raise SystemExit("set ADSMITH_LLM_PROVIDER=anthropic (this calls the vision critic)")
    rt = AgentRuntime.from_settings(s)
    state = rt.storage.runs.load(args.run)
    state = state.model_copy(
        update={"config": state.config.model_copy(update={"llm_provider": "anthropic"})}
    )
    video = Path(args.video)
    if not await asyncio.to_thread(video.is_file):
        raise SystemExit(f"no video at {video}")
    windows = shot_windows(rt, state)
    ref = state.product_reference.artifact if state.product_reference else None
    reference = rt.storage.artifacts.resolve(ref) if ref is not None else None
    print(f"Run {state.id}: {len(windows)} shots, footage {windows[-1].end_s:.2f}s; reference "
          f"{'the run packshot' if reference else 'none'}")  # fmt: skip
    t0 = time.monotonic()
    verdict, calls = await LLMCritic(rt.node_context(state)).review_video(
        state,
        video=video,
        shots=windows,
        reference=reference,
        prompt_text="(no single prompt: the old pipeline generated each shot as its own clip)",
        duration_s=windows[-1].end_s,
    )
    cost = sum(c.cost_usd for c in calls)
    print(f"\nHard gate: {'PASS' if verdict.passed else 'FAIL'} (score {verdict.score:.2f})"
          + ("" if verdict.passed else f": {'; '.join(verdict.fail_reasons)}"))  # fmt: skip
    for i in verdict.issues:
        print(f"   [{i.severity}] {i.category} (image {i.frame_index}): {i.observation}")
    print("\nRubric (1-10):")
    for k, v in verdict.rubric.model_dump().items():
        print(f"   {k:<17} {v}")
    mark = "OK" if verdict.creative_pass else "WARN"
    print(
        f"Creative score: {verdict.creative_score:.2f}/10 "
        f"(warn below {verdict.creative_threshold:g}) → {mark}"
    )
    print(f"Note: {verdict.creative_note}")
    for sh in verdict.shots:
        print(f"   shot {sh.number}: {'delivered' if sh.delivered else 'NOT delivered'}: {sh.note}")
    expected = verdict.passed and not verdict.creative_pass
    outcome = "YES" if expected else "NO: report before tuning"
    print(
        f"\nExpected (hard pass, creative low): {outcome}  ·  {len(calls)} call(s), "
        f"${cost:.4f}, {time.monotonic() - t0:.0f}s"
    )
    out = s.data_dir / "calibration" / f"{state.id}_ad_critic.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verdict.model_dump(mode="json"), indent=2, ensure_ascii=False))
    print(f"Saved: {out}")


if __name__ == "__main__":
    asyncio.run(main())

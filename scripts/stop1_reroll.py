"""STOP 1 follow-up: re-roll only the keyframes the STOP 1 fixes affect, on the same run.

    ADSMITH_LLM_PROVIDER=anthropic ADSMITH_MEDIA_PROVIDER=fal \
    ADSMITH_FAL_BALANCE_USD=<settled+cap> uv run python scripts/stop1_reroll.py <run_id>

1. Re-judge (LLM only, $0 fal) the approved keyframes whose composition is now HARD (key shots and
   event shots): a failure joins wave 2.
2. Wave 1: the macro shots (now composite) are re-prompted, and the spray shot switches to the
   reference-based nozzle entry. Then keyframes run until the generate gate (no video).
3. Wave 2: the re-judged failures, with what is left of the cap.
The fal cap is the wallet headroom (ADSMITH_FAL_BALANCE_USD = settled + cap), carried by the run's
media cap: a still that does not fit is refused by the budget (a gap on the sheet), never overspent.
"""

from __future__ import annotations

import argparse
import asyncio

from adsmith.agent import commands
from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
from adsmith.agent.qa import LLMCritic
from adsmith.config import Settings
from adsmith.domain.enums import Stage

DIRECT_USD = 0.04  # one Kontext still (config/models.toml: kontext_pro)
NOZZLE_AR = "القارورة بدون غطاء تطلق رشّة ضباب ناعمة أفقية من فوهتها، بإضاءة خلفية على سواد."


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    args = ap.parse_args()
    rt = AgentRuntime.from_settings(Settings())
    orch = Orchestrator(rt)
    vocab = rt.settings.vocabulary
    st = rt.storage.runs.load(args.run)
    assert st.storyboard is not None

    # 1. re-judge the approved keyframes whose composition is now the spec
    critic = LLMCritic(rt.node_context(st))
    wave2: list[int] = []
    for sc in st.storyboard.scenes:
        entry = vocab.get(sc.vocab)
        if entry is None or not entry.composition_is_spec or not sc.keyframe_approved:
            continue
        verdict, calls = await critic.review_keyframe(st, sc)
        usd = sum(c.cost_usd for c in calls)
        print(
            f"re-judge S{sc.order} {sc.vocab}: {verdict.score:.2f} "
            + ("PASS" if verdict.passed else "FAIL " + "; ".join(verdict.fail_reasons))
            + f" (LLM ${usd:.3f})"
        )
        for i in verdict.issues:
            print(f"    [{i.severity}] {i.category}: {i.observation}")
        if not verdict.passed:
            wave2.append(sc.order)

    # cap: the wallet headroom becomes the run's media cap
    headroom = rt.fal_account().summary().remaining_usd
    commands.set_budget(rt.storage, args.run, 2.5, wallet_remaining_usd=headroom)
    print(f"fal headroom ${headroom:.2f}")

    # 2. wave 1
    st = rt.storage.runs.load(args.run)
    assert st.storyboard is not None
    for sc in st.storyboard.scenes:
        if sc.vocab in {"cap_macro", "glass_edge_macro", "cap_lift"}:
            commands.edit_scene_spec(rt.storage, args.run, sc.order, reason="macro → composite")
        elif sc.vocab == "spray_burst":
            e = vocab.shots["spray_from_nozzle"]
            commands.edit_scene_spec(
                rt.storage,
                args.run,
                sc.order,
                reason="spray_burst → spray_from_nozzle (FLUX dev ignored the free burst 3x)",
                description=NOZZLE_AR,
                shot={
                    "shot_type": e.shot_type.value,
                    "camera_movement": e.camera.value,
                    "speed": e.speed.value,
                    "lighting": e.lighting,
                    "subject_action": "The bottle, cap off, releases one short, fine horizontal "
                    "plume of mist from its nozzle to the right.",
                    "composition": "the bottle in profile on the left third, the plume of mist "
                    "crossing to the right against black",
                },
                vocab=("spray_from_nozzle", e.shows_person, e.needs_product),
            )
    await orch.resume(args.run, stop_before=Stage.GENERATE)

    # 3. wave 2: only shots for which one more try still fits under the cap (a refused re-roll
    # would discard a still that the old gate approved)
    left = rt.storage.runs.load(args.run).budget.media_remaining_usd or 0.0
    fits = []
    for order in wave2:
        if left >= DIRECT_USD:
            fits.append(order)
            left -= DIRECT_USD
        else:
            print(f"S{order}: re-judged FAIL but no room left under the cap; kept as is")
    wave2 = fits
    for order in wave2:
        commands.edit_scene_spec(rt.storage, args.run, order, reason="composition is now hard")
    if wave2:
        await orch.resume(args.run, stop_before=Stage.GENERATE)
    st = rt.storage.runs.load(args.run)
    print(f"done: stage {st.stage}, media ${st.budget.media_committed_usd:.3f}")


if __name__ == "__main__":
    asyncio.run(main())

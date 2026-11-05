"""Print a real run end to end: brief, concepts, judge decision, storyboard, prompts, verdicts,
recoveries and the fal ledger.

    uv run python scripts/run_report.py <run_id> [--ledger-from N]
"""

from __future__ import annotations

import argparse

from decisions_report import decisions_text  # scripts/ is on sys.path when run as a script
from fal_run import report

from adsmith.agent.context import AgentRuntime
from adsmith.agent.nodes.common import concept_block
from adsmith.config import Settings
from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--ledger-from", type=int, default=0)
    ap.add_argument("--decisions", action="store_true", help="only what the system decided")
    args = ap.parse_args()
    rt = AgentRuntime.from_settings(Settings())
    st = rt.storage.runs.load(args.run)
    if args.decisions:
        print(decisions_text(rt, st))
        return
    assert st.brief is not None and st.concepts is not None and st.storyboard is not None
    b = st.brief
    print(
        f"BRIEF: {b.product} | {b.duration_s:g}s | tone {b.tone} | brand {b.brand_name!r} | "
        f"tagline {b.tagline!r} | dialect {b.dialect.value}"
    )
    print("\nCLICHÉS AVOIDED:", "; ".join(str(c) for c in st.concepts.category_cliches))
    for n, c in enumerate(st.concepts.concepts, 1):
        mark = "  <== SELECTED" if c.id == st.concepts.selected_id else ""
        print(f"\n{concept_block(c, n)}{mark}")
    print(f"\nSELECTION ({st.concepts.selected_by}): {st.concepts.selection_rationale}")
    for e in rt.storage.events.tail(st.id, limit=100_000):
        if e.stage in {Stage.SELECT, Stage.STORYBOARD} and e.type is EventType.DECISION:
            print(f"  [{e.stage}] {e.summary}")
            if "scores" in e.payload:
                print(f"     scores: {e.payload['scores']}")
    for sc in st.storyboard.scenes:
        sh = sc.shot
        print(f"\nSCENE {sc.order} ({sc.purpose.value}, {sc.duration_s:g}s): {sc.description}")
        print(
            f"  shot: {sh.shot_type.value}/{sh.camera_movement.value}, {sh.lens}, "
            f"speed {sh.speed.value}"
        )
        print(f"  action: {sh.subject_action}")
        print(f"  lighting: {sh.lighting}\n  composition: {sh.composition}")
        if sc.prompt:
            print(f"  KEYFRAME PROMPT ({sc.prompt.template_version}): {sc.prompt.keyframe_prompt}")
            print(f"  MOTION PROMPT: {sc.prompt.motion_prompt}")
            print(f"  NEGATIVE: {sc.prompt.negative_prompt}")
    report(rt, st.id, args.ledger_from)


if __name__ == "__main__":
    main()

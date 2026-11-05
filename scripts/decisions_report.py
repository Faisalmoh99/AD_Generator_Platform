"""What the system decided at each stage of a run, read from its state and its trace (STOP 1).

    uv run python scripts/run_report.py <run_id> --decisions
    uv run python scripts/decisions_report.py <run_id>

Format and why; the three concepts with the judge's scores and the pick; per shot the vocabulary
entry, identity mode and every keyframe-gate verdict; the product reference; budget refusals and
fallbacks; spend.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from adsmith.agent.context import AgentRuntime
from adsmith.config import Settings
from adsmith.domain.enums import FailureKind, ScenePhase, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.run import RunState


def decisions_text(rt: AgentRuntime, st: RunState) -> str:  # noqa: PLR0912, PLR0915
    events = rt.storage.events.tail(st.id, limit=100_000)
    out: list[str] = [f"DECISIONS · {st.id} · stage {st.stage.value}"]

    # 1. format
    brief_ev = next(
        (e for e in events if e.stage is Stage.UNDERSTAND and "format" in e.payload), None
    )
    if st.brief is not None:
        why = brief_ev.payload.get("format_why") if brief_ev else "(not in trace)"
        out.append(
            f"\n1. FORMAT: {st.brief.format.value} (why: {why}); requested_format="
            f"{st.brief.requested_format}, physical_product={st.brief.physical_product}, "
            f"{st.brief.duration_s:g}s, {st.brief.language.value}/{st.brief.dialect.value}"
        )

    # 2. concepts
    if st.concepts is not None:
        sel = next((e for e in events if e.stage is Stage.SELECT and "scores" in e.payload), None)
        scores: dict[int, float] = {}
        if sel is not None and isinstance(sel.payload["scores"], list):
            for s in sel.payload["scores"]:
                if isinstance(s, dict):
                    scores[int(str(s["concept_number"]))] = float(str(s["weighted"]))
        out.append(f"\n2. CONCEPTS (judge weighted score; * = picked by {st.concepts.selected_by})")
        for n, c in enumerate(st.concepts.concepts, 1):
            mark = "*" if c.id == st.concepts.selected_id else " "
            out.append(f"  {mark} {n}. {c.title} · {scores.get(n, float('nan')):.2f} · {c.logline}")
        if st.concepts.selection_rationale:
            out.append(f"    why: {st.concepts.selection_rationale}")

    # 3. product reference
    pr = st.product_reference
    out.append("\n3. PRODUCT REFERENCE")
    if pr is None:
        out.append("  none")
    else:
        v = pr.verdict
        out.append(
            f"  {'APPROVED' if pr.approved else 'NOT APPROVED'} after {pr.tries} tries, "
            f"{len(pr.rejected)} rejected, ${pr.cost_usd:.3f}"
            + (f"; last gate {v.score:.2f} {v.fail_reasons or 'pass'}" if v else "")
        )
    for e in events:
        if e.scene_id is None and "Product reference" in e.summary and e.level != "info":
            out.append(f"  [{e.level}] {e.summary[:300]}")

    # 4. shots
    by_scene: dict[str, list[AgentEvent]] = defaultdict(list)
    for e in events:
        if e.scene_id:
            by_scene[e.scene_id].append(e)
    out.append("\n4. SHOTS (vocab · identity · flags · keyframe gate per try)")
    assert st.storyboard is not None
    for sc in st.storyboard.scenes:
        current = {a.id for a in sc.attempts if a.scene_version == sc.version}
        idents = [
            str(e.payload.get("identity"))
            for e in by_scene[sc.id]
            if e.type is EventType.ATTEMPT_STARTED
            and "identity" in e.payload
            and e.payload.get("attempt_id") in current
        ]
        flags = ("person " if sc.shows_character else "") + ("product" if sc.shows_product else "")
        out.append(
            f"  S{sc.order} {sc.purpose.value:<7} {sc.duration_s:g}s · {sc.vocab or '-'} · "
            f"{idents[0] if idents else '-'} · {flags or 'no product, no person'} · "
            f"{sc.status.value}"
        )
        out.append(f"      action: {sc.shot.subject_action[:150]}")
        if sc.version > 1:
            out.append(f"      v{sc.version}: {sc.history[-1].reason[:120]}")
        tries = [
            a
            for a in sc.attempts
            if a.phase is ScenePhase.KEYFRAME and a.scene_version == sc.version
        ]
        for a in tries:
            res = {True: "PASS", False: "FAIL", None: "-"}[a.qa_passed]
            score = f"{a.qa_score:.2f}" if a.qa_score is not None else "-"
            out.append(
                f"      try {a.attempt_no}: {a.outcome} ${a.cost_usd:.3f} gate {score} {res}"
            )
        since = next((e.seq for e in by_scene[sc.id] if e.payload.get("attempt_id") in current), 0)
        for e in by_scene[sc.id]:
            if e.seq < since:
                continue
            if e.payload.get("gate") == "keyframe" and e.level != "info":
                out.append(f"      [{e.level}] {e.summary[:260]}")
            elif e.type is EventType.RECOVERY and "revision" in e.summary.lower():
                out.append(f"      [rewrite] {e.summary[:160]}")
        clips = [
            a for a in sc.attempts if a.phase is ScenePhase.CLIP and a.scene_version == sc.version
        ]
        for a in clips:
            res = {True: "PASS", False: "FAIL", None: "-"}[a.qa_passed]
            score = f"{a.qa_score:.2f}" if a.qa_score is not None else "-"
            out.append(f"      clip {a.attempt_no}: {a.outcome} ${a.cost_usd:.3f} QA {score} {res}")
        if sc.qa is not None and sc.qa.fail_reasons:
            out.append(f"      clip QA: {'; '.join(sc.qa.fail_reasons)[:200]}")
        if sc.use_window is not None:
            out.append(f"      peak window: {sc.use_window[0]:.2f}s + {sc.use_window[1]:.2f}s")
        for e in by_scene[sc.id]:
            if e.seq >= since and e.type is EventType.DECISION and "best-of" in e.summary:
                out.append(f"      [takes] {e.summary[:200]}")
        if sc.failure is not None:
            out.append(f"      FAILED {sc.failure.kind.value}: {sc.failure.reason_debug[:200]}")

    # 5. refusals / fallbacks / repairs
    out.append("\n5. BUDGET REFUSALS, FALLBACKS, REPAIRS")
    refused = [
        sc.order
        for sc in st.storyboard.scenes
        if sc.failure is not None and sc.failure.kind is FailureKind.BUDGET_EXCEEDED
    ]
    out.append(f"  scenes refused by the budget: {refused or 'none'}")
    swaps = [e for e in events if e.payload.get("strategy") == "vocab_fallback"]
    out.append(f"  vocabulary fallbacks: {len(swaps) or 'none'}")
    for e in swaps:
        out.append(f"    {e.summary[:300]}")
    repairs = [e for e in events if e.type is EventType.LLM_REPAIR]
    out.append(f"  schema repairs: {len(repairs)}")
    for e in repairs[:10]:
        out.append(f"    [{e.stage}] {e.summary[:200]}")

    # 6. music and assembly
    out.append("\n6. MUSIC AND ASSEMBLY")
    for e in events:
        music = e.summary.startswith("Music")
        if (e.stage is Stage.ASSEMBLE or music) and e.type is EventType.DECISION:
            out.append(f"  {e.summary[:300]}")

    # 7. spend
    by = st.budget.by_category
    out.append(
        f"\n7. SPEND: image ${by.get('image', 0):.3f} · video ${by.get('video', 0):.3f} · "
        f"music ${by.get('audio', 0):.3f} · LLM ${by.get('llm', 0):.3f} · "
        f"run cap ${st.budget.limit_usd:.2f}"
    )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    args = ap.parse_args()
    rt = AgentRuntime.from_settings(Settings())
    print(decisions_text(rt, rt.storage.runs.load(args.run)))


if __name__ == "__main__":
    main()

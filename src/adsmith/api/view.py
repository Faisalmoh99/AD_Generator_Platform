"""View models for the web UI: one JSON shape per run, built from state + events (read-only)."""

from __future__ import annotations

import re
from typing import Any

from adsmith.domain.enums import Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.run import RunState
from adsmith.storage import Storage

PIPELINE: list[tuple[str, str, str]] = [
    # (stage, English label, Arabic label)
    ("understand", "Brief", "الفهم"),
    ("ideate", "Concepts", "الأفكار"),
    ("select", "Pick", "الاختيار"),
    ("storyboard", "Storyboard", "اللوحة"),
    ("continuity", "Bible", "الاستمرارية"),
    ("prompt", "Prompts", "الأوامر"),
    ("estimate", "Estimate", "التقدير"),
    ("keyframe", "Keyframes", "الإطارات"),
    ("generate", "Clips", "اللقطات"),
    ("qa", "QA", "الجودة"),
    ("assemble", "Assembly", "التجميع"),
]
# Who decides inside each stage (the run-level graph's LLM-vs-code marker). The transition between
# stages is always code (agent/transitions.py).
STAGE_KIND: dict[str, tuple[str, str]] = {
    "understand": ("llm", "LLM reads the request · code resolves the format"),
    "ideate": ("llm", "LLM writes 3 concepts · code checks they differ"),
    "select": ("mixed", "LLM scores · code weights and picks"),
    "storyboard": ("llm", "LLM picks shots · code validates the grammar"),
    "continuity": ("llm", "LLM writes the bible · code lints wardrobe"),
    "prompt": ("llm", "LLM writes prompts · code lints them"),
    "estimate": ("code", "provider prices the one generation · a human approves the spend"),
    "keyframe": ("mixed", "image model · LLM vision gate · code decides pass"),
    "generate": ("code", "video model · code picks takes and windows"),
    "qa": ("mixed", "LLM vision critic · code decides pass"),
    "assemble": ("code", "ffmpeg · code cuts to the beat"),
}
# Payload keys the UI's pipeline view reads (everything else stays server-side).
_DATA_KEYS = (
    "phase", "gate", "score", "strategy", "from_vocab", "to_vocab", "winner", "cost_usd",
    "est_usd", "identity", "vocab", "bpm", "kind", "node", "format", "saved_usd",
)  # fmt: skip
_GATE_POSITION = {
    Stage.AWAITING_CONCEPT_CHOICE: "select",
    Stage.AWAITING_STORYBOARD_APPROVAL: "keyframe",
    Stage.AWAITING_GENERATION_APPROVAL: "estimate",
}


_ABS_PATH = re.compile(r"/\S*/(data/\S+)")


def media_url(path: str | None) -> str | None:
    return f"/files/{path}" if path else None


def event_json(e: AgentEvent) -> dict[str, Any]:
    p = e.payload
    tags: list[str] = []
    if e.type in {EventType.RETRY_SCHEDULED, EventType.RECOVERY}:
        tags.append("recovery")
    if e.stage is Stage.QA or p.get("gate") == "keyframe" or "QA" in e.summary:
        tags.append("qa")
    if e.type in {EventType.LLM_CALL, EventType.LLM_REPAIR, EventType.ATTEMPT_FINISHED} or (
        e.type is EventType.STAGE_COMPLETED
    ):
        tags.append("cost")
    if e.type in {EventType.EDIT_PARSED, EventType.BLAST_RADIUS}:
        tags.append("edit")
    if p.get("chaos_injected") or "chaos" in e.summary:
        tags.append("chaos")
    summary = _ABS_PATH.sub(r"\1", e.summary)  # never show local absolute paths
    if e.type is EventType.LLM_CALL:
        summary = (
            f"llm {p.get('node')} · {str(p.get('requested_model', '')).replace('claude-', '')} · "
            f"${float(str(p.get('cost_usd', 0))):.4f}"
        )
    data: dict[str, Any] = {k: p[k] for k in _DATA_KEYS if k in p and p[k] is not None}
    cands = p.get("candidates")
    if isinstance(cands, int):
        data["candidates"] = cands
    elif isinstance(cands, list):
        data["candidates"] = len(cands)
    actor, plain = decision_line(e)
    return {
        "seq": e.seq,
        "ts": e.ts.isoformat() if hasattr(e, "ts") and e.ts else None,
        "stage": e.stage.value,
        "type": e.type.value,
        "level": e.level,
        "scene_id": e.scene_id,
        "summary": summary,
        "tags": tags,
        "data": data,
        "actor": actor,
        "plain": plain,
    }


def decision_line(e: AgentEvent) -> tuple[str | None, str | None]:  # noqa: PLR0911
    """(who decided, the decision in plain language) for the UI's decision log, or (None, None)
    for events that are not decisions."""
    p, summary = e.payload, _ABS_PATH.sub(r"\1", e.summary)
    if e.type is EventType.DECISION and e.stage is Stage.SELECT and "scores" in p:
        scores = p["scores"] if isinstance(p["scores"], list) else []
        rec = p.get("recommended")
        best = next(
            (s for s in scores if isinstance(s, dict) and s.get("concept_number") == rec), None
        )
        score = f"{float(str(best['weighted'])):.2f}" if best and "weighted" in best else "?"
        verb = "picked" if summary.startswith("Selected") else "recommends"
        return "code", (
            f"Code {verb} concept {rec}: highest weighted score {score} "
            "(the LLM scored six criteria, code weighted them)"
        )
    if e.type is EventType.DECISION and "format" in p:
        return "code", f"Code chose the {p['format']} format: {p.get('format_why', '')}"
    if p.get("strategy") == "vocab_fallback":
        return "code", (
            f"Code swapped a failing shot from {p.get('from_vocab')} to its vocabulary fallback "
            f"{p.get('to_vocab')} (gate rejected it on every try)"
        )
    if e.type is EventType.DECISION and "headroom_usd" in p:
        return "code", summary.replace(" is a key shot:", ": key shot,")
    if e.type is EventType.SCENE_STATUS and "winner" in p:
        return "code", f"Code kept take {p['winner']} of the best-of-N (score {p.get('score')})"
    if e.type is EventType.DECISION and "bpm" in p:
        return "code", summary
    if e.type is EventType.DECISION and summary.startswith("Music"):
        return "code", summary
    if e.type is EventType.DECISION and e.stage in {Stage.IDEATE, Stage.STORYBOARD}:
        return "llm", summary
    if e.type is EventType.DECISION and summary.startswith(("End card", "Duration check")):
        return "code", summary
    if e.type is EventType.DECISION and "Scene" in summary and "edited" in summary:
        return "human", summary
    return None, None


def _scores(events: list[AgentEvent]) -> tuple[dict[int, dict[str, Any]], int | None]:
    """Judge scores per concept number, and the recommended concept (interactive mode)."""
    for e in reversed(events):
        if e.stage is Stage.SELECT and e.type is EventType.DECISION and "scores" in e.payload:
            raw = e.payload["scores"]
            rec = e.payload.get("recommended")
            if isinstance(raw, list):
                scores = {int(str(s["concept_number"])): s for s in raw if isinstance(s, dict)}
                return scores, int(str(rec)) if rec is not None else None
    return {}, None


_RECOVERY_LABEL = {
    "policy_rewrite": "policy rewrite",
    "qa_regenerate": "QA regeneration",
    "keyframe_regenerate": "keyframe re-roll",
    "pause_run": "paused",
}


def _recoveries(events: list[AgentEvent]) -> dict[str, list[str]]:
    """Per scene: the recoveries that actually happened (deduplicated, in order)."""
    out: dict[str, list[str]] = {}
    for e in events:
        if not e.scene_id:
            continue
        label = None
        if e.type is EventType.RETRY_SCHEDULED:
            label = "retry"
        elif e.type is EventType.RECOVERY and "keyframe_kept" in e.payload:
            label = _RECOVERY_LABEL.get(str(e.payload.get("strategy")))
        if label and label not in out.setdefault(e.scene_id, []):
            out[e.scene_id].append(label)
    return out


def _blast(state: RunState, events: list[AgentEvent]) -> dict[str, Any] | None:
    """The most recent edit's blast radius, keyed by scene order."""
    for e in reversed(events):
        if e.type is EventType.BLAST_RADIUS:
            edit = next(
                (
                    x.summary
                    for x in reversed(events)
                    if x.type is EventType.EDIT_PARSED and x.seq < e.seq
                ),
                "",
            )
            return {
                "affected": e.payload.get("affected", {}),
                "untouched": e.payload.get("untouched", []),
                "bible_bump": e.payload.get("bible_bump", False),
                "explanation": e.payload.get("explanation", []),
                "edit": edit.replace("Edit parsed: ", ""),
                "seq": e.seq,
            }
    return None


def run_view(state: RunState, storage: Storage) -> dict[str, Any]:
    events = storage.events.tail(state.id, limit=100_000)
    scores, recommended = _scores(events)
    recoveries = _recoveries(events)
    stage = state.stage
    position = _GATE_POSITION.get(stage, stage.value)
    order = [p[0] for p in PIPELINE]
    current = order.index(position) if position in order else len(order)
    concepts = []
    if state.concepts:
        for n, c in enumerate(state.concepts.concepts, start=1):
            s = scores.get(n, {})
            concepts.append(
                {
                    "number": n,
                    "id": c.id,
                    "title": c.title,
                    "logline": c.logline,
                    "insight": c.insight,
                    "hook": c.visual_hook,
                    "beats": list(c.narrative_arc),
                    "selected": c.id == state.concepts.selected_id,
                    "recommended": n == recommended,
                    "score": s.get("weighted"),
                    "producibility": s.get("producibility"),
                    "originality": s.get("originality"),
                    "cliche": s.get("uses_category_cliche"),
                    "single_shot": s.get("single_shot_plausible"),
                    "product_late": s.get("product_only_at_end"),
                    "rationale": s.get("rationale"),
                }
            )
    scenes = []
    if state.storyboard:
        for sc in state.storyboard.scenes:
            scenes.append(
                {
                    "id": sc.id,
                    "order": sc.order,
                    "purpose": sc.purpose.value,
                    "duration_s": sc.duration_s,
                    "description": sc.description,
                    "status": sc.status.value,
                    "phase": sc.phase.value,
                    "version": sc.version,
                    "attempts": len(sc.attempts),
                    "spend": round(sum(a.cost_usd for a in sc.attempts), 3),
                    "keyframe": media_url(sc.keyframe.path if sc.keyframe else None),
                    "clip": media_url(sc.clip.path if sc.clip else None),
                    "keyframe_approved": sc.keyframe_approved,
                    "qa": sc.qa.score if sc.qa else None,
                    "failure": sc.failure.reason_user if sc.failure else None,
                    "camera": sc.shot.camera_movement.value,
                    "vocab": sc.vocab,
                    "shot": sc.shot.shot_type.value,
                    "time_of_day": sc.shot.time_of_day.value,
                    "shows_character": sc.shows_character,
                    "shows_product": sc.shows_product,
                    "recoveries": recoveries.get(sc.id, []),
                    "human_override": sc.human_override.reason if sc.human_override else None,
                }
            )
    b = state.budget
    return {
        "id": state.id,
        "request": state.request,
        "stage": stage.value,
        "pipeline": [
            {
                "stage": key,
                "en": en,
                "ar": ar,
                "kind": STAGE_KIND[key][0],
                "who": STAGE_KIND[key][1],
                "state": "done" if i < current else "active" if i == current else "todo",
            }
            for i, (key, en, ar) in enumerate(PIPELINE)
        ],
        "language": state.language,
        "interactive": state.config.interactive,
        "chaos": state.config.chaos,
        "media": state.config.media_provider,
        "llm": state.config.llm_provider,
        "brief": (
            {
                "product": state.brief.product,
                "audience": state.brief.target_audience,
                "duration_s": state.brief.duration_s,
                "dialect": state.brief.dialect.value,
            }
            if state.brief
            else None
        ),
        "concepts": concepts,
        "selection": state.concepts.selection_rationale if state.concepts else None,
        "selected_by": state.concepts.selected_by if state.concepts else None,
        "scenes": scenes,
        "blast": _blast(state, events),
        "endcard": state.endcard.model_dump(mode="json") if state.endcard else None,
        "product_reference": _product_reference(state),
        "final": media_url(state.final.path if state.final else None),
        "final_duration": state.final.duration_s if state.final else None,
        "error": state.error.reason_user if state.error else None,
        "budget": {
            "limit": b.limit_usd,
            "spent": round(b.spent_usd, 4),
            "reserved": round(b.reserved_usd, 4),
            "by_category": {k: round(v, 4) for k, v in b.by_category.items()},
            "simulated": state.config.media_provider == "mock",
        },
        "generation": _generation(state),
        "gates": {"concept": state.config.interactive, "storyboard": state.config.interactive},
        "busy": stage not in {Stage.DONE, Stage.DONE_PARTIAL, Stage.ERRORED}
        and stage not in _GATE_POSITION,
    }


def _product_reference(state: RunState) -> dict[str, Any] | None:
    pr = state.product_reference
    if pr is None:
        return None
    shown = pr.artifact or (pr.rejected[-1] if pr.rejected else None)
    return {
        "url": media_url(shown.path) if shown else None,
        "approved": pr.approved,
        "score": pr.verdict.score if pr.verdict else None,
        "tries": pr.tries,
        "reasons": pr.verdict.fail_reasons if pr.verdict else [],
        "cost": round(pr.cost_usd, 3),
    }


def _generation(state: RunState) -> dict[str, Any] | None:
    g = state.generation
    if g is None:
        return None
    return {
        "status": g.status,
        "version": g.version,
        "model": g.model,
        "duration_s": g.duration_s,
        "resolution": g.resolution,
        "audio": g.generate_audio,
        "est_usd": g.est_usd,
        "approved_by": g.approved_by,
        "prompt": g.prompt.text,
        "video": media_url(g.artifact.path if g.artifact else None),
        "score": g.qa.score if g.qa else None,
        "reasons": g.qa.fail_reasons if g.qa else [],
        "failure": g.failure.reason_user if g.failure else None,
    }

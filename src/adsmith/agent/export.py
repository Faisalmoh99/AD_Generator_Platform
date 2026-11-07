"""Production Pack: a hand-off of the plan that is useful even if video generation never runs.

    runs/<id>/production_pack/storyboard.json   machine-readable (schema-versioned)
    runs/<id>/production_pack/storyboard.md     human-readable shot list for a crew or another tool

Written after the ``prompt`` stage and again after every re-prompt (orchestrator post-commit hook).
A pure function of ``RunState`` plus a file write; it never changes state.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from adsmith.domain.concept import Concept
from adsmith.domain.enums import Language, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.rubric import RUBRIC_KEYS
from adsmith.domain.run import RunState

PACK_SCHEMA_VERSION = 1

_LABELS = {
    "en": {
        "title": "Production Pack",
        "brief": "Brief",
        "concept": "Selected concept",
        "bible": "Continuity bible",
        "scenes": "Shot list",
        "scene": "Scene",
        "duration": "Duration",
        "status": "Status",
        "keyframe": "Keyframe prompt",
        "motion": "Motion prompt",
        "negative": "Negative prompt",
        "no_prompt": "_(no prompt yet)_",
        "failed": "Failed",
    },
    "ar": {
        "title": "ملف الإنتاج",
        "brief": "الموجز",
        "concept": "الفكرة المختارة",
        "bible": "مرجع الاستمرارية البصرية",
        "scenes": "قائمة اللقطات",
        "scene": "المشهد",
        "duration": "المدة",
        "status": "الحالة",
        "keyframe": "أمر الإطار الرئيسي",
        "motion": "أمر الحركة",
        "negative": "الأمر السلبي",
        "no_prompt": "_(لا يوجد أمر بعد)_",
        "failed": "فشل",
    },
}


def pack_json(state: RunState) -> dict[str, Any]:
    sb, concepts = state.storyboard, state.concepts
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "run_id": state.id,
        "state_version": state.state_version,
        "request": state.request,
        "brief": state.brief.model_dump(mode="json") if state.brief else None,
        "concept": concepts.selected.model_dump(mode="json")
        if concepts and concepts.selected
        else None,
        "category_cliches_avoided": list(concepts.category_cliches) if concepts else [],
        "insights": [x.model_dump(mode="json") for x in state.insights.insights]
        if state.insights
        else [],
        "bible": state.bible.model_dump(mode="json") if state.bible else None,
        "total_duration_s": sb.total_duration_s if sb else None,
        "endcard": (
            {
                **state.endcard.model_dump(mode="json"),
                "note": "PLACEHOLDER copy invented by the agent; replace with the client's brand"
                if state.endcard.is_placeholder
                else "from the brief",
            }
            if state.endcard
            else None
        ),
        "scenes": [
            {
                "order": s.order,
                "id": s.id,
                "version": s.version,
                "purpose": s.purpose.value,
                "duration_s": s.duration_s,
                "description": s.description,
                "shot": s.shot.model_dump(mode="json"),
                "shows_character": s.shows_character,
                "shows_product": s.shows_product,
                "prompt": s.prompt.model_dump(mode="json") if s.prompt else None,
                "status": s.status.value,
                "failure": s.failure.reason_user if s.failure else None,
                "keyframe": s.keyframe.path if s.keyframe else None,
                "clip": s.clip.path if s.clip else None,
            }
            for s in (sb.scenes if sb else [])
        ],
        "whole_ad_generation": (
            state.generation.model_dump(mode="json", exclude={"attempts", "history"})
            if state.generation
            else None
        ),
        "director_review": (
            state.director_review.model_dump(mode="json") if state.director_review else None
        ),
        "copy_selection": (
            state.copy_selection.model_dump(mode="json") if state.copy_selection else None
        ),
    }


def _reference_lines(state: RunState) -> list[str]:
    """The images sent with the whole-ad prompt, in order (Image 1 is the product photo)."""
    if not state.reference_images:
        return []
    photo = f" ({state.product_photo.path})" if state.product_photo else ""
    out = ["References sent with the prompt:", "", f"- Image 1: product photo{photo}"]
    for i, r in enumerate(state.reference_images, start=2):
        extra = f", ${r.cost_usd:.3f}, {r.check}" if r.source == "generated" else ""
        link = f"![{r.name}](../../../{r.artifact.path})"
        out.append(f"- Image {i}: {r.name} ({r.source}{extra}) {link}")
    return [*out, ""]


def _director_lines(state: RunState) -> list[str]:
    """The creative director's review (ADR-0026): verdicts per rule, changes, before/after."""
    r = state.director_review
    if r is None:
        return []
    out = [f"## Director review: {r.outcome}{' (re-timed)' if r.retimed else ''}", ""]
    for rnd in r.rounds:
        out.append(f"Round {rnd.round}: {rnd.summary}")
        out += [f"- {'PASS' if v.passed else 'FAIL'} {v.rule_id}: {v.note}" for v in rnd.verdicts]
        out += [f"- change: {c}" for c in rnd.changes]
        out.append("")
    if r.note:
        out += [f"Note: {r.note}", ""]
    if r.outcome == "rewritten":
        out += ["Writer's prompt (before the director):", "", "```text", r.before_text, "```", ""]
    return out


def _copy_lines(state: RunState) -> list[str]:
    """The end-card copy the system wrote (ADR-0028): every line with its score; the pick."""
    sel = state.copy_selection
    if sel is None:
        return []
    out = [f"## End-card copy ({sel.dialect})", ""]
    if state.endcard is not None:
        out += [f"Picked by code: **{state.endcard.brand_name}** — **{state.endcard.tagline}**", ""]
    out += ["| # | tagline | device | gloss | weighted | note |", "|---|---|---|---|---|---|"]
    for i, t in enumerate(sel.taglines, 1):
        mark = " (chosen)" if i - 1 == sel.tagline_index else ""
        w = f"{t.weighted:.2f}" if t.weighted is not None else "-"
        g = " (generic)" if t.generic else ""
        out.append(f"| {i} | {t.text}{mark} | {t.device} | {t.gloss_en} | {w}{g} | {t.note} |")
    if sel.brands:
        out += ["", "Brand names: " + "; ".join(
            f"{b.text}{' (chosen)' if i == sel.brand_index else ''} ({b.weighted or '-'})"
            for i, b in enumerate(sel.brands)
        )]  # fmt: skip
    return [*out, ""]


def _creative_lines(concept: Concept) -> list[str]:
    """The creative layer of the chosen concept (ideate v6), if it has one."""
    lines: list[str] = []
    if concept.story is not None:
        lines += [
            f"- Insight: {concept.insight}",
            f"- Story: {concept.story.setup} / {concept.story.turn} / {concept.story.payoff}",
        ]
    if concept.hook is not None:
        lines.append(f"- Hook: {concept.hook.image} ({concept.hook.question})")
    if concept.product_role is not None:
        lines.append(f"- Product: {concept.product_role.role}: {concept.product_role.line}")
    if concept.cultural_meaning:
        lines.append(f"- Culture: {concept.cultural_meaning}")
    return [*lines, ""] if lines else []


def pack_markdown(state: RunState, scores: Mapping[int, Mapping[str, object]] | None = None) -> str:
    lang = "ar" if state.language == Language.AR.value else "en"
    t = _LABELS[lang]
    out = [f"# {t['title']}: {state.id}", ""]
    if state.brief:
        b = state.brief
        out += [
            f"## {t['brief']}",
            "",
            f"> {state.request}",
            "",
            f"- {b.product} · {b.target_audience} · {b.duration_s:g}s · {b.aspect_ratio}",
            "",
        ]
    if state.insights:
        out += ["## Insights", ""]
        out += [
            f"{i}. {x.statement}"
            + (f" (culture anchor: {x.culture_anchor})" if x.culture_anchor != "none" else "")
            for i, x in enumerate(state.insights.insights, 1)
        ]
        out.append("")
    concept = state.concepts.selected if state.concepts else None
    if concept:
        out += _concept_lines(state, scores or {})
        out += [f"## {t['concept']}: {concept.title}", "", concept.logline, ""]
        out += _creative_lines(concept)
    if state.generation is not None:
        g = state.generation
        est = f"${g.est_usd:.3f}" if g.est_usd is not None else "-"
        out += [
            f"## Whole-ad prompt ({g.model}, {g.duration_s}s {g.resolution}, estimate {est})",
            "",
            "```text",
            g.prompt_text,
            "```",
            "",
        ]
        out += _reference_lines(state)
        out += _director_lines(state)
        out += _copy_lines(state)
    if state.bible:
        out += [
            f"## {t['bible']} (v{state.bible.version})",
            "",
            "```text",
            state.bible.prompt_block(),
            "```",
            "",
        ]
    if state.storyboard:
        if state.endcard:
            e = state.endcard
            flag = (
                " — **PLACEHOLDER** (invented by the agent; replace before publishing)"
                if e.is_placeholder
                else ""
            )
            out += ["## End card", "", f"**{e.brand_name}**: {e.tagline}{flag}", ""]
        out += [f"## {t['scenes']}", ""]
        for s in state.storyboard.scenes:
            sh = s.shot
            out += [
                f"### {t['scene']} {s.order} · {s.purpose.value} · {s.duration_s:g}s",
                "",
                s.description,
                "",
                "| | |",
                "|---|---|",
                f"| Shot | {sh.shot_type.value} |",
                f"| Camera | {sh.camera_movement.value} ({sh.speed.value}) |",
                f"| Lens | {sh.lens} |",
                f"| Lighting | {sh.lighting} |",
                f"| Time of day | {sh.time_of_day.value} |",
                f"| Composition | {sh.composition} |",
                f"| Action | {sh.subject_action} |",
                f"| Setting | {sh.setting} |",
                f"| Mood | {sh.mood} |",
                f"| {t['status']} | {s.status.value} |",
                "",
            ]
            if s.failure:
                out += [f"**{t['failed']}:** {s.failure.reason_user}", ""]
            if s.prompt:
                out += [
                    f"**{t['keyframe']}**",
                    "",
                    "```text",
                    s.prompt.keyframe_prompt,
                    "```",
                    "",
                    f"**{t['motion']}**",
                    "",
                    "```text",
                    s.prompt.motion_prompt,
                    "```",
                    "",
                    f"**{t['negative']}**",
                    "",
                    "```text",
                    s.prompt.negative_prompt,
                    "```",
                    "",
                ]
            else:
                out += [t["no_prompt"], ""]
            if s.keyframe:
                out += [f"![{t['scene']} {s.order}](../../../{s.keyframe.path})", ""]
    return "\n".join(out).rstrip() + "\n"


def concept_scores(events: Sequence[AgentEvent]) -> dict[int, dict[str, object]]:
    """The judge's scores per concept number, from the select decision event."""
    for e in reversed(events):
        if e.stage is Stage.SELECT and e.type is EventType.DECISION and "scores" in e.payload:
            raw = e.payload["scores"]
            if isinstance(raw, list):
                return {
                    int(str(s["concept_number"])): dict(s.items())
                    for s in raw
                    if isinstance(s, dict)
                }
    return {}


def _concept_lines(state: RunState, scores: Mapping[int, Mapping[str, object]]) -> list[str]:
    """All three concepts with the six rubric scores and the weighted total (ADR-0024)."""
    if state.concepts is None or not scores:
        return []
    out = ["## Concepts and rubric", ""]
    head = " | ".join(RUBRIC_KEYS)
    out += [f"| # | concept | {head} | total |", "|" + "---|" * (len(RUBRIC_KEYS) + 3)]
    for n, c in enumerate(state.concepts.concepts, 1):
        s = scores.get(n, {})
        mark = " (picked)" if c.id == state.concepts.selected_id else ""
        cells = " | ".join(str(s.get(k, "-")) for k in RUBRIC_KEYS)
        cliche = " (cliché)" if s.get("uses_category_cliche") else ""
        out.append(f"| {n} | {c.title}{mark} | {cells} | {s.get('weighted', '-')}{cliche} |")
    dropped = state.concepts.candidates_dropped
    if dropped:
        out += ["", "Written but not kept by code (least different, ADR-0028):", ""]
        for c in dropped:
            turn = f" Turn: {c.story.turn}" if c.story else ""
            out.append(f"- [{c.axis or '-'}] **{c.title}**: {c.logline}{turn} "
                       f"(motifs: {', '.join(c.motifs) or '-'})")  # fmt: skip
    return [*out, ""]


def write_production_pack(
    state: RunState, out_dir: Path, scores: Mapping[int, Mapping[str, object]] | None = None
) -> Path:
    """Atomically (re)write the pack: build in a temp dir, then swap it into place."""
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=out_dir.parent, prefix=".pack-"))
    try:
        (tmp / "storyboard.json").write_text(
            json.dumps(pack_json(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (tmp / "storyboard.md").write_text(pack_markdown(state, scores or {}), encoding="utf-8")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        tmp.replace(out_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_dir


def zip_production_pack(pack_dir: Path) -> Path:
    archive = shutil.make_archive(str(pack_dir), "zip", root_dir=pack_dir)
    return Path(archive)

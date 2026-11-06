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
from pathlib import Path
from typing import Any

from adsmith.domain.concept import Concept
from adsmith.domain.enums import Language
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
    }


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


def pack_markdown(state: RunState) -> str:
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
        out += [f"{i}. {x.statement}" for i, x in enumerate(state.insights.insights, 1)]
        out.append("")
    concept = state.concepts.selected if state.concepts else None
    if concept:
        out += [f"## {t['concept']}: {concept.title}", "", concept.logline, ""]
        out += _creative_lines(concept)
    if state.generation is not None:
        g = state.generation
        est = f"${g.est_usd:.3f}" if g.est_usd is not None else "-"
        out += [
            f"## Whole-ad prompt ({g.model}, {g.duration_s}s {g.resolution}, estimate {est})",
            "",
            "```text",
            g.prompt.text,
            "```",
            "",
        ]
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


def write_production_pack(state: RunState, out_dir: Path) -> Path:
    """Atomically (re)write the pack: build in a temp dir, then swap it into place."""
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=out_dir.parent, prefix=".pack-"))
    try:
        (tmp / "storyboard.json").write_text(
            json.dumps(pack_json(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (tmp / "storyboard.md").write_text(pack_markdown(state), encoding="utf-8")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        tmp.replace(out_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_dir


def zip_production_pack(pack_dir: Path) -> Path:
    archive = shutil.make_archive(str(pack_dir), "zip", root_dir=pack_dir)
    return Path(archive)

"""Blast radius: which scenes an edit invalidates, and how deep (PLAN §6 step 3, ADR-0005).

Pure functions, no I/O. Depth table (shared with ``Scene.keyframe_spec_hash`` through
``MOTION_SHOT_FIELDS``, so the two can never disagree):

- motion only (``camera_movement``, ``speed``, ``duration_s``, ``motion_change``) → ``clip``:
  the keyframe is still valid; only the motion prompt / render changes.
- any other shot field or the description → ``prompt``: new prompt, new keyframe, new clip.
- bible change → ``prompt`` for every scene that uses the changed section (a wardrobe change
  skips product-only inserts).
- duration change → the other scenes are rebalanced proportionally (within the model's bounds)
  at ``clip`` depth; when their rendered length does not change the clip is a cache hit.
- ``propagate_neighbors`` → the neighbours named in conflicts get the same shot change, at
  ``prompt`` depth.
"""

from __future__ import annotations

from adsmith.domain.continuity import ContinuityBible
from adsmith.domain.edit import BlastRadius, ConflictReport, Resolution, ScenePatchDraft
from adsmith.domain.enums import BibleSection, StaleFrom
from adsmith.domain.scene import MOTION_SHOT_FIELDS, Scene
from adsmith.domain.storyboard import Storyboard


def depth_for(patch: ScenePatchDraft) -> StaleFrom | None:
    look = (patch.shot.changed_fields() - MOTION_SHOT_FIELDS) or patch.description_change
    if look:
        return StaleFrom.PROMPT
    if patch.shot.changed_fields() or patch.duration_s is not None or patch.motion_change:
        return StaleFrom.CLIP
    return None


def rebalance_durations(
    sb: Storyboard, target_id: str, new_duration: float, *, min_s: float, max_s: float
) -> dict[str, float]:
    """New durations for the OTHER scenes so the total stays fixed (proportional, clamped)."""
    others = [s for s in sb.scenes if s.id != target_id]
    remaining = sb.target_duration_s - new_duration
    if not others or remaining <= 0:
        raise ValueError("the other scenes would have no time left")
    if not min_s * len(others) - 1e-9 <= remaining <= max_s * len(others) + 1e-9:
        raise ValueError(
            f"{new_duration:g}s leaves {remaining:g}s for {len(others)} scene(s); each needs "
            f"{min_s:g}–{max_s:g}s"
        )
    total = sum(s.duration_s for s in others)
    out = {s.id: round(s.duration_s * remaining / total, 2) for s in others}
    for _ in range(len(others)):  # clamp, then hand the excess to scenes with room
        excess = 0.0
        for sid, d in out.items():
            clamped = min(max(d, min_s), max_s)
            excess += d - clamped
            out[sid] = clamped
        if abs(excess) < 1e-6:
            break
        free = [sid for sid, d in out.items() if min_s < d + excess / len(out) < max_s]
        for sid in free or list(out):
            out[sid] = round(out[sid] + excess / len(free or out), 2)
    drift = round(remaining - sum(out.values()), 2)
    last = others[-1].id
    out[last] = round(out[last] + drift, 2)
    return {sid: d for sid, d in out.items() if abs(d - sb.by_id(sid).duration_s) > 1e-6}


def compute_blast_radius(
    sb: Storyboard,
    bible: ContinuityBible,
    patch: ScenePatchDraft,
    target_ids: list[str],
    conflicts: ConflictReport,
    resolution: Resolution,
    *,
    patch_id: str,
    min_s: float,
    max_s: float,
) -> BlastRadius:
    affected: dict[str, StaleFrom] = {}
    why: list[str] = []
    by_id = {s.id: s for s in sb.scenes}

    def mark(scene: Scene, depth: StaleFrom, reason: str) -> None:
        affected[scene.id] = StaleFrom.deepest(affected.get(scene.id), depth)
        why.append(f"scene {scene.order} → {depth.value}: {reason}")

    depth = depth_for(patch)
    changed = sorted(patch.shot.changed_fields())
    extra = (["duration"] if patch.duration_s is not None else []) + (
        ["motion"] if patch.motion_change else []
    )
    if depth is not None:
        for sid in target_ids:
            what = ", ".join(
                changed + extra + (["description"] if patch.description_change else [])
            )
            keep = " (keyframe kept)" if depth is StaleFrom.CLIP else ""
            mark(by_id[sid], depth, f"edited {what}{keep}")

    rebalance: dict[str, float] = {}
    if patch.duration_s is not None and len(target_ids) == 1:
        rebalance = rebalance_durations(
            sb, target_ids[0], patch.duration_s, min_s=min_s, max_s=max_s
        )
        for sid, d in rebalance.items():
            mark(by_id[sid], StaleFrom.CLIP, f"duration rebalanced to {d:g}s (keyframe kept)")

    if resolution == "propagate_neighbors":
        for c in conflicts.conflicts:
            if c.against in {"prev_scene", "next_scene"} and c.scene_id in by_id:
                mark(by_id[c.scene_id], StaleFrom.PROMPT, f"propagated: {c.description}")

    sections = patch.changed_bible_sections()
    if sections:
        for s in sb.scenes:
            hit = sections & s.uses_bible_sections()
            if hit:
                names = ", ".join(sorted(h.value for h in hit))
                mark(s, StaleFrom.PROMPT, f"uses the changed bible section(s): {names}")
        untouched = [s.order for s in sb.scenes if s.id not in affected]
        if untouched:
            why.append(f"scenes {untouched} do not use the changed bible section(s): untouched")
    if not why:
        why.append("nothing to regenerate")
    return BlastRadius(
        patch_id=patch_id,
        affected=affected,
        bible_bump=bool(sections),
        duration_rebalance=rebalance,
        explanation=[w[:300] for w in why],
    )


def apply_bible_changes(bible: ContinuityBible, changes: dict[str, str]) -> ContinuityBible:
    """Dotted-path edits ('character.wardrobe') → a new, validated bible with version + 1.
    List fields take ';'-separated items. Unknown paths raise ValueError (→ clarification)."""
    data = bible.model_dump()
    for path, value in changes.items():
        section, _, field = path.partition(".")
        BibleSection(section)  # validates the section name
        node = data.get(section)
        if not field:
            raise ValueError(f"bible change {path!r} must name a field, e.g. '{section}.<field>'")
        if not isinstance(node, dict) or field not in node:
            raise ValueError(f"unknown bible field {path!r}")
        current = node[field]
        node[field] = (
            [v.strip() for v in value.split(";") if v.strip()]
            if isinstance(current, list)
            else value
        )
    data["version"] = bible.version + 1
    return ContinuityBible.model_validate(data)

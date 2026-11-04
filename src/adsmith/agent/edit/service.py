"""EditService: natural-language edit → typed patch → conflicts → blast radius → apply (PLAN §6).

1. **Parse** (``edit_parser`` LLM) into a ``ScenePatchDraft``; scene references are resolved
   deterministically (``refs.py``). Low confidence, a clarification question or an unresolved
   reference ⇒ ask back, never guess.
2. **Conflicts**: deterministic findings (time-of-day jumps against neighbours, duration budget)
   are handed to the ``conflict_check`` LLM, which keeps the real ones and suggests a resolution.
   Interactive callers may override it; auto mode uses the suggestion.
3. **Blast radius** (pure, ``blast.py``): which scenes go stale and how deep.
4. **Motion-only edits** get a motion-prompt rewrite with the keyframe kept (``spec_edit``).
5. **Apply** in ONE atomic commit: patch recorded, bible bumped if needed, each affected scene
   archived (``SceneVersion``), patched and marked stale at its depth, clip-depth scenes
   requeued at once; the run re-enters at ``prompt`` (or ``generate`` if nothing needs a new
   prompt). Untouched scenes stay approved: they make no provider call at all.

The caller then runs the orchestrator; assembly produces the new final.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from adsmith.agent.context import AgentRuntime, render_window
from adsmith.agent.edit.blast import apply_bible_changes, compute_blast_radius, depth_for
from adsmith.agent.edit.refs import UnresolvedRefError, resolve_ref, resolve_targets
from adsmith.agent.nodes.common import language_name, scene_duration_bounds
from adsmith.agent.nodes.rewrite import rewrite_prompt
from adsmith.agent.recovery.errors import AgentError
from adsmith.config import LLMNode
from adsmith.domain.continuity import ContinuityBible
from adsmith.domain.edit import (
    BlastRadius,
    Conflict,
    ConflictReport,
    Resolution,
    ScenePatch,
    ScenePatchDraft,
    SceneRef,
)
from adsmith.domain.enums import SceneStatus, Stage, StaleFrom
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene, ScenePrompt, ShotSpec
from adsmith.domain.scene_flags import enforce_scene_flags
from adsmith.domain.storyboard import Storyboard

MIN_CONFIDENCE = 0.6
EDITABLE_STAGES = frozenset(
    {Stage.DONE, Stage.DONE_PARTIAL, Stage.AWAITING_STORYBOARD_APPROVAL, Stage.ERRORED}
)


class EditRejectedError(ValueError):
    """The run cannot take an edit right now (e.g. it is still generating)."""


@dataclass
class EditOutcome:
    status: Literal["applied", "clarify", "rejected"]
    message: str
    patch: ScenePatchDraft | None = None
    target_orders: list[int] = field(default_factory=list)
    conflicts: ConflictReport | None = None
    resolution: Resolution | None = None
    blast: BlastRadius | None = None
    llm_calls: list[LLMCallRecord] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.llm_calls)


def _scene_line(s: Scene) -> str:
    sh = s.shot
    return (
        f"Scene {s.order} (id {s.id}, {s.purpose.value}, {s.duration_s:g}s): {s.description}\n"
        f"  shot {sh.shot_type.value}, camera {sh.camera_movement.value}, speed {sh.speed.value}, "
        f"time {sh.time_of_day.value}\n  action: {sh.subject_action}\n  lighting: {sh.lighting}\n"
        f"  shows character: {s.shows_character}; shows product: {s.shows_product}"
    )


def _bible_fields(bible: ContinuityBible) -> str:
    data = bible.model_dump(exclude={"version"})
    lines = []
    for section, value in data.items():
        if isinstance(value, dict):
            for k, v in value.items():
                lines.append(f"{section}.{k}: {'; '.join(v) if isinstance(v, list) else v}")
        else:
            lines.append(f"{section}: {value}")
    return "\n".join(lines)


def deterministic_findings(sb: Storyboard, patch: ScenePatchDraft, targets: list[str]) -> list[str]:
    out = []
    new_tod = patch.shot.time_of_day
    if new_tod is not None:
        for sid in targets:
            prev, nxt = sb.neighbors(sid)
            for label, n in (("prev_scene", prev), ("next_scene", nxt)):
                if n is not None and n.id not in targets and n.shot.time_of_day is not new_tod:
                    out.append(
                        f"{label} scene {n.order} (id {n.id}) is {n.shot.time_of_day.value}; the "
                        f"edit makes scene {sb.by_id(sid).order} {new_tod.value}: a time jump "
                        "across the cut unless it is a separate moment"
                    )
    return out


class EditService:
    def __init__(self, rt: AgentRuntime) -> None:
        self.rt = rt

    async def edit(  # noqa: PLR0911, PLR0912, PLR0915 - one linear, documented flow
        self, run_id: str, instruction: str, *, resolution: Resolution | None = None
    ) -> EditOutcome:
        state = self.rt.storage.runs.load(run_id)
        if state.stage not in EDITABLE_STAGES or state.storyboard is None or state.bible is None:
            raise EditRejectedError(f"run is at {state.stage}; edits apply to a finished run")
        sb, bible = state.storyboard, state.bible
        ctx = self.rt.node_context(state)
        calls: list[LLMCallRecord] = []

        # 1. parse ---------------------------------------------------------------------------
        prompt = ctx.prompts.get("edit_parser").render(
            scenes="\n\n".join(_scene_line(s) for s in sb.scenes),
            bible=_bible_fields(bible),
            instruction=instruction,
        )
        try:
            parsed = await ctx.llm.call(LLMNode.EDIT_PARSER, prompt, ScenePatchDraft)
        except AgentError as exc:
            self._record(run_id, [], exc.llm_calls, f"Edit not understood: {exc.message}")
            return EditOutcome("rejected", exc.message, llm_calls=exc.llm_calls)
        patch, calls = parsed.value, list(parsed.calls)
        if patch.clarification_needed or patch.confidence < MIN_CONFIDENCE:
            question = patch.clarification_needed or "Which scene and which change do you mean?"
            self._record(run_id, [], calls, f"Edit needs clarification: {question}")
            return EditOutcome("clarify", question, patch=patch, llm_calls=calls)
        try:
            targets = resolve_targets(patch.targets, sb)
        except UnresolvedRefError as exc:
            self._record(run_id, [], calls, f"Edit needs clarification: {exc}")
            return EditOutcome("clarify", str(exc), patch=patch, llm_calls=calls)
        orders = [sb.by_id(t).order for t in targets]

        # 2. conflicts -----------------------------------------------------------------------
        min_s, max_s, _single = scene_duration_bounds(
            self.rt.settings, state.config.video_model, sb.format
        )
        findings = deterministic_findings(sb, patch, targets)
        neighbour_ids = {
            n.id for t in targets for n in sb.neighbors(t) if n is not None and n.id not in targets
        }
        report = ConflictReport()
        if findings or neighbour_ids:
            cprompt = ctx.prompts.get("conflict_check").render(
                patch=patch.model_dump_json(exclude_none=True, indent=2),
                targets="\n\n".join(_scene_line(sb.by_id(t)) for t in targets) or "(bible only)",
                neighbours="\n\n".join(_scene_line(s) for s in sb.scenes if s.id in neighbour_ids)
                or "(none)",
                bible=bible.prompt_block(),
                findings="\n".join(f"- {f}" for f in findings) or "(none)",
                user_language=language_name(state.brief.language) if state.brief else "English",
            )
            try:
                checked = await ctx.llm.call(LLMNode.CONFLICT_CHECK, cprompt, ConflictReport)
                report = _normalise_scene_refs(checked.value, sb)
                calls += checked.calls
            except AgentError as exc:  # degrade to the deterministic findings, apply locally
                calls += exc.llm_calls
                report = ConflictReport(
                    conflicts=[],
                    suggested_resolution="apply_local",
                    explanation=f"conflict check failed ({exc.message[:80]}); applied locally",
                )
        chosen: Resolution = resolution or report.suggested_resolution
        if chosen == "reject":
            self._record(run_id, [], calls, f"Edit rejected: {report.explanation}")
            return EditOutcome(
                "rejected", report.explanation, patch, orders, report, chosen, llm_calls=calls
            )

        # 3. bible + blast radius ------------------------------------------------------------
        new_bible = None
        if patch.bible_changes:
            try:
                new_bible = apply_bible_changes(bible, patch.bible_changes)
            except ValueError as exc:
                self._record(run_id, [], calls, f"Edit needs clarification: {exc}")
                return EditOutcome("clarify", str(exc), patch, orders, llm_calls=calls)
        scene_patch = ScenePatch(
            **patch.model_dump(), raw_instruction=instruction, resolved_scene_ids=targets
        )
        try:
            blast = compute_blast_radius(
                sb, bible, patch, targets, report, chosen,
                patch_id=scene_patch.id, min_s=min_s, max_s=max_s,
            )  # fmt: skip
        except ValueError as exc:  # e.g. impossible duration budget
            self._record(run_id, [], calls, f"Edit needs clarification: {exc}")
            return EditOutcome("clarify", str(exc), patch, orders, report, chosen, llm_calls=calls)
        if not blast.affected and new_bible is None:
            return EditOutcome("rejected", "this edit changes nothing", patch, orders, report,
                               chosen, blast, calls)  # fmt: skip

        # 4. motion-only rewrites (keyframe kept) --------------------------------------------
        motion_prompts: dict[str, ScenePrompt] = {}
        if depth_for(patch) is StaleFrom.CLIP:
            for sid in targets:
                scene = _patched(sb.by_id(sid), patch, is_target=True)
                if scene.prompt is None:
                    continue
                render_s, _ = render_window(self.rt.settings, state, scene.duration_s)
                try:
                    res = await rewrite_prompt(
                        ctx, scene, new_bible or bible, reason="spec_edit",
                        detail=_motion_detail(sb.by_id(sid), patch), render_s=render_s,
                        keep_keyframe=True,
                    )  # fmt: skip
                except AgentError as exc:
                    calls += exc.llm_calls
                    self._record(run_id, [], calls, f"Motion rewrite failed: {exc.message}")
                    return EditOutcome("rejected", exc.message, patch, orders, report, chosen,
                                       blast, calls)  # fmt: skip
                motion_prompts[sid] = res.value
                calls += res.calls

        # 5. apply atomically ----------------------------------------------------------------
        self._apply(run_id, scene_patch, blast, targets, new_bible, motion_prompts, calls, report,
                    chosen, orders)  # fmt: skip
        return EditOutcome("applied", report.explanation or "applied", patch, orders, report,
                           chosen, blast, calls)  # fmt: skip

    # ------------------------------------------------------------------------------ apply
    def _apply(
        self,
        run_id: str,
        patch: ScenePatch,
        blast: BlastRadius,
        targets: list[str],
        new_bible: ContinuityBible | None,
        motion_prompts: dict[str, ScenePrompt],
        calls: list[LLMCallRecord],
        report: ConflictReport,
        resolution: Resolution,
        orders: list[int],
    ) -> None:
        def mutate(s: RunState) -> list[AgentEvent]:
            assert s.storyboard is not None
            if any(sc.status is SceneStatus.GENERATING for sc in s.storyboard.scenes):
                raise EditRejectedError("a scene is generating; retry when it settles")
            events = _book(s, calls)
            s.patches.append(patch)
            if new_bible is not None:
                if s.bible is not None:
                    s.bible_history.append(s.bible)  # keep the version scenes were made with
                s.bible = new_bible
            touched = []
            propagated = {
                c.scene_id
                for c in report.conflicts
                if resolution == "propagate_neighbors" and c.against in {"prev_scene", "next_scene"}
            }
            for sid, depth in blast.affected.items():
                sc = s.storyboard.by_id(sid)
                sc.bump_version(f"edit: {patch.raw_instruction[:200]}", patch.id)
                updated = _patched(
                    sc, patch, is_target=sid in targets,
                    shot_change=sid in targets or sid in propagated,
                    duration=blast.duration_rebalance.get(sid),
                )  # fmt: skip
                sc.shot, sc.description, sc.duration_s = (
                    updated.shot,
                    updated.description,
                    updated.duration_s,
                )
                sc.mark_stale(depth)
                if sid in motion_prompts:
                    sc.set_prompt(motion_prompts[sid])
                if sc.prompt is not None:
                    sc.requeue()  # clip/keyframe depth: no new prompt needed
                touched.append(sc)
            enforce_scene_flags(touched, s.bible)
            needs_prompt = any(sc.prompt is None for sc in touched)
            s.stage = Stage.PROMPT if needs_prompt else Stage.GENERATE
            s.final = None
            untouched = [sc.order for sc in s.storyboard.scenes if sc.id not in blast.affected]
            radius = {
                str(s.storyboard.by_id(sid).order): d.value for sid, d in blast.affected.items()
            }
            events += [
                AgentEvent(
                    run_id=s.id,
                    stage=s.stage,
                    type=EventType.EDIT_PARSED,
                    summary=f"Edit parsed: {patch.raw_instruction[:120]!r} → scenes {orders or '-'}"
                    + (f"; bible {sorted(patch.bible_changes)}" if patch.bible_changes else ""),
                    payload={
                        "patch": patch.model_dump(mode="json", exclude_none=True),
                        "resolution": resolution,
                        "conflicts": report.model_dump(mode="json"),
                    },
                ),
                AgentEvent(
                    run_id=s.id,
                    stage=s.stage,
                    type=EventType.BLAST_RADIUS,
                    summary=f"Blast radius: {radius or 'none'}"
                    + (f"; bible v{new_bible.version}" if new_bible else "")
                    + f"; untouched (0 provider calls): {untouched or 'none'}",
                    payload={
                        "affected": dict[str, JsonValue](radius),
                        "untouched": list[JsonValue](untouched),
                        "bible_bump": blast.bible_bump,
                        "explanation": list(blast.explanation),
                    },
                ),
            ]
            return events

        self.rt.storage.runs.update(run_id, mutate, reason=f"edit {patch.id}")

    def _record(
        self, run_id: str, events: list[AgentEvent], calls: list[LLMCallRecord], summary: str
    ) -> None:
        def mutate(s: RunState) -> list[AgentEvent]:
            return [
                *events,
                *_book(s, calls),
                AgentEvent(
                    run_id=s.id,
                    stage=s.stage,
                    type=EventType.AWAITING_INPUT,
                    summary=summary[:500],
                ),
            ]

        self.rt.storage.runs.update(run_id, mutate, reason="edit not applied")


def _normalise_scene_refs(report: ConflictReport, sb: Storyboard) -> ConflictReport:
    """Conflicts may name a neighbour by number ("4", "scene 4") instead of its id: map it with
    the same deterministic resolver; drop references that do not resolve to a real scene."""
    ids = {s.id for s in sb.scenes}
    fixed: list[Conflict] = []
    for c in report.conflicts:
        if c.scene_id is None or c.scene_id in ids:
            fixed.append(c)
            continue
        try:
            (sid,) = resolve_ref(SceneRef(kind="ordinal", value=c.scene_id), sb)
        except (UnresolvedRefError, ValueError):
            continue
        fixed.append(c.model_copy(update={"scene_id": sid}))
    return report.model_copy(update={"conflicts": fixed})


def _book(s: RunState, calls: list[LLMCallRecord]) -> list[AgentEvent]:
    out = []
    for c in calls:
        s.budget.record_actual("llm", c.cost_usd, ref=c.id)
        out.append(c.as_event(s.id, s.stage))
    return out


def _patched(
    scene: Scene,
    patch: ScenePatchDraft,
    *,
    is_target: bool,
    shot_change: bool | None = None,
    duration: float | None = None,
) -> Scene:
    """The scene with the patch applied (targets get everything; propagated neighbours get the
    shot change only; rebalanced scenes get their new duration)."""
    shot = scene.shot
    changes = patch.shot.model_dump(exclude_none=True)
    if changes and (is_target if shot_change is None else shot_change):
        shot = ShotSpec.model_validate({**shot.model_dump(), **changes})
    update: dict[str, object] = {"shot": shot}
    if is_target:
        if patch.description_change:
            update["description"] = patch.description_change
        if patch.duration_s is not None:
            update["duration_s"] = patch.duration_s
    if duration is not None:
        update["duration_s"] = duration
    return scene.model_copy(update=update)


def _motion_detail(scene: Scene, patch: ScenePatchDraft) -> str:
    lines = ["The user edited this scene's motion. Changes:"]
    for k, v in patch.shot.model_dump(exclude_none=True).items():
        lines.append(f"- {k}: {getattr(scene.shot, k)!s} → {v}")
    if patch.duration_s is not None:
        lines.append(f"- duration: {scene.duration_s:g}s → {patch.duration_s:g}s")
    if patch.motion_change:
        lines.append(f"- motion: {patch.motion_change}")
    lines.append(f"User request: {json.dumps(patch.model_dump(exclude_none=True))[:300]}")
    return "\n".join(lines)

"""Human commands at the gates. Each is one atomic, validated state update.

Commands may carry ``expected_version``, the state version the human was looking at. If the run
moved on, ``ConcurrencyConflictError`` is raised and the client must re-read (ADR-0011).
"""

from __future__ import annotations

from pydantic import JsonValue

from adsmith.agent.transitions import GATE_EXITS
from adsmith.domain.ad import (
    HumanPrompt,
    human_prompt_problems,
    parse_prompt_shots,
    prompt_diff_summary,
)
from adsmith.domain.enums import FailureKind, Language, SceneStatus, Stage, StaleFrom
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.generation import Artifact
from adsmith.domain.qa import QAVerdict
from adsmith.domain.run import RunState
from adsmith.domain.scene import SceneFailure, ShotSpec
from adsmith.domain.scene_flags import enforce_scene_flags
from adsmith.storage import Storage


class CommandRejectedError(ValueError):
    """The command does not apply to the run's current state."""


def choose_concept(
    storage: Storage, run_id: str, choice: str | int, *, expected_version: int | None = None
) -> RunState:
    """Pick a concept by number (1–3) or id at the concept gate."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.stage is not Stage.AWAITING_CONCEPT_CHOICE or s.concepts is None:
            raise CommandRejectedError(f"run is at {s.stage}, not waiting for a concept choice")
        concepts = s.concepts.concepts
        if isinstance(choice, int) or (isinstance(choice, str) and choice.isdigit()):
            n = int(choice)
            if not 1 <= n <= len(concepts):
                raise CommandRejectedError(f"choose a concept between 1 and {len(concepts)}")
            concept = concepts[n - 1]
        else:
            matches = [c for c in concepts if c.id == choice]
            if not matches:
                raise CommandRejectedError(f"unknown concept {choice!r}")
            concept = matches[0]
        s.concepts.select(concept.id, "user", None)
        s.stage = GATE_EXITS[Stage.AWAITING_CONCEPT_CHOICE]
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.AWAITING_CONCEPT_CHOICE,
                type=EventType.DECISION,
                summary=f"User selected concept '{concept.title}'.",
                payload={"concept_id": concept.id, "by": "user"},
            )
        ]

    return storage.runs.update(
        run_id, mutate, reason="concept chosen", expected_version=expected_version
    ).state


def approve_storyboard(
    storage: Storage, run_id: str, *, expected_version: int | None = None
) -> RunState:
    """Approve the keyframes at the storyboard gate; video generation (and spend) starts after."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.stage is not Stage.AWAITING_STORYBOARD_APPROVAL:
            raise CommandRejectedError(f"run is at {s.stage}, not waiting for storyboard approval")
        s.stage = GATE_EXITS[Stage.AWAITING_STORYBOARD_APPROVAL]
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.AWAITING_STORYBOARD_APPROVAL,
                type=EventType.DECISION,
                summary="User approved the storyboard keyframes; generating clips.",
            )
        ]

    return storage.runs.update(
        run_id, mutate, reason="storyboard approved", expected_version=expected_version
    ).state


def set_budget(
    storage: Storage, run_id: str, limit_usd: float, *, wallet_remaining_usd: float | None = None
) -> RunState:
    """Set a run's spend cap (e.g. raise it after planning ran with a near-zero wallet). For fal
    runs the caller passes the wallet's remaining headroom: the cap never exceeds what the run has
    already spent or reserved plus that headroom: a run never out-spends the prepaid balance."""
    if limit_usd <= 0:
        raise CommandRejectedError("the budget must be positive")

    def mutate(s: RunState) -> list[AgentEvent]:
        committed = s.budget.spent_usd + s.budget.reserved_usd
        new = limit_usd
        # Future fal spend can never exceed the wallet. Runs with a media cap carry the wallet
        # limit there (LLM calls are billed elsewhere); older runs clamp the total, as before.
        if wallet_remaining_usd is not None and s.budget.media_limit_usd is None:
            new = min(new, committed + wallet_remaining_usd)
        if new < committed:
            raise CommandRejectedError(f"run already committed ${committed:.3f}")
        old = s.budget.limit_usd
        s.budget.limit_usd = round(new, 6)
        if wallet_remaining_usd is not None and s.budget.media_limit_usd is not None:
            s.budget.media_limit_usd = round(
                s.budget.media_committed_usd + min(new, wallet_remaining_usd), 6
            )
        return [
            AgentEvent(
                run_id=s.id,
                stage=s.stage,
                type=EventType.DECISION,
                summary=f"Budget set by the user: ${old:.2f} → ${new:.2f}"
                + (
                    f" (wallet headroom ${wallet_remaining_usd:.2f})"
                    if wallet_remaining_usd
                    else ""
                ),
            )
        ]

    return storage.runs.update(run_id, mutate, reason="budget set").state


def edit_scene_spec(
    storage: Storage,
    run_id: str,
    scene_order: int,
    *,
    reason: str,
    description: str | None = None,
    shot: dict[str, object] | None = None,
    shows_product: bool | None = None,
    expected_version: int | None = None,
    vocab: tuple[str, bool, bool] | None = None,
) -> RunState:
    """Minimal scene edit (first real use: M11 S4 simplification; the full natural-language edit
    flow is M7). Changes the creative spec of ONE scene, bumps its version (archiving the old spec
    and prompt), marks it stale at prompt depth, and sends the run back to the ``prompt`` stage:
    the prompt node rewrites only this scene's prompt and requeues it. Other scenes are untouched.
    With no changes it is a pure re-prompt (e.g. after a prompt template upgrade).

    ``vocab`` = (entry name, shows_person, needs_product): switch a montage shot to another shot
    vocabulary entry; its flags come from the entry (ADR-0018)."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.storyboard is None or s.bible is None:
            raise CommandRejectedError("the run has no storyboard yet")
        matches = [sc for sc in s.storyboard.scenes if sc.order == scene_order]
        if not matches:
            raise CommandRejectedError(f"no scene {scene_order}")
        scene = matches[0]
        if scene.status is SceneStatus.GENERATING:
            raise CommandRejectedError(f"scene {scene_order} is generating; retry when it settles")
        unknown = set(shot or {}) - set(ShotSpec.model_fields)
        if unknown:
            raise CommandRejectedError(f"unknown shot fields: {sorted(unknown)}")
        new_shot = ShotSpec.model_validate({**scene.shot.model_dump(), **(shot or {})})
        scene.bump_version(f"edit: {reason}")
        scene.shot = new_shot
        if description is not None:
            scene.description = description
        if vocab is not None:
            scene.vocab, scene.shows_character, scene.shows_product = vocab
        if shows_product is not None:
            # An explicit user decision (e.g. "no bottle in the macro shot"). ADR-0010's
            # upgrade-only rule governs inferred flags; a user override is logged, and code may
            # still upgrade it back if the new text clearly shows the product.
            scene.shows_product = shows_product
        scene.mark_stale(StaleFrom.PROMPT)
        overrides = enforce_scene_flags([scene], s.bible)  # upgrade-only (ADR-0010)
        s.stage = Stage.PROMPT
        changed = (
            sorted((shot or {}).keys())
            + (["description"] if description else [])
            + ([f"shows_product={shows_product}"] if shows_product is not None else [])
            + ([f"vocab={vocab[0]}"] if vocab is not None else [])
        )
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.PROMPT,
                scene_id=scene.id,
                type=EventType.DECISION,
                summary=f"Scene {scene_order} edited ({reason}): "
                f"{', '.join(changed) or 're-prompt only'}; v{scene.version}, stale from prompt. "
                "Other scenes untouched.",
                payload={
                    "changed": list[JsonValue](changed),
                    "version": scene.version,
                    "flag_overrides": list[JsonValue]([o.flag for o in overrides]),
                },
            )
        ]

    return storage.runs.update(
        run_id, mutate, reason=f"scene {scene_order} edited", expected_version=expected_version
    ).state


def adopt_product_reference(
    storage: Storage, run_id: str, artifact: Artifact, verdict: QAVerdict, *, reason: str
) -> RunState:
    """Make a stored packshot the run's locked product reference after it passed the CURRENT
    gate (e.g. re-judged after a gate recalibration, so no new image is paid for). Only a
    packshot this run generated and recorded may be adopted, and only with a passing verdict."""
    if not verdict.passed:
        raise CommandRejectedError("only a packshot that passes the gate can be adopted")

    def mutate(s: RunState) -> list[AgentEvent]:
        pr = s.product_reference
        if pr is None or all(a.sha256 != artifact.sha256 for a in pr.rejected):
            raise CommandRejectedError("not a packshot recorded by this run")
        s.product_reference = pr.model_copy(update={"artifact": artifact, "verdict": verdict})
        return [
            AgentEvent(
                run_id=s.id,
                stage=s.stage,
                type=EventType.DECISION,
                summary=f"Product reference adopted ({reason}): gate score {verdict.score:.2f}",
                payload={"sha256": artifact.sha256, "score": verdict.score},
            )
        ]

    return storage.runs.update(run_id, mutate, reason="product reference adopted").state


def patch_bible(storage: Storage, run_id: str, changes: dict[str, str], *, reason: str) -> RunState:
    """Explicit bible patch (dotted paths, validated like an edit's bible changes) for a run that is
    already mid-edit. Archives the old bible; scenes that use a changed section go stale at prompt
    depth (upgrade-only: scenes already stale deeper stay so). The product reference stays valid
    unless the product section changed."""
    from adsmith.agent.edit.blast import apply_bible_changes  # noqa: PLC0415

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.bible is None or s.storyboard is None:
            raise CommandRejectedError("the run has no bible yet")
        try:
            new = apply_bible_changes(s.bible, changes)
        except ValueError as exc:
            raise CommandRejectedError(str(exc)) from exc
        sections = {p.partition(".")[0] for p in changes}
        touched = []
        for sc in s.storyboard.scenes:
            uses = {str(x) for x in sc.uses_bible_sections()}
            if sections & uses and sc.status is not SceneStatus.STALE:
                sc.bump_version(f"bible: {reason}")
                sc.mark_stale(StaleFrom.PROMPT)
                touched.append(sc.order)
        s.bible_history.append(s.bible)
        s.bible = new
        s.stage = Stage.PROMPT
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.PROMPT,
                type=EventType.DECISION,
                summary=f"Bible v{new.version} ({reason}): {', '.join(sorted(changes))}; "
                f"newly stale scenes {touched or 'none (already stale)'}",
                payload={
                    "changes": dict[str, JsonValue](changes),
                    "touched": list[JsonValue](touched),
                },
            )
        ]

    return storage.runs.update(run_id, mutate, reason="bible patched").state


def correct_scene_flags(
    storage: Storage,
    run_id: str,
    scene_order: int,
    *,
    reason: str,
    shows_character: bool | None = None,
    shows_product: bool | None = None,
) -> RunState:
    """Human correction of a false-positive scene flag (e.g. a mist macro flagged as showing the
    character) WITHOUT regenerating anything: the flag only decides which future bible changes
    reach the scene. ADR-0010 keeps inferred flags upgrade-only; a human may downgrade, logged."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.storyboard is None:
            raise CommandRejectedError("the run has no storyboard")
        matches = [sc for sc in s.storyboard.scenes if sc.order == scene_order]
        if not matches:
            raise CommandRejectedError(f"no scene {scene_order}")
        sc = matches[0]
        changed: list[str] = []
        if shows_character is not None and sc.shows_character != shows_character:
            sc.shows_character = shows_character
            changed.append(f"shows_character={shows_character}")
        if shows_product is not None and sc.shows_product != shows_product:
            sc.shows_product = shows_product
            changed.append(f"shows_product={shows_product}")
        sc.check_invariants()
        return [
            AgentEvent(
                run_id=s.id,
                stage=s.stage,
                scene_id=sc.id,
                type=EventType.DECISION,
                summary=f"HUMAN: scene {scene_order} flags corrected "
                f"({', '.join(changed) or 'no change'}): {reason}. Nothing regenerated.",
                payload={"changed": list[JsonValue](changed), "human": True},
            )
        ]

    return storage.runs.update(run_id, mutate, reason=f"scene {scene_order} flags corrected").state


def accept_scene(
    storage: Storage,
    run_id: str,
    scene_order: int,
    *,
    reason: str,
    verdict: QAVerdict | None = None,
) -> RunState:
    """Human override: accept the last clip of a QA-rejected scene. Logged as a HUMAN decision in
    the trace, with the critic's failing verdict kept as the record. Reassemble afterwards."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.storyboard is None:
            raise CommandRejectedError("the run has no storyboard")
        matches = [sc for sc in s.storyboard.scenes if sc.order == scene_order]
        if not matches:
            raise CommandRejectedError(f"no scene {scene_order}")
        sc = matches[0]
        clip = next(
            (
                a.artifact
                for a in reversed(sc.attempts)
                if a.scene_version == sc.version and a.phase.value == "clip" and a.artifact
            ),
            None,
        )
        v = verdict or sc.qa
        if clip is None or v is None:
            raise CommandRejectedError(f"scene {scene_order} has no rejected clip with a verdict")
        try:
            sc.accept_by_human(clip, v, reason)
        except ValueError as exc:  # IllegalTransitionError
            raise CommandRejectedError(str(exc)) from exc
        return [
            AgentEvent(
                run_id=s.id,
                stage=s.stage,
                scene_id=sc.id,
                type=EventType.DECISION,
                summary=f"HUMAN OVERRIDE: scene {scene_order} clip accepted despite QA "
                f"{v.score:.2f} ({'; '.join(v.fail_reasons) or 'rejected'}). Reason: {reason}",
                payload={
                    "human_override": True,
                    "critic_score": v.score,
                    "fail_reasons": list[JsonValue](v.fail_reasons),
                    "reason": reason,
                },
            )
        ]

    return storage.runs.update(run_id, mutate, reason=f"scene {scene_order} accepted").state


def reassemble(
    storage: Storage,
    run_id: str,
    *,
    partial_mode: str | None = None,
    music: Artifact | None = None,
    music_from: str = "",
) -> RunState:
    """Re-run only the assembly of a finished run (new end card / partial mode / a stored music
    track), no generation: ffmpeg plus at most one small LLM call for the end-card copy.
    ``music`` is an audio artifact already in the CAS (e.g. another run's generated track): it
    becomes this run's track, so the montage is beat-cut to it and nothing is generated."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.stage not in {Stage.DONE, Stage.DONE_PARTIAL}:
            raise CommandRejectedError(f"run is at {s.stage}; reassemble needs a finished run")
        note = ""
        if music is not None:
            if music.kind != "audio" or not storage.artifacts.resolve(music).is_file():
                raise CommandRejectedError("music must be an audio artifact stored in the CAS")
            s.music = music
            note = f" with the stored music track {music.sha256[:10]}" + (
                f" (from {music_from})" if music_from else ""
            )
        if partial_mode is not None:
            if partial_mode not in {"skip", "slate"}:
                raise CommandRejectedError("partial_mode must be 'skip' or 'slate'")
            s.config = s.config.model_copy(update={"partial_mode": partial_mode})
        s.stage = Stage.ASSEMBLE
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.ASSEMBLE,
                type=EventType.DECISION,
                summary=f"Re-assembling (partial mode: {s.config.partial_mode}){note}.",
            )
        ]

    return storage.runs.update(run_id, mutate, reason="reassemble").state


def retry_scene(storage: Storage, run_id: str, scene_order: int) -> RunState:
    """Re-arm one failed scene (e.g. after a top-up or a transient outage) with a fresh attempt
    budget. It resumes from the deepest still-valid artifact: an approved keyframe is kept."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.storyboard is None:
            raise CommandRejectedError("the run has no storyboard yet")
        if s.stage not in {Stage.DONE, Stage.DONE_PARTIAL, Stage.ERRORED}:
            raise CommandRejectedError(f"run is at {s.stage}; retry applies to a finished run")
        matches = [sc for sc in s.storyboard.scenes if sc.order == scene_order]
        if not matches or matches[0].status is not SceneStatus.FAILED:
            raise CommandRejectedError(f"scene {scene_order} has not failed")
        sc = matches[0]
        if sc.prompt is None:
            depth = StaleFrom.PROMPT
        elif sc.keyframe_approved:
            depth = StaleFrom.CLIP
        else:
            depth = StaleFrom.KEYFRAME
        sc.bump_version("retry after failure")  # fresh per-version attempt and QA budgets
        sc.mark_stale(depth)
        if sc.prompt is not None:
            sc.requeue()
        s.error = None
        s.stage = Stage.PROMPT if sc.prompt is None else Stage.GENERATE
        return [
            AgentEvent(
                run_id=s.id,
                stage=s.stage,
                scene_id=sc.id,
                type=EventType.DECISION,
                summary=f"Retrying scene {scene_order} from {depth.value} (v{sc.version}).",
            )
        ]

    return storage.runs.update(run_id, mutate, reason=f"retry scene {scene_order}").state


# ------------------------------------------------------------------------ whole-ad generations
def approve_generation(
    storage: Storage, run_id: str, *, expected_version: int | None = None
) -> RunState:
    """The estimate gate (ADR-0023): approve spending exactly the shown estimate on ONE
    generation. Nothing is spent before this command."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.stage is not Stage.AWAITING_GENERATION_APPROVAL or s.generation is None:
            raise CommandRejectedError(f"run is at {s.stage}, not waiting for a spend approval")
        g = s.generation
        g.approve("user")
        s.stage = GATE_EXITS[Stage.AWAITING_GENERATION_APPROVAL]
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.AWAITING_GENERATION_APPROVAL,
                type=EventType.DECISION,
                summary=f"HUMAN: approved ${g.approved_usd or 0:.3f} for one generation of the "
                f"whole ad ({g.model}, {g.duration_s}s {g.resolution}).",
                payload={"approved_usd": g.approved_usd, "human": True},
            )
        ]

    return storage.runs.update(
        run_id, mutate, reason="generation approved", expected_version=expected_version
    ).state


def decline_generation(storage: Storage, run_id: str, *, reason: str = "declined") -> RunState:
    """The estimate gate: decline. Nothing is spent; the run ends partial and can be renewed."""

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.stage is not Stage.AWAITING_GENERATION_APPROVAL or s.generation is None:
            raise CommandRejectedError(f"run is at {s.stage}, not waiting for a spend approval")
        s.generation.fail(
            SceneFailure(
                kind=FailureKind.BUDGET_EXCEEDED,
                reason_user="Declined at the estimate gate: nothing was spent.",
                reason_debug=reason[:4000],
            )
        )
        s.stage = Stage.DONE_PARTIAL
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.AWAITING_GENERATION_APPROVAL,
                type=EventType.DECISION,
                summary=f"HUMAN: declined the estimate ({reason}); nothing was spent.",
                payload={"human": True},
            )
        ]

    return storage.runs.update(run_id, mutate, reason="generation declined").state


def accept_ad(storage: Storage, run_id: str, *, reason: str) -> RunState:
    """Human override: accept a whole-ad generation the critic flagged (the failing verdict stays
    as the record). The ad was already assembled, so the run is simply done."""

    def mutate(s: RunState) -> list[AgentEvent]:
        g = s.generation
        if g is None or g.status != "flagged":
            raise CommandRejectedError("there is no flagged whole-ad generation to accept")
        g.accept(reason)
        if s.final is not None and s.stage is Stage.DONE_PARTIAL:
            s.stage = Stage.DONE
        score = g.qa.score if g.qa else 0.0
        return [
            AgentEvent(
                run_id=s.id,
                stage=s.stage,
                type=EventType.DECISION,
                summary=f"HUMAN OVERRIDE: whole ad accepted despite the critic ({score:.2f}). "
                f"Reason: {reason}",
                payload={"human_override": True, "critic_score": score, "reason": reason},
            )
        ]

    return storage.runs.update(run_id, mutate, reason="ad accepted").state


def regenerate_ad(storage: Storage, run_id: str, *, reason: str) -> RunState:
    """A human asks for a NEW generation of the whole ad (same prompt, new version, so a new
    request): it goes back to the estimate, and real money waits at the gate again."""

    def mutate(s: RunState) -> list[AgentEvent]:
        g = s.generation
        if g is None:
            raise CommandRejectedError("this run has no whole-ad generation")
        if s.stage not in {Stage.DONE, Stage.DONE_PARTIAL, Stage.ERRORED}:
            raise CommandRejectedError(f"run is at {s.stage}; regenerate a finished run")
        old = g.version
        g.renew(f"regenerate: {reason}")
        s.final, s.error = None, None
        s.stage = Stage.ESTIMATE
        return [
            AgentEvent(
                run_id=s.id,
                stage=Stage.ESTIMATE,
                type=EventType.DECISION,
                summary=f"HUMAN: new generation of the whole ad (v{old} → v{g.version}): "
                f"{reason}. It is estimated first and waits for approval.",
                payload={"human": True, "version": g.version},
            )
        ]

    return storage.runs.update(run_id, mutate, reason="ad regenerate").state


def replace_generation_prompt(
    storage: Storage,
    run_id: str,
    text: str,
    *,
    max_chars: int,
    source: str = "",
    endcard_tagline: str | None = None,
) -> RunState:
    """Human creative direction at the spend gate: replace the model-written prompt with a
    human one (sent verbatim). The run goes back to ESTIMATE so the new prompt is priced again
    and waits at the gate; reservation, critic and assembly are unchanged. Optionally sets the
    end card's tagline (the brand stays as the brief or the end-card writer gives it)."""
    text = text.strip()

    def mutate(s: RunState) -> list[AgentEvent]:
        g = s.generation
        if s.stage is not Stage.AWAITING_GENERATION_APPROVAL or g is None:
            raise CommandRejectedError(f"run is at {s.stage}, not waiting for a spend approval")
        problems = human_prompt_problems(text, duration_s=g.duration_s, max_chars=max_chars)
        if problems:
            raise CommandRejectedError("the prompt was not accepted: " + "; ".join(problems))
        old_text, old_reserve = g.prompt_text, g.est_usd
        diff = prompt_diff_summary(old_text, text)
        g.replace_prompt(HumanPrompt(text=text, shots=parse_prompt_shots(text), source=source))
        s.stage = Stage.ESTIMATE
        events = [
            AgentEvent(
                run_id=s.id,
                stage=Stage.AWAITING_GENERATION_APPROVAL,
                type=EventType.DECISION,
                summary=f"HUMAN creative direction: prompt replaced ({diff}). It is priced "
                "again and waits at the gate.",
                payload={
                    "human": True, "diff": diff, "old_prompt": old_text, "new_prompt": text,
                    "previous_reserved_usd": old_reserve, "source": source,
                },
            )
        ]  # fmt: skip
        events += _endcard_direction(s, brand=None, tagline=endcard_tagline)
        return events

    return storage.runs.update(run_id, mutate, reason="human prompt").state


def _endcard_direction(s: RunState, *, brand: str | None, tagline: str | None) -> list[AgentEvent]:
    """Human end-card copy goes into the brief (it is the client's own, not a placeholder); the
    end card is re-made at assembly from it."""
    if s.brief is None:
        raise CommandRejectedError("the run has no brief yet")
    events: list[AgentEvent] = []
    for field, value in (("brand_name", brand), ("tagline", tagline)):
        if value is None:
            continue
        text = " ".join(value.split())
        if not text:
            raise CommandRejectedError(f"the end-card {field} is empty")
        update: dict[str, object] = {field: text}
        if field == "tagline":
            arabic = any("\u0600" <= ch <= "\u06ff" for ch in text)
            update["tagline_language"] = Language.AR if arabic else Language.EN
        old = getattr(s.brief, field)
        s.brief = s.brief.model_copy(update=update)
        s.brief = s.brief.model_validate(s.brief.model_dump())  # length limits
        label = "brand name" if field == "brand_name" else "tagline"
        events.append(
            AgentEvent(
                run_id=s.id,
                stage=s.stage,
                type=EventType.DECISION,
                summary=f"HUMAN creative direction: end-card {label} set to «{text}»",
                payload={"human": True, field: text, "previous": old},
            )
        )
    if events:
        s.endcard = None  # re-made at assembly from the brief
    return events


def set_endcard(
    storage: Storage, run_id: str, *, brand: str | None = None, tagline: str | None = None
) -> RunState:
    """Human end-card copy (brand name and/or tagline) before the ad is assembled."""
    if brand is None and tagline is None:
        raise CommandRejectedError("give a brand name or a tagline")

    def mutate(s: RunState) -> list[AgentEvent]:
        if s.final is not None or s.stage in {Stage.DONE, Stage.DONE_PARTIAL, Stage.ERRORED}:
            raise CommandRejectedError(f"run is at {s.stage}: the end card is already made")
        return _endcard_direction(s, brand=brand, tagline=tagline)

    return storage.runs.update(run_id, mutate, reason="human end card").state

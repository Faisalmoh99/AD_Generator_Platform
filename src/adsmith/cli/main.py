"""Command-line interface (Typer + Rich).

Every command that makes the agent work shows a live trace tailed from the event store (stages,
scene progress, retries, rewrites, QA verdicts, cache hits, cost), then a summary of the run.
``adsmith demo`` is the zero-key showcase (see ``cli/demo.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from adsmith import __version__
from adsmith.agent import commands
from adsmith.agent.context import AgentRuntime, ad_endpoint
from adsmith.agent.orchestrator import Orchestrator
from adsmith.cli.live import LiveTrace
from adsmith.config import Settings
from adsmith.domain.enums import SceneStatus, Stage
from adsmith.domain.events import EventType
from adsmith.domain.formats import AdFormat
from adsmith.domain.qa import AdVerdict
from adsmith.domain.run import RunState
from adsmith.logging import configure_logging

app = typer.Typer(help="Adsmith: marketing-ad agent.", no_args_is_help=True)
console = Console()

_STATUS_STYLE = {
    SceneStatus.APPROVED: "green",
    SceneStatus.FAILED: "red",
    SceneStatus.STALE: "yellow",
    SceneStatus.QA_REVIEW: "cyan",
    SceneStatus.GENERATING: "cyan",
    SceneStatus.PENDING: "white",
}


def _orchestrator(settings: Settings | None = None) -> Orchestrator:
    s = settings or Settings()
    configure_logging("WARNING", s.log_format)  # the live trace is the user-facing output
    return Orchestrator(AgentRuntime.from_settings(s))


def follow(orch: Orchestrator, run_id: str, work: Awaitable[RunState]) -> RunState:
    """Run ``work`` with the live trace for ``run_id``."""
    return asyncio.run(LiveTrace(console, orch.rt.storage).follow(run_id, work))


def print_state(orch: Orchestrator, state: RunState) -> None:
    console.rule(f"[bold]{state.id}[/]  stage=[cyan]{state.stage}[/]  v{state.state_version}")
    if state.brief:
        b = state.brief
        console.print(
            f"[bold]Brief[/] {b.product} · {b.target_audience} · {b.duration_s:g}s "
            f"{b.aspect_ratio} · {b.language.value}/{b.dialect.value}"
        )
    if state.concepts:
        table = Table("#", "Concept", "Logline", show_lines=True, expand=True)
        for i, c in enumerate(state.concepts.concepts, start=1):
            mark = " ✓" if c.id == state.concepts.selected_id else ""
            table.add_row(f"{i}{mark}", c.title, c.logline)
        console.print(table)
        if state.concepts.selection_rationale:
            console.print(f"[dim]Selection: {state.concepts.selection_rationale}[/]")
    if state.storyboard:
        table = Table("Scene", "Status", "Attempts", "Spend", "Note")
        for sc in state.storyboard.scenes:
            note = sc.failure.reason_user if sc.failure else ""
            if sc.qa is not None and sc.status is SceneStatus.APPROVED:
                note = f"QA {sc.qa.score:.2f}"
            spend = sum(a.cost_usd for a in sc.attempts)
            style = _STATUS_STYLE.get(sc.status, "white")
            table.add_row(
                str(sc.order),
                f"[{style}]{sc.status.value}[/]",
                str(len(sc.attempts)),
                f"${spend:.2f}",
                note,
            )
        console.print(table)
    _print_generation(state)
    if state.endcard:
        flag = "  [yellow](placeholder)[/]" if state.endcard.is_placeholder else ""
        console.print(
            f"[bold]End card[/] {state.endcard.brand_name} — {state.endcard.tagline}{flag}"
        )
    if state.final:
        name = state.generation.final_name if state.generation else "final.mp4"
        path = orch.rt.storage.artifacts.run_dir(state.id) / name
        console.print(f"[bold green]Final ad:[/] {path} ({state.final.duration_s:g}s)")
    if state.error:
        console.print(
            f"[red]Error:[/] {state.error.reason_user}  ([bold]adsmith resume {state.id}[/])"
        )
    led = state.budget
    by = ", ".join(f"{k} ${v:.2f}" for k, v in sorted(led.by_category.items()))
    console.print(f"[dim]Spent ${led.spent_usd:.3f} of ${led.limit_usd:g} ({by})[/]")
    nxt = {
        Stage.AWAITING_CONCEPT_CHOICE: f"adsmith choose {state.id} <1-3>",
        Stage.AWAITING_STORYBOARD_APPROVAL: f"adsmith approve {state.id}",
        Stage.AWAITING_GENERATION_APPROVAL: f"adsmith approve {state.id}   (or: adsmith decline "
        f"{state.id})",
    }.get(state.stage)
    if nxt:
        console.print(f"Next: [bold]{nxt}[/]")


def reference_lines(state: RunState) -> list[str]:
    """The images the video model receives, in order (Image 1 is always the product photo)."""
    if state.product_photo is None:
        return []
    out = [f"Image 1: product photo {state.product_photo.path}"]
    for i, r in enumerate(state.reference_images, start=2):
        extra = f", ${r.cost_usd:.3f}, check {r.check}" if r.source == "generated" else ""
        out.append(f"Image {i}: {r.name} ({r.source}{extra}) {r.artifact.path}")
    return out


def _print_generation(state: RunState) -> None:
    g = state.generation
    if g is None:
        return
    est = f"${g.est_usd:.3f}" if g.est_usd is not None else "not estimated"
    if g.est_usd is not None and g.computed_usd is not None and g.computed_usd != g.est_usd:
        est = f"price ${g.computed_usd:.3f}, reserve ${g.est_usd:.3f} (with margin)"
    appr = f", approved by {g.approved_by}" if g.approved_by else ""
    console.print(
        f"[bold]Whole ad[/] v{g.version} [cyan]{g.status}[/] · {g.model} · {g.duration_s}s "
        f"{g.resolution} {g.aspect_ratio} · audio {'on' if g.generate_audio else 'off'} · "
        f"estimate {est}{appr}"
    )
    if state.stage is Stage.AWAITING_GENERATION_APPROVAL:
        if g.price_basis:
            console.print(f"[dim]Price: {g.price_basis}[/]")
        for line in reference_lines(state):
            console.print(f"[dim]{line}[/]")
        if state.director_review is not None:
            r = state.director_review
            failed = ", ".join(r.failed) or "none"
            console.print(
                f"[dim]Creative director: {r.outcome} (rules failed in round 1: {failed})[/]"
            )
        who = " (written by a human creative director)" if g.human_prompt else ""
        console.print(f"[bold]Prompt sent to the model{who}:[/]\n{g.prompt_text}")
    if g.qa is not None:
        why = "; ".join(g.qa.fail_reasons) or "hard gate passed"
        console.print(f"[dim]Critic: {g.qa.score:.2f} ({why})[/]")
        if isinstance(g.qa, AdVerdict):
            flag = "" if g.qa.creative_pass else "  [yellow]WARN[/]"
            scores = ", ".join(f"{k} {v}" for k, v in g.qa.rubric.model_dump().items())
            console.print(f"[dim]Creative: {g.qa.creative_score:.2f}/10 ({scores})[/]{flag}")
    if g.failure is not None:
        console.print(f"[red]Whole ad failed:[/] {g.failure.reason_user}")


def run_edit(orch: Orchestrator, run_id: str, instruction: str, resolution: str | None) -> None:
    """Apply one edit with the live trace and print the blast-radius table."""
    from adsmith.agent.edit.service import EditService  # noqa: PLC0415

    store = orch.rt.storage
    before = store.runs.load(run_id)
    spent0 = before.budget.spent_usd
    trace = LiveTrace(console, store)
    seq0 = trace.last_seq(run_id)
    console.rule(f"[bold magenta]Edit[/] {instruction}")
    outcome = asyncio.run(
        trace.follow(
            run_id,
            EditService(orch.rt).edit(run_id, instruction, resolution=resolution),  # type: ignore[arg-type]
        )
    )
    if outcome.status != "applied":
        console.print(f"[yellow]{outcome.status}:[/] {outcome.message}")
        return
    assert outcome.blast is not None and before.storyboard is not None
    console.print(f"resolution: [bold]{outcome.resolution}[/]  {outcome.message}")
    final = asyncio.run(trace.follow(run_id, orch.run_until_blocked(run_id)))
    events = store.events.tail(run_id, after_seq=seq0, limit=100_000)
    why = {ln.split(" ", 2)[1]: ln for ln in outcome.blast.explanation if ln.startswith("scene")}
    table = Table(
        "Scene", "Blast radius", "Provider calls", "Cache hits", "Why", title="Blast radius"
    )
    for s in before.storyboard.scenes:
        depth = outcome.blast.affected.get(s.id)
        mine = [e for e in events if e.scene_id == s.id]
        calls = sum(1 for e in mine if e.type is EventType.ATTEMPT_STARTED)
        hits = sum(1 for e in mine if e.type is EventType.CACHE_HIT)
        reason = why.get(str(s.order), "").split(": ", 1)[-1][:70] if depth else "$0"
        label = f"[bold red]{depth.value}[/]" if depth else "[green]untouched[/]"
        table.add_row(str(s.order), label, str(calls), str(hits), reason)
    console.print(table)
    if outcome.blast.bible_bump and final.bible is not None:
        console.print(f"bible bumped to v{final.bible.version}")
    console.print(f"edit cost ${final.budget.spent_usd - spent0:.3f} (incl. LLM) · {final.stage}")
    if final.final is not None:
        console.print(f"[bold green]New final:[/] {store.artifacts.run_dir(run_id) / 'final.mp4'}")


# ------------------------------------------------------------------------------ commands
@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def run(
    request: Annotated[str, typer.Argument(help="The ad request (English or Arabic).")],
    auto: Annotated[bool, typer.Option("--auto", help="Let the agent choose the concept.")] = False,
    until: Annotated[
        Stage | None, typer.Option(help="Stop before this stage (e.g. keyframe for planning only).")
    ] = None,
    chaos: Annotated[
        bool | None, typer.Option("--chaos/--no-chaos", help="Inject faults to demo recovery.")
    ] = None,
    hero: Annotated[
        bool, typer.Option("--hero", help="Best-of-2 clips per scene from the approved keyframe.")
    ] = False,
    product_ref: Annotated[
        bool | None,
        typer.Option(
            "--product-ref/--no-product-ref",
            help="Lock one gated product still and condition product keyframes on it.",
        ),
    ] = None,
    product_photo: Annotated[
        Path | None,
        typer.Option(
            "--product-photo",
            help="A real product photo: the ad is made in ONE multi-shot generation from it.",
        ),
    ] = None,
    ref_image: Annotated[
        list[Path] | None,
        typer.Option(
            "--ref-image",
            help="A prop reference image (repeatable, order kept): sent after the product photo "
            "(Image 2, 3, ...) and checked by the critic as a prop, never as the product.",
        ),
    ] = None,
    ad_format: Annotated[
        AdFormat | None, typer.Option("--format", help="Force a format instead of resolving it.")
    ] = None,
) -> None:
    """Start a new run."""
    orch = _orchestrator()
    try:
        state = orch.create_run(
            request, interactive=not auto, chaos=chaos, hero=hero, product_reference=product_ref,
            product_photo=product_photo, ref_images=ref_image or [], ad_format=ad_format,
        )  # fmt: skip
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    console.print(f"Run [bold]{state.id}[/] created.")
    print_state(orch, follow(orch, state.id, orch.run_until_blocked(state.id, stop_before=until)))


@app.command()
def resume(
    run_id: str,
    until: Annotated[Stage | None, typer.Option()] = None,
    from_version: Annotated[
        int | None, typer.Option(help="Fork a new run from this past snapshot version.")
    ] = None,
) -> None:
    """Resume a run after a failure, a crash, or a gate (or fork it from a past version)."""
    orch = _orchestrator()
    if from_version is not None:
        run_id = orch.rt.storage.runs.fork(run_id, from_version).state.id
        console.print(f"Forked into [bold]{run_id}[/] from version {from_version}.")
    print_state(orch, follow(orch, run_id, orch.resume(run_id, stop_before=until)))


@app.command()
def budget(
    run_id: str, usd: Annotated[float, typer.Argument(help="New spend cap for this run (USD).")]
) -> None:
    """Set a run's spend cap. With real media it is clamped to the prepaid wallets' headroom."""
    orch = _orchestrator()
    state = orch.rt.storage.runs.load(run_id)
    headroom = orch.rt.media_remaining_usd(state.config)
    state = commands.set_budget(orch.rt.storage, run_id, usd, wallet_remaining_usd=headroom)
    console.print(
        f"Run [bold]{run_id}[/] budget: ${state.budget.limit_usd:.2f} "
        f"(spent ${state.budget.spent_usd:.3f})"
    )


@app.command()
def accept(
    run_id: str,
    scene: Annotated[int, typer.Argument(help="Scene number")],
    reason: Annotated[str, typer.Option(help="Why the human accepts the rejected clip.")],
) -> None:
    """Human override: accept a QA-rejected clip (logged in the trace), then reassemble."""
    orch = _orchestrator()
    commands.accept_scene(orch.rt.storage, run_id, scene, reason=reason)
    commands.reassemble(orch.rt.storage, run_id)
    print_state(orch, follow(orch, run_id, orch.run_until_blocked(run_id)))


@app.command()
def approve(
    run_id: str,
    prompt_file: Annotated[
        Path | None,
        typer.Option(
            help="Spend gate only: replace the model's prompt with this human-written "
            "one (sent verbatim); it is priced again before anything is approved."
        ),
    ] = None,
    no_approve: Annotated[
        bool,
        typer.Option("--no-approve", help="With --prompt-file: re-price and stop at the gate."),
    ] = False,
    endcard_tagline: Annotated[
        str | None, typer.Option(help="The end card's tagline (before assembly).")
    ] = None,
    endcard_brand: Annotated[
        str | None, typer.Option(help="The end card's brand name (before assembly).")
    ] = None,
) -> None:
    """Approve at the current gate: the storyboard keyframes, or the estimate of a whole-ad
    generation (real money is spent only after this)."""
    orch = _orchestrator()
    state = orch.rt.storage.runs.load(run_id)
    at_spend = state.stage is Stage.AWAITING_GENERATION_APPROVAL
    endcard = endcard_brand is not None or endcard_tagline is not None
    if no_approve and prompt_file is None and not endcard:
        raise typer.BadParameter("--no-approve needs --prompt-file or an --endcard-* option")
    if endcard and prompt_file is None:
        state = commands.set_endcard(
            orch.rt.storage, run_id, brand=endcard_brand, tagline=endcard_tagline
        )
        if no_approve:
            print_state(orch, state)
            return
    if prompt_file is not None:
        if not at_spend or state.generation is None:
            raise typer.BadParameter(
                f"--prompt-file is for the spend gate; run is at {state.stage}"
            )
        shown = state.generation.est_usd
        ep = ad_endpoint(orch.rt.settings, state.config.ad_model)
        commands.replace_generation_prompt(
            orch.rt.storage, run_id, prompt_file.read_text(encoding="utf-8"),
            max_chars=ep.max_prompt_chars, source=str(prompt_file),
            endcard_tagline=endcard_tagline,
        )  # fmt: skip
        if endcard_brand is not None:
            commands.set_endcard(orch.rt.storage, run_id, brand=endcard_brand)
        state = follow(orch, run_id, orch.run_until_blocked(run_id))
        g = state.generation
        if no_approve or state.stage is not Stage.AWAITING_GENERATION_APPROVAL or g is None:
            print_state(orch, state)
            return
        if shown is not None and (g.est_usd or 0) > shown + 1e-9:
            print_state(orch, state)
            console.print(
                f"[yellow]The new prompt's reservation ${g.est_usd or 0:.3f} is above the "
                f"${shown:.3f} you saw: not approved. Review it, then run "
                f"`adsmith approve {run_id}`.[/]"
            )
            return
    if at_spend:
        commands.approve_generation(orch.rt.storage, run_id)
    else:
        commands.approve_storyboard(orch.rt.storage, run_id)
    print_state(orch, follow(orch, run_id, orch.run_until_blocked(run_id)))


@app.command()
def decline(
    run_id: str, reason: Annotated[str, typer.Option(help="Why")] = "declined at the gate"
) -> None:
    """Decline the estimate of a whole-ad generation: nothing is spent."""
    orch = _orchestrator()
    print_state(orch, commands.decline_generation(orch.rt.storage, run_id, reason=reason))


@app.command("settle-actual")
def settle_actual(
    run_id: str,
    usd: Annotated[float, typer.Option(help="The charge the provider's console shows")],
    version: Annotated[
        int | None, typer.Option(help="Generation version (default: latest)")
    ] = None,
) -> None:
    """Book the real (console) charge of a whole-ad generation instead of the computed price."""
    orch = _orchestrator()
    state = commands.settle_actual(
        orch.rt.storage, orch.rt.hf_account(), run_id, usd=usd, version=version
    )
    s = orch.rt.hf_account().summary()
    console.print(
        f"Booked ${usd:.3f}. Higgsfield ledger: settled ${s.settled_usd:.3f}, reserved "
        f"${s.reserved_usd:.3f}, headroom ${s.remaining_usd:.3f} of ${s.balance_usd:.2f}."
    )
    print_state(orch, state)


def _span(text: str) -> tuple[float, float]:
    a, sep, b = text.partition("-")
    if not sep:
        raise typer.BadParameter(f"{text!r}: use START-END in seconds, e.g. 3.875-4.375")
    return float(a), float(b)


@app.command()
def retime(
    run_id: str,
    window: Annotated[str, typer.Option(help="Seconds to slow, START-END (generation time)")],
    trim: Annotated[str, typer.Option(help="Seconds to remove to keep the length, START-END")],
    factor: Annotated[float, typer.Option(help="Slow-down factor")] = 2.5,
    method: Annotated[str, typer.Option(help="mci (motion interpolation) or blend")] = "mci",
    output: Annotated[str, typer.Option(help="File name next to final.mp4")] = "final_v2.mp4",
    crossfade: Annotated[
        int, typer.Option(help="Dissolve across the trim's join over N frames (even, 0 = hard)")
    ] = 0,
) -> None:
    """Human edit on a finished whole ad: slow a window, trim a later stretch, re-assemble."""
    from adsmith.domain.ad import RetimeEdit  # noqa: PLC0415
    from adsmith.media import ffmpeg  # noqa: PLC0415

    orch = _orchestrator()
    g = orch.rt.storage.runs.load(run_id).generation
    if g is None or g.artifact is None:
        raise typer.BadParameter("this run has no generated whole ad")
    src = orch.rt.storage.artifacts.resolve(g.artifact)
    fps = asyncio.run(ffmpeg.frame_rate(src))
    frames = asyncio.run(ffmpeg.frame_count(src))
    (ws, we), (ts, te) = _span(window), _span(trim)
    if method not in {"mci", "blend"}:
        raise typer.BadParameter("--method is mci or blend")
    edit = RetimeEdit(
        window_start_s=ws, window_end_s=we, factor=factor, trim_start_s=ts, trim_end_s=te,
        method="mci" if method == "mci" else "blend", crossfade_frames=crossfade,
    )  # fmt: skip
    try:
        commands.retime_ad(orch.rt.storage, run_id, [edit], fps=fps, frames=frames, output=output)
    except commands.CommandRejectedError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    print_state(orch, follow(orch, run_id, orch.run_until_blocked(run_id)))


@app.command("accept-ad")
def accept_ad(run_id: str, reason: Annotated[str, typer.Option(help="Why you accept it")]) -> None:
    """Human override: accept a whole-ad generation the critic flagged (logged)."""
    orch = _orchestrator()
    print_state(orch, commands.accept_ad(orch.rt.storage, run_id, reason=reason))


@app.command("regenerate-ad")
def regenerate_ad(
    run_id: str, reason: Annotated[str, typer.Option(help="Why a new generation")]
) -> None:
    """A new generation of the whole ad: estimated first, then it waits at the spend gate."""
    orch = _orchestrator()
    commands.regenerate_ad(orch.rt.storage, run_id, reason=reason)
    print_state(orch, follow(orch, run_id, orch.run_until_blocked(run_id)))


@app.command()
def choose(
    run_id: str, concept: Annotated[str, typer.Argument(help="1-3 or a concept id")]
) -> None:
    """Choose a concept at the concept gate and continue."""
    orch = _orchestrator()
    commands.choose_concept(orch.rt.storage, run_id, concept)
    print_state(orch, follow(orch, run_id, orch.run_until_blocked(run_id)))


@app.command()
def edit(
    run_id: str,
    instruction: Annotated[str, typer.Argument(help="The edit, in English or Arabic.")],
    resolution: Annotated[
        str | None,
        typer.Option(help="Override: apply_local | propagate_neighbors | update_bible | reject"),
    ] = None,
) -> None:
    """Edit a finished ad in natural language; only the blast radius is regenerated."""
    run_edit(_orchestrator(), run_id, instruction, resolution)


@app.command()
def retry(run_id: str, scene: Annotated[int, typer.Argument(help="Scene number")]) -> None:
    """Re-arm a failed scene (fresh attempt budget) and finish the run."""
    orch = _orchestrator()
    st = commands.retry_scene(orch.rt.storage, run_id, scene)
    assert st.storyboard is not None
    console.print(f"Retrying scene {scene} (now v{st.storyboard.scenes[scene - 1].version}).")
    print_state(orch, follow(orch, run_id, orch.run_until_blocked(run_id)))


@app.command()
def assemble(
    run_id: str,
    partial: Annotated[str | None, typer.Option(help="skip | slate for failed scenes")] = None,
) -> None:
    """Re-assemble a finished run (end card, partial mode) without generating anything."""
    orch = _orchestrator()
    commands.reassemble(orch.rt.storage, run_id, partial_mode=partial)
    print_state(orch, follow(orch, run_id, orch.run_until_blocked(run_id)))


@app.command()
def history(run_id: str) -> None:
    """List the run's snapshots (for inspection or --from-version)."""
    table = Table("version", "stage", "reason", "size")
    for h in _orchestrator().rt.storage.runs.history(run_id):
        table.add_row(str(h.state_version), h.stage.value, h.reason, f"{h.size_bytes} B")
    console.print(table)


@app.command()
def show(run_id: str) -> None:
    """Show the current state of a run."""
    orch = _orchestrator()
    print_state(orch, orch.rt.storage.runs.load(run_id))


@app.command(name="list")
def list_runs(limit: int = 15) -> None:
    """List recent runs."""
    table = Table("run", "stage", "updated", "request")
    for r in _orchestrator().rt.storage.runs.list_runs()[:limit]:
        table.add_row(r.id, r.stage.value, r.updated_at.strftime("%m-%d %H:%M"), r.request[:60])
    console.print(table)


@app.command()
def export(
    run_id: str, out: Annotated[Path | None, typer.Option(help="Output directory")] = None
) -> None:
    """Write the Production Pack (storyboard.md + storyboard.json) for a run."""
    orch = _orchestrator()
    path = orch.export(orch.rt.storage.runs.load(run_id), out)
    console.print(f"Production Pack: [bold]{path}[/]" if path else "[red]Export failed[/]")


@app.command()
def trace(
    run_id: str,
    after: int = 0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    follow_: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep tailing (Ctrl-C to stop).")
    ] = False,
) -> None:
    """Print the event trace of a run (optionally follow it live)."""
    tracer = LiveTrace(console, _orchestrator().rt.storage, verbose=verbose)
    seq = tracer.flush(run_id, after)
    if follow_:

        async def tail() -> None:
            nonlocal seq
            while True:
                await asyncio.sleep(0.3)
                seq = tracer.flush(run_id, seq)

        try:
            asyncio.run(tail())
        except KeyboardInterrupt:
            return


@app.command()
def demo(
    crash: Annotated[bool, typer.Option(help="Include the kill/resume part.")] = True,
    latency: Annotated[float, typer.Option(help="Simulated media latency (s).")] = 1.0,
) -> None:
    """Zero-key showcase: chaos run → recovery → end card → one edit → crash/resume."""
    from adsmith.cli.demo import run_demo  # noqa: PLC0415

    run_demo(console, crash=crash, latency=latency)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the web UI."""
    try:
        import uvicorn  # noqa: PLC0415

        from adsmith.api.app import create_app  # type: ignore[import-not-found,unused-ignore]  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - the UI arrives in M10
        console.print(f"[red]The web UI is not available yet:[/] {exc}")
        raise typer.Exit(1) from exc
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@app.callback()
def main() -> None:
    """Adsmith: marketing-ad agent."""


if __name__ == "__main__":
    app()

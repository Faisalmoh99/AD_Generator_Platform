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
from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
from adsmith.cli.live import LiveTrace
from adsmith.config import Settings
from adsmith.domain.enums import SceneStatus, Stage
from adsmith.domain.events import EventType
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
    if state.endcard:
        flag = "  [yellow](placeholder)[/]" if state.endcard.is_placeholder else ""
        console.print(
            f"[bold]End card[/] {state.endcard.brand_name} — {state.endcard.tagline}{flag}"
        )
    if state.final:
        path = orch.rt.storage.artifacts.run_dir(state.id) / "final.mp4"
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
    }.get(state.stage)
    if nxt:
        console.print(f"Next: [bold]{nxt}[/]")


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
) -> None:
    """Start a new run."""
    orch = _orchestrator()
    state = orch.create_run(request, interactive=not auto, chaos=chaos, hero=hero)
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
def approve(run_id: str) -> None:
    """Approve the storyboard keyframes and generate the clips."""
    orch = _orchestrator()
    commands.approve_storyboard(orch.rt.storage, run_id)
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

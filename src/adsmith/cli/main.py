"""Command-line interface (Typer + Rich). Polished in M9; enough here to drive the agent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from adsmith import __version__
from adsmith.agent import commands
from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.enums import Stage
from adsmith.domain.run import RunState
from adsmith.logging import configure_logging

app = typer.Typer(help="Adsmith: marketing-ad agent.", no_args_is_help=True)
console = Console()


def _orchestrator() -> Orchestrator:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    return Orchestrator(AgentRuntime.from_settings(settings))


def _print_state(state: RunState) -> None:
    console.rule(f"[bold]{state.id}[/]  stage=[cyan]{state.stage}[/]  v{state.state_version}")
    if state.brief:
        b = state.brief
        console.print(
            f"[bold]Brief[/] {b.product} · {b.target_audience} · {b.duration_s:g}s "
            f"{b.aspect_ratio} · {b.language.value}/{b.dialect.value}"
        )
    if state.concepts:
        table = Table("#", "Title", "Logline", show_lines=True)
        for i, c in enumerate(state.concepts.concepts, start=1):
            mark = " ✓" if c.id == state.concepts.selected_id else ""
            table.add_row(f"{i}{mark}", c.title, c.logline)
        console.print(table)
        if state.concepts.selection_rationale:
            console.print(f"[dim]Selection: {state.concepts.selection_rationale}[/]")
    if state.storyboard:
        table = Table("Scene", "Status", "Attempts", "Note")
        for sc in state.storyboard.scenes:
            note = sc.failure.reason_user if sc.failure else ""
            table.add_row(str(sc.order), sc.status.value, str(len(sc.attempts)), note)
        console.print(table)
    if state.final:
        path = Settings().data_dir / "runs" / state.id / "final.mp4"
        console.print(f"[bold green]Final ad:[/] {path} ({state.final.duration_s:g}s)")
    if state.error:
        console.print(f"[red]Error:[/] {state.error.reason_user}")
    ledger = state.budget
    console.print(f"[dim]Spent ${ledger.spent_usd:.4f} of ${ledger.limit_usd:g}[/]")
    if state.stage is Stage.AWAITING_CONCEPT_CHOICE:
        console.print(f"Next: [bold]adsmith choose {state.id} <1-3>[/]")


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
) -> None:
    """Start a new run."""
    orch = _orchestrator()
    state = orch.create_run(request, interactive=not auto, chaos=chaos)
    console.print(f"Run [bold]{state.id}[/] created.")
    _print_state(asyncio.run(orch.run_until_blocked(state.id, stop_before=until)))


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
    _print_state(asyncio.run(orch.resume(run_id, stop_before=until)))


@app.command()
def approve(run_id: str) -> None:
    """Approve the storyboard keyframes and generate the clips."""
    orch = _orchestrator()
    commands.approve_storyboard(orch.rt.storage, run_id)
    _print_state(asyncio.run(orch.run_until_blocked(run_id)))


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
    from adsmith.agent.edit.service import EditService  # noqa: PLC0415
    from adsmith.domain.events import EventType  # noqa: PLC0415

    orch = _orchestrator()
    store = orch.rt.storage
    before = store.runs.load(run_id)
    spent0 = before.budget.spent_usd
    seq0 = max((e.seq for e in store.events.tail(run_id, limit=100_000)), default=0)
    outcome = asyncio.run(
        EditService(orch.rt).edit(run_id, instruction, resolution=resolution)  # type: ignore[arg-type]
    )
    if outcome.status != "applied":
        console.print(f"[yellow]{outcome.status}:[/] {outcome.message}")
        return
    assert outcome.blast is not None and before.storyboard is not None
    console.rule(f"[bold]Edit[/] {instruction}")
    if outcome.patch is not None:
        console.print(outcome.patch.model_dump_json(exclude_none=True, exclude={"confidence"}))
    for c in outcome.conflicts.conflicts if outcome.conflicts else []:
        console.print(f"[magenta]conflict[/] {c.against}: {c.description}")
    console.print(f"resolution: [bold]{outcome.resolution}[/]  {outcome.message}")
    final = asyncio.run(orch.run_until_blocked(run_id))
    events = store.events.tail(run_id, after_seq=seq0, limit=100_000)
    why = {ln.split(" ", 2)[1]: ln for ln in outcome.blast.explanation if ln.startswith("scene")}
    table = Table("scene", "blast radius", "provider calls", "cache hits", "why")
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
        console.print(f"New final: {store.artifacts.run_dir(run_id) / 'final.mp4'}")


@app.command()
def history(run_id: str) -> None:
    """List the run's snapshots (for inspection or --from-version)."""
    table = Table("version", "stage", "reason", "size")
    for h in _orchestrator().rt.storage.runs.history(run_id):
        table.add_row(str(h.state_version), h.stage.value, h.reason, f"{h.size_bytes} B")
    console.print(table)


@app.command()
def choose(
    run_id: str, concept: Annotated[str, typer.Argument(help="1-3 or a concept id")]
) -> None:
    """Choose a concept at the concept gate and continue."""
    orch = _orchestrator()
    commands.choose_concept(orch.rt.storage, run_id, concept)
    _print_state(asyncio.run(orch.run_until_blocked(run_id)))


@app.command()
def show(run_id: str) -> None:
    """Show the current state of a run."""
    _print_state(_orchestrator().rt.storage.runs.load(run_id))


@app.command()
def export(
    run_id: str, out: Annotated[Path | None, typer.Option(help="Output directory")] = None
) -> None:
    """Write the Production Pack (storyboard.md + storyboard.json) for a run."""
    orch = _orchestrator()
    path = orch.export(orch.rt.storage.runs.load(run_id), out)
    console.print(f"Production Pack: [bold]{path}[/]" if path else "[red]Export failed[/]")


@app.command()
def trace(run_id: str, after: int = 0) -> None:
    """Print the event trace of a run."""
    table = Table("seq", "stage", "type", "summary")
    for ev in _orchestrator().rt.storage.events.tail(run_id, after_seq=after, limit=5000):
        style = {"warn": "yellow", "error": "red"}.get(ev.level, "")
        table.add_row(str(ev.seq), ev.stage.value, ev.type.value, ev.summary, style=style)
    console.print(table)


@app.callback()
def main() -> None:
    """Adsmith: marketing-ad agent."""

"""Command-line interface (Typer + Rich). Polished in M9; enough here to drive the agent."""

from __future__ import annotations

import asyncio
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
) -> None:
    """Start a new run."""
    orch = _orchestrator()
    state = orch.create_run(request, interactive=not auto)
    console.print(f"Run [bold]{state.id}[/] created.")
    _print_state(asyncio.run(orch.run_until_blocked(state.id, stop_before=until)))


@app.command()
def resume(run_id: str, until: Annotated[Stage | None, typer.Option()] = None) -> None:
    """Resume a run after a failure, a crash, or a gate."""
    _print_state(asyncio.run(_orchestrator().resume(run_id, stop_before=until)))


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

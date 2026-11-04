"""Command-line interface. Commands are added in M4 and M9."""

from __future__ import annotations

import typer

from adsmith import __version__

app = typer.Typer(help="Adsmith: marketing-ad agent.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.callback()
def main() -> None:
    """Adsmith: marketing-ad agent."""

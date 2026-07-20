"""Warren command-line interface."""

from __future__ import annotations

import typer

from warren import __version__

app = typer.Typer(
    name="warren",
    help="Isolated parallel dev environments, one per git worktree.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the Warren version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Warren — one repo, N agents, zero port collisions."""


@app.command()
def version() -> None:
    """Print the Warren version."""
    typer.echo(__version__)

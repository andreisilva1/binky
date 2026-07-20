"""Warren command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from warren import __version__, ipc
from warren import daemon as _daemon
from warren.config import ConfigError, load_config

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
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the Warren version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Warren — one repo, N agents, zero port collisions."""


@app.command()
def version() -> None:
    """Print the Warren version."""
    typer.echo(__version__)


@app.command()
def check(
    path: Annotated[
        Path,
        typer.Argument(help="Path to the warren.toml to validate."),
    ] = Path("warren.toml"),
) -> None:
    """Validate a warren.toml and report any problems."""
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(
        f"✓ {path} is valid — project '{cfg.project.name}', "
        f"{len(cfg.services)} service(s)",
        fg=typer.colors.GREEN,
    )


daemon_app = typer.Typer(help="Manage the Warren daemon.", no_args_is_help=True)
app.add_typer(daemon_app, name="daemon")


@daemon_app.command(hidden=True)
def serve() -> None:
    """(internal) Run the daemon in the foreground — used by the detached spawn."""
    _daemon.run()


@daemon_app.command()
def start() -> None:
    """Start the Warren daemon (no-op if already running)."""
    _daemon.start()
    typer.secho("✓ daemon running", fg=typer.colors.GREEN)


@daemon_app.command()
def stop() -> None:
    """Stop the Warren daemon."""
    if _daemon.stop():
        typer.secho("✓ daemon stopped", fg=typer.colors.GREEN)
    else:
        typer.secho("daemon was not running", fg=typer.colors.YELLOW)


@daemon_app.command()
def status() -> None:
    """Show whether the daemon is running."""
    if _daemon.is_running():
        info = ipc.read_daemon_info()
        typer.secho(f"✓ running (pid {info.pid}, {info.transport})", fg=typer.colors.GREEN)
    else:
        typer.secho("not running", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

"""Warren command-line interface."""

from __future__ import annotations

from itertools import groupby
from pathlib import Path
from typing import Annotated

import typer

from warren import __version__, ipc, paths
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


@app.command()
def up(
    worktree: Annotated[str, typer.Argument(help="Worktree/branch name to bring up.")],
    group: Annotated[str, typer.Option("--group", "-g", help="Only start a service group.")] = "",
) -> None:
    """Bring a worktree's isolated world online."""
    _daemon.ensure()
    try:
        res = ipc.call(
            "up",
            {"root": str(Path.cwd()), "worktree": worktree, "group": group or None},
            timeout=120,
        )
    except ipc.RpcError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for svc in res["services"]:
        tail = f"  :{svc['port']}" if svc.get("port") else ""
        typer.echo(f"  {svc['service']}  pid {svc['pid']}{tail}")
    typer.secho(
        f"✓ up: {res['worktree']} ({len(res['services'])} service(s))", fg=typer.colors.GREEN
    )


@app.command()
def down(worktree: Annotated[str, typer.Argument(help="Worktree to stop.")]) -> None:
    """Stop a worktree's processes (keeps its files and ports)."""
    if not _daemon.is_running():
        typer.secho("daemon not running — nothing to stop", fg=typer.colors.YELLOW)
        return
    try:
        res = ipc.call("down", {"root": str(Path.cwd()), "worktree": worktree})
    except ipc.RpcError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"✓ down: {worktree} ({res['stopped']} stopped)", fg=typer.colors.GREEN)


@app.command()
def remove(
    worktree: Annotated[str, typer.Argument(help="Worktree to destroy.")],
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Discard uncommitted changes.")
    ] = False,
) -> None:
    """Destroy a worktree — stop it, free its ports, and remove its files."""
    if not _daemon.is_running():
        typer.secho("daemon not running — nothing to remove", fg=typer.colors.YELLOW)
        return
    try:
        ipc.call("remove", {"root": str(Path.cwd()), "worktree": worktree, "force": force})
    except ipc.RpcError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"✓ removed: {worktree}", fg=typer.colors.GREEN)


@app.command()
def status(
    scope: Annotated[str, typer.Argument(help='"." to show only the current project')] = "",
) -> None:
    """Show worktrees on this machine, grouped by project (`status .` = current project)."""
    if not _daemon.is_running():
        typer.secho("daemon not running", fg=typer.colors.YELLOW)
        return
    worktrees = ipc.call("status")["worktrees"]
    if scope == ".":
        try:
            current = load_config(Path.cwd() / "warren.toml").project.name
        except ConfigError:
            typer.secho("not inside a warren project", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1) from None
        worktrees = [w for w in worktrees if w["project"] == current]
    if not worktrees:
        typer.echo("no worktrees")
        return
    worktrees.sort(key=lambda w: (w["project"], w["worktree"]))
    for project, group in groupby(worktrees, key=lambda w: w["project"]):
        typer.secho(project, bold=True)
        for wt in group:
            state = "running" if wt["running"] else wt["status"]
            typer.echo(f"  {wt['worktree']}  [{state}]")
            for port in wt["ports"]:
                label = f".{port['label']}" if port["label"] else ""
                typer.echo(f"      {port['service']}{label}  :{port['port']}")


@app.command()
def dashboard() -> None:
    """Open the live dashboard (TUI)."""
    _daemon.ensure()
    from warren.dashboard import run as run_dashboard

    run_dashboard()


@app.command()
def logs(worktree: Annotated[str, typer.Argument(help="Worktree to show logs for.")]) -> None:
    """Print a worktree's captured service logs."""
    try:
        project = load_config(Path.cwd() / "warren.toml").project.name
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    log_dir = paths.logs_dir() / project / worktree
    files = sorted(log_dir.glob("*.log")) if log_dir.exists() else []
    if not files:
        typer.secho("no logs", fg=typer.colors.YELLOW)
        return
    for log in files:
        typer.secho(f"── {log.stem} ──", fg=typer.colors.CYAN)
        typer.echo(log.read_text(encoding="utf-8", errors="ignore").rstrip())


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


@daemon_app.command("status")
def daemon_status() -> None:
    """Show whether the daemon is running."""
    if _daemon.is_running():
        info = ipc.read_daemon_info()
        typer.secho(f"✓ running (pid {info.pid}, {info.transport})", fg=typer.colors.GREEN)
    else:
        typer.secho("not running", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

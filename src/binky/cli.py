"""Binky command-line interface."""

from __future__ import annotations

import contextlib
import json
from itertools import groupby
from pathlib import Path
from typing import Annotated

import typer

from binky import __version__, init_cmd, ipc, paths, settings
from binky import daemon as _daemon
from binky.config import ConfigError, config_warnings, load_config

app = typer.Typer(
    name="binky",
    help="Isolated parallel dev environments, one per git worktree.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


# EX_TEMPFAIL (sysexits.h): the request was accepted but hasn't finished — a script should retry
# or poll, not treat it as a hard failure. We use it for the daemon-is-busy timeout below.
EXIT_PENDING = 75


def _rpc_fail(exc: ipc.RpcError) -> None:
    """Render an RPC error and exit — but a `timeout` isn't a failure. The daemon is still working
    the (serialized) op and it will converge; a command queued behind a slow op on the same worktree
    hits this. So it exits `EXIT_PENDING` (75), which a caller can treat as "pending, poll status"
    rather than "failed", and prints in the neutral colour rather than red."""
    if exc.code == "timeout":
        typer.secho(f"… {exc.message}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=EXIT_PENDING) from exc
    typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1) from exc


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the Binky version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Binky — one repo, N agents, zero port collisions."""


@app.command()
def version() -> None:
    """Print the Binky version."""
    typer.echo(__version__)


@app.command()
def init(
    directory: Annotated[
        Path, typer.Argument(help="Project directory to read (default: current).")
    ] = Path("."),
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing binky.toml.")
    ] = False,
) -> None:
    """Write a starting `binky.toml` from docker-compose.yml and .env."""
    target = directory / "binky.toml"
    if target.exists() and not force:
        typer.secho(f"✗ {target} already exists — pass --force to overwrite", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    try:
        result = init_cmd.generate(directory)
    except init_cmd.InitError as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    target.write_text(result.toml, encoding="utf-8")
    typer.secho(f"✓ wrote {target}", fg=typer.colors.GREEN)
    for note in result.notes:
        typer.secho(f"  · {note}", fg=typer.colors.YELLOW)
    typer.echo("Review it, then: binky check && binky up <worktree>")


@app.command()
def check(
    path: Annotated[
        Path,
        typer.Argument(help="Path to the binky.toml to validate."),
    ] = Path("binky.toml"),
) -> None:
    """Validate a binky.toml and report any problems."""
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
    for warning in config_warnings(cfg):
        typer.secho(f"  ⚠ {warning}", fg=typer.colors.YELLOW)


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
        _rpc_fail(exc)
    for svc in res["services"]:
        tail = f"  :{svc['port']}" if svc.get("port") else ""
        typer.echo(f"  {svc['service']}  pid {svc['pid']}{tail}")
    for warning in res.get("warnings", []):
        typer.secho(f"  ⚠ {warning}", fg=typer.colors.YELLOW)
    typer.secho(
        f"✓ up: {res['worktree']} ({len(res['services'])} service(s))", fg=typer.colors.GREEN
    )


@app.command()
def reload(
    worktree: Annotated[str, typer.Argument(help="Worktree/branch name to restart.")],
    group: Annotated[str, typer.Option("--group", "-g", help="Only restart a service group.")] = "",
) -> None:
    """Restart a worktree in place — stop it, then bring it back up on the same ports and data.

    A single atomic step under the worktree's lock, so nothing slips in between the stop and the
    start. Use it to pick up a config or dependency change without re-cloning the data slice.
    """
    _daemon.ensure()
    try:
        res = ipc.call(
            "reload",
            {"root": str(Path.cwd()), "worktree": worktree, "group": group or None},
            timeout=120,
        )
    except ipc.RpcError as exc:
        _rpc_fail(exc)
    for svc in res["services"]:
        tail = f"  :{svc['port']}" if svc.get("port") else ""
        typer.echo(f"  {svc['service']}  pid {svc['pid']}{tail}")
    for warning in res.get("warnings", []):
        typer.secho(f"  ⚠ {warning}", fg=typer.colors.YELLOW)
    typer.secho(
        f"✓ reload: {res['worktree']} ({res.get('stopped', 0)} stopped, "
        f"{len(res['services'])} up)",
        fg=typer.colors.GREEN,
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
        _rpc_fail(exc)
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
        _rpc_fail(exc)
    typer.secho(f"✓ removed: {worktree}", fg=typer.colors.GREEN)


@app.command()
def verify(worktree: Annotated[str, typer.Argument(help="Worktree to verify.")]) -> None:
    # Square brackets are Rich markup in Typer's help renderer and get eaten, so the
    # TOML section is named without them here.
    """Run the project's agents.verify checks against a worktree (exit non-zero on failure)."""
    if not _daemon.is_running():
        typer.secho("daemon not running", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    try:
        res = ipc.call("verify", {"root": str(Path.cwd()), "worktree": worktree}, timeout=600)
    except ipc.RpcError as exc:
        _rpc_fail(exc)
    for check in res["checks"]:
        mark = "✓" if check["passed"] else "✗"
        color = typer.colors.GREEN if check["passed"] else typer.colors.RED
        typer.secho(f"  {mark} {check['command']}", fg=color)
    if res["passed"]:
        typer.secho(f"✓ verify: {worktree} passing", fg=typer.colors.GREEN)
    else:
        typer.secho(f"✗ verify: {worktree} failing", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def env(
    worktree: Annotated[str, typer.Argument(help="Worktree whose live coordinates to print.")],
    dotenv: Annotated[
        bool, typer.Option("--dotenv", help="Print KEY=value lines instead of JSON.")
    ] = False,
) -> None:
    """Print a worktree's exposed coordinates (agents.expose) as JSON or dotenv."""
    if not _daemon.is_running():
        typer.secho("daemon not running", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    try:
        res = ipc.call("env", {"root": str(Path.cwd()), "worktree": worktree})
    except ipc.RpcError as exc:
        _rpc_fail(exc)
    coords = res["env"]
    if dotenv:
        for name, value in coords.items():
            typer.echo(f"{name.upper()}_URL={value}")
    else:
        typer.echo(json.dumps(coords, indent=2))


@app.command()
def curl(
    worktree: Annotated[str, typer.Argument(help="Worktree the service lives in.")],
    service: Annotated[str, typer.Argument(help="Service to reach (must have a `url`).")],
    path: Annotated[str, typer.Argument(help="Request path, e.g. /tasks.")] = "/",
    method: Annotated[str, typer.Option("-X", "--request", help="HTTP method.")] = "GET",
    data: Annotated[
        str, typer.Option("-d", "--data", help="Request body (JSON content-type by default).")
    ] = "",
    header: Annotated[
        list[str], typer.Option("-H", "--header", help="Extra 'Name: value' header (repeatable).")
    ] = None,
    include: Annotated[
        bool, typer.Option("-i", "--include", help="Print the status line and response headers.")
    ] = False,
    fail: Annotated[
        bool, typer.Option("-f", "--fail", help="Exit non-zero on a >= 400 response.")
    ] = False,
) -> None:
    """Call a worktree's service through the proxy, with the right Host header filled in.

    `*.localhost` names resolve in browsers but not from the Windows CLI, so hitting a service
    by hostname there needs a manual `127.0.0.1:<proxy>` + `Host:` dance. This does it for you:
    it asks the daemon for the service's route and dials the proxy on your behalf.
    """
    import http.client

    if not _daemon.is_running():
        typer.secho("daemon not running", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    try:
        route = ipc.call("route", {"root": str(Path.cwd()), "worktree": worktree, "service": service})
    except ipc.RpcError as exc:
        _rpc_fail(exc)

    proxy_port = route.get("proxy_port") or 0
    if proxy_port <= 0:
        detail = route.get("proxy_error") or "proxy_port = 0"
        typer.secho(f"✗ http proxy isn't listening ({detail})", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    headers = {"Host": route["host"]}
    for raw_header in header or []:
        if ":" not in raw_header:
            typer.secho(f"✗ bad header '{raw_header}' — expected 'Name: value'", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        key, value = raw_header.split(":", 1)
        headers[key.strip()] = value.strip()
    body = data.encode("utf-8") if data else None
    if body is not None and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"  # the common case here is a JSON API

    target = path if path.startswith("/") else f"/{path}"
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=30)
    try:
        conn.request(method.upper(), target, body=body, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
    except http.client.InvalidURL as exc:
        # a path with spaces/control chars never leaves the client — call it out plainly
        typer.secho(f"✗ invalid request path {target!r} — {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except (OSError, http.client.HTTPException) as exc:
        typer.secho(
            f"✗ couldn't reach {route['host']} via the proxy on :{proxy_port} — "
            f"is '{worktree}' up? ({exc})",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    color = typer.colors.GREEN if resp.status < 400 else typer.colors.RED
    typer.secho(f"HTTP {resp.status} {resp.reason}", fg=color, err=True)
    if include:
        for key, value in resp.getheaders():
            typer.echo(f"{key}: {value}")
        typer.echo("")
    typer.echo(payload.decode("utf-8", errors="replace"))
    if fail and resp.status >= 400:
        raise typer.Exit(code=1)


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
            current = load_config(Path.cwd() / "binky.toml").project.name
        except ConfigError:
            typer.secho("not inside a binky project", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1) from None
        worktrees = [w for w in worktrees if w["project"] == current]
    if not worktrees:
        typer.echo("no worktrees")
        return
    worktrees.sort(key=lambda w: (w["project"], w["worktree"]))
    for project, group in groupby(worktrees, key=lambda w: w["project"]):
        typer.secho(project, bold=True)
        for wt in group:
            if wt["running"]:
                state = "unhealthy" if wt.get("health") == "unhealthy" else "running"
            else:
                state = wt["status"]
            vtag = f" · {wt['verify']}" if wt.get("verify") else ""
            line = f"  {wt['worktree']}  [{state}{vtag}]"
            # a crash-loop give-up (failed) or a live unhealthy service is an alert, not noise
            if state in ("failed", "unhealthy"):
                typer.secho(line, fg=typer.colors.RED)
            else:
                typer.echo(line)
            for port in wt["ports"]:
                label = f".{port['label']}" if port["label"] else ""
                typer.echo(f"      {port['service']}{label}  :{port['port']}")


@app.command()
def dashboard() -> None:
    """Open the live dashboard (TUI)."""
    _daemon.ensure()
    from binky.dashboard import run as run_dashboard

    run_dashboard()


@app.command()
def mcp() -> None:
    """Run the Binky MCP server (stdio) so AI agents can call Binky's verbs as tools."""
    try:
        from binky.mcp import serve

        serve()
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def metrics() -> None:
    """Print point-in-time daemon metrics as JSON."""
    if not _daemon.is_running():
        typer.secho("daemon not running", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(ipc.call("metrics"), indent=2))


def _dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for item in path.rglob("*"):
            with contextlib.suppress(OSError):
                if item.is_file():
                    total += item.stat().st_size
    return total


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}TB"


@app.command()
def doctor() -> None:
    """Diagnose the Binky install + daemon; exit non-zero if something is wrong."""
    ok = True

    def line(passed: bool, label: str, detail: str = "", warn: bool = False) -> None:
        nonlocal ok
        mark = "✓" if passed else ("⚠" if warn else "✗")
        color = (
            typer.colors.GREEN if passed else (typer.colors.YELLOW if warn else typer.colors.RED)
        )
        typer.secho(f"  {mark} {label}" + (f" — {detail}" if detail else ""), fg=color)
        if not passed and not warn:
            ok = False

    running = _daemon.is_running()
    line(running, "daemon reachable", "" if running else "starts on demand", warn=not running)

    try:
        from binky.registry import Registry, default_db_path

        Registry.connect().close()
        line(True, "registry openable", str(default_db_path()))
    except Exception as exc:
        line(False, "registry openable", str(exc))

    from binky.https import caddy_binary

    caddy = caddy_binary()
    line(bool(caddy), "Caddy (https)", caddy or "not on PATH — https unavailable", warn=not caddy)

    logs_size = _dir_size(paths.logs_dir())
    logs_ok = logs_size <= 500 * 1024 * 1024
    line(logs_ok, "log size", _human(logs_size), warn=not logs_ok)

    if running:
        d = ipc.call("doctor")
        if d["proxy_error"]:
            line(False, "http proxy", f"not listening on {d['proxy_error']}")
        elif d["proxy_port"]:
            line(True, "http proxy", f"listening on :{d['proxy_port']}")
        else:
            line(True, "http proxy", "disabled (proxy_port = 0)", warn=True)
        if d["https_error"]:
            line(False, "https (Caddy)", d["https_error"], warn=True)
        elif d["https_active"]:
            line(True, "https (Caddy)", "active")

        m = ipc.call("metrics")
        wt = m["worktrees"]
        typer.echo(
            f"  · {wt['running']} running / {wt['total']} worktrees, "
            f"{m['ports_allocated']} ports, {m['slices']} slices"
        )
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def gc() -> None:
    """Sweep orphaned resources a crash left behind (half-provisioned adapter slices)."""
    if not _daemon.is_running():
        typer.secho("daemon not running — nothing to collect", fg=typer.colors.YELLOW)
        return
    try:
        res = ipc.call("gc", timeout=120)
    except ipc.RpcError as exc:
        _rpc_fail(exc)
    if not res["count"]:
        typer.secho("✓ nothing to collect", fg=typer.colors.GREEN)
        return
    for item in res["swept"]:
        typer.echo(f"  swept {item['adapter']} slice for service '{item['service']}'")
    typer.secho(f"✓ gc: {res['count']} orphan(s) collected", fg=typer.colors.GREEN)


@app.command()
def config() -> None:
    """Show Binky's machine-global settings (RAM/disk reserves and proxy ports)."""
    s = settings.load()
    typer.echo(f"  ram reserve   {s.ram_reserve_pct}% of total RAM kept free")
    disk = f"{s.disk_reserve_gb} GB kept free" if s.disk_reserve_gb else "off"
    typer.echo(f"  disk reserve  {disk}")
    typer.echo(f"  proxy port    {s.proxy_port}" + ("  (disabled)" if not s.proxy_port else ""))
    typer.echo(f"  https port    {s.https_port}")


def _apply_settings(changes: dict, ok_message: str) -> dict:
    _daemon.ensure()  # a live apply needs the daemon (reserves take effect on the next admit)
    try:
        res = ipc.call("settings_set", changes)
    except ipc.RpcError as exc:
        _rpc_fail(exc)
    typer.secho(ok_message.format(**res), fg=typer.colors.GREEN)
    return res


@app.command()
def reserve(
    ram: Annotated[
        int | None, typer.Option("--ram", help="Percent of total RAM to keep free (5-50).")
    ] = None,
    disk: Annotated[
        int | None, typer.Option("--disk", help="GB of disk to always keep free (0 disables).")
    ] = None,
) -> None:
    """Set how much RAM/disk Binky permanently keeps free (raise it to leave the machine more).

    RAM is a percentage of total; disk is a fixed headroom in GB (a percentage of a big disk
    would demand more free space than a nearly-full disk can ever offer).
    """
    changes = {}
    if ram is not None:
        changes["ram_reserve_pct"] = ram
    if disk is not None:
        changes["disk_reserve_gb"] = disk
    if not changes:
        typer.secho(
            "nothing to set — pass --ram and/or --disk (see `binky config`)",
            fg=typer.colors.YELLOW,
        )
        return
    _apply_settings(
        changes, "✓ reserves: {ram_reserve_pct}% RAM, {disk_reserve_gb} GB disk kept free"
    )


@app.command()
def port(
    proxy: Annotated[
        int | None, typer.Option("--proxy", help="HTTP proxy port (0 disables hostname routing).")
    ] = None,
    https: Annotated[int | None, typer.Option("--https", help="HTTPS (Caddy) port.")] = None,
) -> None:
    """Set the proxy/HTTPS ports (takes effect on the next daemon restart)."""
    changes = {}
    if proxy is not None:
        changes["proxy_port"] = proxy
    if https is not None:
        changes["https_port"] = https
    if not changes:
        typer.secho("nothing to set — pass --proxy and/or --https", fg=typer.colors.YELLOW)
        return
    res = _apply_settings(changes, "✓ ports: proxy {proxy_port}, https {https_port}")
    if res.get("restart_required"):
        typer.secho(
            "  ⚠ restart the daemon to bind the new port: `binky daemon restart`",
            fg=typer.colors.YELLOW,
        )


@app.command()
def logs(worktree: Annotated[str, typer.Argument(help="Worktree to show logs for.")]) -> None:
    """Print a worktree's captured service logs."""
    try:
        project = load_config(Path.cwd() / "binky.toml").project.name
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


daemon_app = typer.Typer(help="Manage the Binky daemon.", no_args_is_help=True)
app.add_typer(daemon_app, name="daemon")


@daemon_app.command(hidden=True)
def serve() -> None:
    """(internal) Run the daemon in the foreground — used by the detached spawn."""
    _daemon.run()


@daemon_app.command()
def start() -> None:
    """Start the Binky daemon (no-op if already running)."""
    _daemon.start()
    typer.secho("✓ daemon running", fg=typer.colors.GREEN)


@daemon_app.command()
def stop() -> None:
    """Stop the Binky daemon."""
    if _daemon.stop():
        typer.secho("✓ daemon stopped", fg=typer.colors.GREEN)
    else:
        typer.secho("daemon was not running", fg=typer.colors.YELLOW)


@daemon_app.command()
def restart() -> None:
    """Restart the Binky daemon (e.g. to bind a changed proxy/https port)."""
    _daemon.stop()
    _daemon.start()
    typer.secho("✓ daemon restarted", fg=typer.colors.GREEN)


@daemon_app.command("status")
def daemon_status() -> None:
    """Show whether the daemon is running."""
    if _daemon.is_running():
        info = ipc.read_daemon_info()
        typer.secho(f"✓ running (pid {info.pid}, {info.transport})", fg=typer.colors.GREEN)
    else:
        typer.secho("not running", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

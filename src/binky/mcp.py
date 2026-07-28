"""Optional MCP server exposing Binky's verbs as tools for AI agents.

The same verbs as the CLI (``up``/``down``/``status``/``env``/``verify``), surfaced
over the Model Context Protocol so an agent discovers and calls Binky as a standard
tool provider. Binky stays the substrate — the agent *calls* it; it never drives
the agent. Each tool is a thin wrapper over the daemon RPC, so people (CLI) and
agents (MCP) hit exactly the same logic.

Needs the extra: ``pip install 'binky[mcp]'``. Run with ``binky mcp``.
"""

from __future__ import annotations

import os

from binky import daemon, ipc

_INSTRUCTIONS = """\
Binky gives each git worktree its own isolated local world — its own ports, env,
private database slice, and stable URLs — so several agents can work the same repo
in parallel without colliding on ports, databases, or .env. You call Binky; it
never drives you.

Reach for it when you're about to run a branch's dev server or migrations while
another branch is already up, or when a task needs a real (not mocked) throwaway
database. If you only ever have one world up at a time, you don't need it.

Typical loop: up(worktree) brings a worktree online and returns its ports; env()
reads its live URLs to hand to a process or a test; verify() runs the project's
own checks to gate the work; down() pauses it (files and ports kept, resumes
instantly). Destroying a worktree — freeing its ports and dropping its database —
is deliberately left to a human (`binky remove`) and not exposed here.

`worktree` is a branch/worktree name you choose. `root` is the project directory
and defaults to the current working directory — pass it only when the project
lives elsewhere.
"""


def _root(root: str) -> str:
    return root or os.getcwd()


def up(worktree: str, root: str = "", group: str = "") -> dict:
    """Bring a worktree's isolated world online, starting the daemon if needed.

    Creates or reuses the git worktree, allocates a port for every service,
    provisions each adapter's private slice (e.g. a per-worktree database), starts
    the services in dependency order, and routes them through the proxy.

    Args:
        worktree: the branch/worktree name to bring up.
        root: the project directory (defaults to the current working directory).
        group: an optional service group to start on its own instead of all services.

    Returns a dict with the ``worktree`` and its started ``services`` (each with its
    name, ``pid``, and ``port``), plus any ``warnings``. Call env() for the URLs.
    """
    daemon.ensure()
    return ipc.call(
        "up", {"root": _root(root), "worktree": worktree, "group": group or None}, timeout=120
    )


def down(worktree: str, root: str = "") -> dict:
    """Stop a worktree's processes while keeping its files, ports, and data.

    A paused worktree resumes instantly with up(). Idempotent: if no daemon is
    running there is nothing to stop, and this reports that instead of failing.

    Args:
        worktree: the worktree to stop.
        root: the project directory (defaults to the current working directory).

    Returns a dict with the number of processes ``stopped``.
    """
    if not daemon.is_running():
        return {"worktree": worktree, "stopped": 0, "note": "daemon not running — nothing to stop"}
    # A graceful stop (SIGTERM, drain, SIGKILL only if it overstays) can take several
    # seconds per service — the 5s default would spuriously time out on a normal stop.
    return ipc.call("down", {"root": _root(root), "worktree": worktree}, timeout=30)


def status() -> dict:
    """List every worktree on this machine with its state, health, and ports.

    A read-only snapshot — never starts the daemon. When no daemon is running
    nothing is up, so this returns an empty ``worktrees`` list rather than failing.

    Returns a dict with a ``worktrees`` list; each entry carries its ``project``,
    ``worktree``, ``status``, whether it's ``running``, its ``health``, and ``ports``.
    """
    if not daemon.is_running():
        return {"worktrees": []}
    return ipc.call("status")


def env(worktree: str, root: str = "") -> dict:
    """A worktree's live coordinates — service URLs and adapter addresses.

    These are what you hand to a process, a test run, or your own HTTP calls to reach
    the worktree's services. The worktree must be up first (call up()).

    Args:
        worktree: the worktree whose coordinates to read.
        root: the project directory (defaults to the current working directory).

    Returns a dict with an ``env`` map of name → URL/address.
    """
    _require_up(worktree)
    return ipc.call("env", {"root": _root(root), "worktree": worktree})


def verify(worktree: str, root: str = "") -> dict:
    """Run the project's agents.verify checks against a worktree.

    The project defines these checks in its binky.toml; this runs them against the
    live worktree and reports the verdict — use it to gate work before handing off.
    The worktree must be up first (call up()).

    Args:
        worktree: the worktree to verify.
        root: the project directory (defaults to the current working directory).

    Returns a dict with an overall ``passed`` boolean and a ``checks`` list (each
    with its ``command`` and ``passed``).
    """
    _require_up(worktree)
    return ipc.call("verify", {"root": _root(root), "worktree": worktree}, timeout=600)


def _require_up(worktree: str) -> None:
    """Turn 'no daemon' into an actionable error instead of an opaque transport failure."""
    if not daemon.is_running():
        raise RuntimeError(
            f"no Binky daemon is running, so '{worktree}' is not up — call up('{worktree}') first"
        )


TOOLS = (up, down, status, env, verify)


def _load_server_class():
    """The high-level server class the ``mcp`` extra provides.

    It is ``FastMCP`` (``mcp.server.fastmcp``) in the 1.x SDK and ``MCPServer``
    (``mcp.server.mcpserver``) in 2.x. Both take ``(name, instructions=...)`` and
    expose the same ``.tool(name=, description=)`` decorator and ``.run()``, so the
    rest of :func:`build_server` is version-agnostic. Prefer the 2.x class; fall
    back to 1.x; only if neither is importable is the extra genuinely missing.
    """
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError as exc:
        raise RuntimeError("MCP support needs the extra: pip install 'binky[mcp]'") from exc


def build_server():
    """Build the MCP server with Binky's tools. Requires the ``mcp`` extra."""
    server = _load_server_class()("binky", instructions=_INSTRUCTIONS)
    for fn in TOOLS:
        server.tool(name=fn.__name__, description=(fn.__doc__ or "").strip())(fn)
    return server


def serve() -> None:
    """Run the MCP server over stdio (the entry point for ``binky mcp``)."""
    build_server().run()

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


def _root(root: str) -> str:
    return root or os.getcwd()


def up(worktree: str, root: str = "", group: str = "") -> dict:
    """Bring a worktree's isolated world online (starts the daemon if needed)."""
    daemon.ensure()
    return ipc.call(
        "up", {"root": _root(root), "worktree": worktree, "group": group or None}, timeout=120
    )


def down(worktree: str, root: str = "") -> dict:
    """Stop a worktree's processes (keeps its files and ports)."""
    return ipc.call("down", {"root": _root(root), "worktree": worktree})


def status() -> dict:
    """List every worktree on this machine with its state, health, and ports."""
    return ipc.call("status")


def env(worktree: str, root: str = "") -> dict:
    """A worktree's exposed live coordinates (service URLs / adapter addresses)."""
    return ipc.call("env", {"root": _root(root), "worktree": worktree})


def verify(worktree: str, root: str = "") -> dict:
    """Run the project's verify checks against a worktree; returns the pass/fail verdict."""
    return ipc.call("verify", {"root": _root(root), "worktree": worktree}, timeout=600)


TOOLS = (up, down, status, env, verify)


def build_server():
    """Build the FastMCP server with Binky's tools. Requires the ``mcp`` extra."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("MCP support needs the extra: pip install 'binky[mcp]'") from exc
    server = FastMCP("binky")
    for fn in TOOLS:
        server.tool(name=fn.__name__, description=(fn.__doc__ or "").strip())(fn)
    return server


def serve() -> None:
    """Run the MCP server over stdio (the entry point for ``binky mcp``)."""
    build_server().run()

"""The MCP surface is a thin wrapper: each tool maps to the same daemon RPC the CLI
uses. These pin the mapping without needing the `mcp` SDK installed."""

import importlib.util

import pytest

from binky import mcp

_HAS_MCP = importlib.util.find_spec("mcp") is not None


def test_tools_are_the_binky_verbs() -> None:
    assert {fn.__name__ for fn in mcp.TOOLS} == {"up", "down", "status", "env", "verify"}


def test_tools_wrap_the_daemon_rpc(monkeypatch) -> None:
    seen: dict = {}

    def fake_call(method, params=None, timeout=None):
        seen["method"] = method
        seen["params"] = params
        return {"ok": True}

    monkeypatch.setattr(mcp.daemon, "ensure", lambda: None)
    monkeypatch.setattr(mcp.daemon, "is_running", lambda: True)
    monkeypatch.setattr(mcp.ipc, "call", fake_call)

    mcp.up("feat", root="/proj")
    assert seen["method"] == "up"
    assert seen["params"]["worktree"] == "feat"
    assert seen["params"]["root"] == "/proj"

    for verb in ("down", "env", "verify"):
        getattr(mcp, verb)("feat", root="/proj")
        assert seen["method"] == verb

    mcp.status()
    assert seen["method"] == "status"


def test_status_without_daemon_is_empty_not_an_error(monkeypatch) -> None:
    # A read-only snapshot: no daemon means nothing is up, which is a normal answer,
    # not a transport failure the agent has to reason about.
    monkeypatch.setattr(mcp.daemon, "is_running", lambda: False)
    monkeypatch.setattr(mcp.ipc, "call", _forbidden_call)
    assert mcp.status() == {"worktrees": []}


def test_down_without_daemon_is_a_noop(monkeypatch) -> None:
    # Idempotent pause: nothing to stop is success, not an error.
    monkeypatch.setattr(mcp.daemon, "is_running", lambda: False)
    monkeypatch.setattr(mcp.ipc, "call", _forbidden_call)
    res = mcp.down("feat")
    assert res["stopped"] == 0


def test_env_and_verify_without_daemon_raise_an_actionable_error(monkeypatch) -> None:
    # These need a live worktree; surface a clear "call up() first" instead of an
    # opaque transport exception.
    monkeypatch.setattr(mcp.daemon, "is_running", lambda: False)
    monkeypatch.setattr(mcp.ipc, "call", _forbidden_call)
    for verb in ("env", "verify"):
        with pytest.raises(RuntimeError, match=r"up\('feat'\)"):
            getattr(mcp, verb)("feat")


def _forbidden_call(method, params=None, timeout=None):
    raise AssertionError(f"ipc.call({method!r}) should not run without a daemon")


@pytest.mark.skipif(_HAS_MCP, reason="mcp installed → build_server succeeds")
def test_build_server_requires_extra() -> None:
    with pytest.raises(RuntimeError, match=r"binky\[mcp\]"):
        mcp.build_server()


@pytest.mark.skipif(not _HAS_MCP, reason="mcp not installed")
def test_build_server_registers_tools() -> None:
    # Actually builds against the installed SDK — this is the one test that would
    # have caught the FastMCP→MCPServer class move between mcp 1.x and 2.x. It only
    # runs where the `mcp` extra is installed, so CI must install it to cover this.
    server = mcp.build_server()
    assert server is not None
    assert "worktree" in (getattr(server, "instructions", "") or "")

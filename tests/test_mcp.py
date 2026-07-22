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


@pytest.mark.skipif(_HAS_MCP, reason="mcp installed → build_server succeeds")
def test_build_server_requires_extra() -> None:
    with pytest.raises(RuntimeError, match=r"binky\[mcp\]"):
        mcp.build_server()


@pytest.mark.skipif(not _HAS_MCP, reason="mcp not installed")
def test_build_server_registers_tools() -> None:
    server = mcp.build_server()
    assert server is not None

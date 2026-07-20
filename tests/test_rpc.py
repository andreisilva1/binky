"""Unit tests for the RPC router + error envelope (no daemon process)."""

import asyncio

from warren import rpc


def _run(coro):
    return asyncio.run(coro)


def test_dispatch_ok() -> None:
    r = rpc.Router()

    @r.method("echo")
    async def _echo(params: dict) -> dict:
        return {"got": params}

    assert _run(r.dispatch("echo", {"a": 1})) == {"ok": True, "result": {"got": {"a": 1}}}


def test_unknown_method() -> None:
    r = rpc.Router()
    resp = _run(r.dispatch("nope", {}))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "unknown_method"
    assert "nope" in resp["error"]["message"]


def test_fault_is_structured() -> None:
    r = rpc.Router()

    @r.method("boom")
    async def _boom(_params: dict) -> dict:
        raise rpc.RpcFault("conflict", "already exists")

    resp = _run(r.dispatch("boom", {}))
    assert resp == {"ok": False, "error": {"code": "conflict", "message": "already exists"}}


def test_unexpected_error_is_internal() -> None:
    r = rpc.Router()

    @r.method("crash")
    async def _crash(_params: dict) -> dict:
        raise ValueError("kaboom")

    resp = _run(r.dispatch("crash", {}))
    assert resp["error"]["code"] == "internal"
    assert "kaboom" in resp["error"]["message"]


def test_names_sorted() -> None:
    r = rpc.Router()

    async def _noop(_params: dict) -> dict:
        return {}

    r.add("beta", _noop)
    r.add("alpha", _noop)
    assert r.names() == ["alpha", "beta"]

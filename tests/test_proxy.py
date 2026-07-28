"""The embedded reverse proxy's routing edges — in particular Caddy's on-demand TLS
``ask``: the proxy must authorize a cert only for a host it actually routes, else an
attacker could make Caddy mint certs for arbitrary SNIs. Driven against a real Proxy
with a fake resolver, so the request/response bytes are exercised, no worktree needed."""

import asyncio

from binky.https import ONDEMAND_ASK_PATH
from binky.proxy import Proxy


async def _get(port: int, target: str, host: str = "127.0.0.1") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    await writer.drain()
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await reader.read(4096)
        if not chunk:
            break
        head += chunk
    writer.close()
    status = int(head.split(b" ", 2)[1]) if head else 0
    return status, head


def _run(coro):
    return asyncio.run(coro)


def test_ondemand_ask_authorizes_only_routable_hosts() -> None:
    # resolver routes exactly one host; everything else is unknown
    routed = "api.feat.acme.localhost"
    proxy = Proxy(lambda h: 4783 if h == routed else None)

    async def scenario():
        port = await proxy.start(0)
        try:
            ok, _ = await _get(port, f"{ONDEMAND_ASK_PATH}?domain={routed}")
            nope, _ = await _get(port, f"{ONDEMAND_ASK_PATH}?domain=evil.example.com")
            empty, _ = await _get(port, ONDEMAND_ASK_PATH)  # no domain at all
            return ok, nope, empty
        finally:
            await proxy.stop()

    ok, nope, empty = _run(scenario())
    assert ok == 200  # Caddy is told to issue for a host we route
    assert nope == 404  # and refused for one we don't
    assert empty == 404  # a domain-less ask is refused too


def test_non_ask_request_with_no_route_is_502_not_404() -> None:
    # a normal request (not the ask path) for an unknown host still 502s as before —
    # the ask handling must not swallow ordinary routing.
    proxy = Proxy(lambda h: None)

    async def scenario():
        port = await proxy.start(0)
        try:
            return await _get(port, "/tasks", host="ghost.localhost")
        finally:
            await proxy.stop()

    status, _ = _run(scenario())
    assert status == 502

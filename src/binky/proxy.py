"""Embedded HTTP reverse proxy.

Routes ``<service>.<worktree>.<domain>`` to the worktree's port by reading the
request's ``Host`` header, then relays raw bytes both ways — so WebSockets (HMR),
SSE, chunked and streaming all just work (we only parse enough to route, then get
out of the way). HTTP only; HTTPS/HTTP2 is a future opt-in that delegates to a
bundled Caddy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from binky.https import ONDEMAND_ASK_PATH

Resolver = Callable[[str], "int | None"]  # hostname -> worktree port

_MAX_HEADER = 64 * 1024


def _host(header: bytes) -> str | None:
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"host:"):
            return line[5:].strip().decode("latin1").split(":")[0]
    return None


def _request_target(header: bytes) -> str:
    """The path+query from the request line (``GET <target> HTTP/1.1``)."""
    line = header.split(b"\r\n", 1)[0].decode("latin1")
    parts = line.split(" ")
    return parts[1] if len(parts) > 1 else ""


def _ondemand_domain(target: str) -> str | None:
    """If ``target`` is Caddy's on-demand ask, the ``domain`` it's asking about.

    Caddy hits ``GET <ask-url>?domain=<sni>`` before issuing a cert for ``<sni>``;
    returns the domain when this is that request, else ``None`` (a normal request)."""
    split = urlsplit(target)
    if split.path != ONDEMAND_ASK_PATH:
        return None
    domain = parse_qs(split.query).get("domain", [""])[0]
    return domain or ""


def _http_error(status: str, message: str) -> bytes:
    body = message.encode()
    return (
        f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
        f"Content-Type: text/plain\r\nConnection: close\r\n\r\n"
    ).encode() + body


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(ConnectionError, OSError):
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    with contextlib.suppress(Exception):
        writer.close()


class Proxy:
    def __init__(self, resolver: Resolver) -> None:
        self._resolve = resolver
        self._server: asyncio.AbstractServer | None = None

    async def start(self, port: int) -> int:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=port)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        try:
            header = await self._read_head(creader)
            if not header:
                return
            domain = _ondemand_domain(_request_target(header))
            if domain is not None:
                # Caddy asking whether to mint an on-demand cert for `domain`: authorize
                # only names we actually route, so an attacker can't make Caddy issue for
                # arbitrary SNIs. 200 = go ahead, anything else = refuse.
                ok = bool(domain) and self._resolve(domain) is not None
                status = "200 OK" if ok else "404 Not Found"
                cwriter.write(_http_error(status, "ok" if ok else f"no route for {domain}"))
                await cwriter.drain()
                return
            host = _host(header)
            port = self._resolve(host) if host else None
            if port is None:
                cwriter.write(_http_error("502 Bad Gateway", f"no route for {host}"))
                await cwriter.drain()
                return
            try:
                treader, twriter = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                cwriter.write(_http_error("502 Bad Gateway", f"{host} is not listening"))
                await cwriter.drain()
                return
            twriter.write(header)  # forward the request head (+ any body bytes already read)
            await twriter.drain()
            await asyncio.gather(_pump(creader, twriter), _pump(treader, cwriter))
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                cwriter.close()

    async def _read_head(self, reader: asyncio.StreamReader) -> bytes | None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(4096)
            if not chunk:
                return data or None
            data += chunk
            if len(data) > _MAX_HEADER:
                return None
        return data

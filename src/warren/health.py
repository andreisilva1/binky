"""Service health checks.

`depends_on` means "start me only after my dependencies are healthy". A health
check is one of:

- **http** — a GET that returns < 500
- **tcp** — a successful connection to a port
- **cmd** — a shell command that exits 0
- **default** (none declared) — TCP to the service's primary port, or instantly
  healthy if the service has no port

Health strings/fields are token-resolved first (`${self.port}` etc).
"""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.error
import urllib.request

from warren import resolve
from warren.config import Service

DEFAULT_TIMEOUT = 30.0


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # local http only
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _tcp_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _cmd_ok(command: str, cwd: str) -> bool:
    result = subprocess.run(command, cwd=cwd, shell=True, capture_output=True, check=False)
    return result.returncode == 0


def _parse_duration(value: str | None, default: float) -> float:
    if not value:
        return default
    text = value.strip().lower()
    try:
        if text.endswith("ms"):
            return float(text[:-2]) / 1000
        if text.endswith("s"):
            return float(text[:-1])
        if text.endswith("m"):
            return float(text[:-1]) * 60
        return float(text)
    except ValueError:
        return default


def timeout_for(svc: Service) -> float:
    health = svc.health
    if health is not None and not isinstance(health, str):
        return _parse_duration(health.timeout, DEFAULT_TIMEOUT)
    return DEFAULT_TIMEOUT


def check_once(
    service: str, svc: Service, ports: dict[tuple[str, str], int], worktree: str, cwd: str
) -> bool:
    health = svc.health
    if health is None:
        port = resolve.primary_port(ports, service)
        return True if port is None else _tcp_ok(port)
    if isinstance(health, str):
        return _http_ok(resolve.resolve(health, service, ports, worktree))
    if health.http:
        return _http_ok(resolve.resolve(health.http, service, ports, worktree))
    if health.tcp:
        value = resolve.resolve(health.tcp, service, ports, worktree)
        try:
            return _tcp_ok(int(value))
        except ValueError:
            return False
    if health.cmd:
        return _cmd_ok(resolve.resolve(health.cmd, service, ports, worktree), cwd)
    return True


def wait_healthy(
    service: str,
    svc: Service,
    ports: dict[tuple[str, str], int],
    worktree: str,
    cwd: str,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check_once(service, svc, ports, worktree, cwd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)

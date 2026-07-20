"""Resolve ``${...}`` tokens and build each service's environment (THESIS §8.1).

Every process boots already knowing the whole port map — its own ports and its
dependencies' — so nobody discovers a port at runtime. Supported tokens:

- ``${self.port}`` / ``${self.<label>}`` — this service's primary / named port
- ``${<svc>.port}`` / ``${<svc>.url}`` — another service's primary port / URL
- ``${worktree}`` — the worktree name

The URL form is ``http://localhost:<port>`` for now; the ``*.localhost`` proxy
lands in Phase 9.
"""

from __future__ import annotations

import re

from warren.config import Service

# ports for the whole worktree graph: (service, label) -> port
PortMap = dict[tuple[str, str], int]

_TOKEN = re.compile(r"\$\{([^}]+)\}")


def _primary(ports: PortMap, service: str) -> int | None:
    owned = {label: port for (svc, label), port in ports.items() if svc == service}
    if "" in owned:
        return owned[""]
    return next(iter(owned.values()), None)


def primary_port(ports: PortMap, service: str) -> int | None:
    """A service's primary port (label ``""``), else its first, else ``None``."""
    return _primary(ports, service)


def resolve(text: str, service: str, ports: PortMap, worktree: str) -> str:
    """Substitute ``${...}`` tokens in *text*. Unknown tokens are left untouched."""

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        if expr == "worktree":
            return worktree
        if "." not in expr:
            return match.group(0)
        obj, attr = expr.split(".", 1)
        target = service if obj == "self" else obj
        if attr == "url":
            port = _primary(ports, target)
            return f"http://localhost:{port}" if port is not None else match.group(0)
        # "port" = primary; anything else is a named port, e.g. ${self.grpc}
        port = _primary(ports, target) if attr == "port" else ports.get((target, attr))
        return str(port) if port is not None else match.group(0)

    return _TOKEN.sub(replace, text)


def build_env(service_name: str, service: Service, ports: PortMap, worktree: str) -> dict[str, str]:
    """The environment injected into *service*'s processes."""
    env: dict[str, str] = {"WARREN_WORKTREE": worktree}
    primary = _primary(ports, service_name)
    if primary is not None:
        env["PORT"] = str(primary)
    for (svc, label), port in ports.items():
        if svc == service_name and label:
            env[f"PORT_{label.upper()}"] = str(port)
    for key, value in service.env.items():
        env[key] = resolve(value, service_name, ports, worktree)
    return env


def resolve_command(service_name: str, service: Service, ports: PortMap, worktree: str) -> str:
    """The service's ``cmd`` with tokens resolved (e.g. ``--port ${self.port}``)."""
    return resolve(service.cmd or "", service_name, ports, worktree)


def port_requests(service_name: str, service: Service) -> list[tuple[str, str]]:
    """Which ports a service needs: its declared ``ports``, or one primary if it has a URL."""
    if service.ports is not None:
        # list(...) yields items for a list and keys for a dict — both are the labels
        return [(service_name, label) for label in service.ports]
    if service.url:
        return [(service_name, "")]
    return []

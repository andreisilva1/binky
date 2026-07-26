"""Parse and validate ``binky.toml`` into typed models.

The whole schema is modelled here (it matches the annotated `binky.toml`).
Fields that belong to v2 features (`sync`, `clone_from`,
`agents`, `replicas`, …) are parsed and validated now so the file format is
stable — even though the behaviour lands later. Unknown fields are rejected.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


class ConfigError(Exception):
    """Raised when a ``binky.toml`` is missing, malformed, or invalid."""


class _Base(BaseModel):
    # Reject unknown keys everywhere → typos and stale fields fail loudly.
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
#  enums
# --------------------------------------------------------------------------- #
class Restart(StrEnum):
    on_failure = "on-failure"
    always = "always"
    never = "never"


class SyncMode(StrEnum):
    off = "off"
    notify = "notify"
    cdc = "cdc"
    reactive = "reactive"


# --------------------------------------------------------------------------- #
#  small value objects
# --------------------------------------------------------------------------- #
class Limits(_Base):
    cpu: str | None = None
    memory: str | None = None


class HealthCheck(_Base):
    """The table form of ``health`` (the string form is an http URL shorthand)."""

    http: str | None = None
    tcp: str | None = None
    cmd: str | None = None
    timeout: str | None = None

    @model_validator(mode="after")
    def _one_probe(self) -> HealthCheck:
        if not (self.http or self.tcp or self.cmd):
            raise ValueError("health must define one of: http, tcp, cmd")
        return self


Health = str | HealthCheck
PortSpec = str | int
Ports = list[str] | dict[str, PortSpec]
UrlSpec = bool | str | dict[str, str]


# --------------------------------------------------------------------------- #
#  sections
# --------------------------------------------------------------------------- #
class Project(_Base):
    name: str
    domain: str | None = None  # None → derived as "<name>.localhost" at runtime
    port_range: tuple[int, int] | None = None
    https: bool = False  # front the proxy with Caddy (TLS via a local CA) when available

    @model_validator(mode="after")
    def _valid_range(self) -> Project:
        # RAM/disk limits are machine-global (a singleton daemon), not per-project — they
        # live in the settings store (`binky reserve`), never in a project's binky.toml.
        if self.port_range and self.port_range[0] >= self.port_range[1]:
            raise ValueError("port_range must be [low, high] with low < high")
        return self


class Defaults(_Base):
    workdir: str = "."
    restart: Restart = Restart.on_failure
    limits: Limits | None = None


class Service(_Base):
    # --- process (has `cmd`) ---
    workdir: str | None = None
    cmd: str | None = None
    setup: str | None = None
    restart: Restart | None = None
    limits: Limits | None = None
    url: UrlSpec = False
    ports: Ports | None = None
    health: Health | None = None
    depends_on: list[str] = []
    watch: list[str] = []
    env: dict[str, str] = {}
    env_file: list[str] = []
    replicas: int = 1
    shared: bool = False  # singleton across worktrees (process or resource)

    # --- resource (has `adapter`) ---
    adapter: str | None = None
    admin_url: str | None = None
    migrate: str | None = None
    seed: str | None = None
    clone_from: str | None = None
    sync: SyncMode = SyncMode.off
    buckets: list[str] = []
    # Free-form, per-adapter settings. Core never interprets these — they are handed
    # to the adapter verbatim (after `${env.X}` resolution), which is what keeps new
    # adapters from needing a schema change here. Values are strings so the same
    # table serialises unchanged over the external adapters' JSON protocol.
    options: dict[str, str] = {}

    @model_validator(mode="after")
    def _coherent(self) -> Service:
        if bool(self.cmd) == bool(self.adapter):
            raise ValueError("a service needs exactly one of `cmd` or `adapter`")
        resource_only = {
            "admin_url": self.admin_url,
            "migrate": self.migrate,
            "seed": self.seed,
            "clone_from": self.clone_from,
            "buckets": self.buckets or None,
            "options": self.options or None,
        }
        for field, value in resource_only.items():
            if value and not self.adapter:
                raise ValueError(f"`{field}` is only valid on an adapter (resource) service")
        if self.sync is not SyncMode.off and not self.shared:
            raise ValueError("`sync` requires `shared = true`")
        if self.replicas < 1:
            raise ValueError("replicas must be >= 1")
        return self


class Agents(_Base):
    harness: str | None = None
    expose: list[str] = []
    verify: list[str] = []


class Hooks(_Base):
    post_up: str | None = None
    pre_down: str | None = None


class BinkyConfig(_Base):
    project: Project
    defaults: Defaults = Defaults()
    services: dict[str, Service] = {}
    groups: dict[str, list[str] | str] = {}
    agents: Agents | None = None
    hooks: Hooks | None = None

    @model_validator(mode="after")
    def _cross_refs(self) -> BinkyConfig:
        names = set(self.services)
        for svc, spec in self.services.items():
            for dep in spec.depends_on:
                if dep not in names:
                    raise ValueError(
                        f"service '{svc}' depends_on unknown service '{dep}'"
                    )
        for group, members in self.groups.items():
            if members == "*":
                continue
            if isinstance(members, str):
                raise ValueError(f"group '{group}' must be a list or \"*\"")
            for member in members:
                if member not in names:
                    raise ValueError(
                        f"group '{group}' references unknown service '{member}'"
                    )
        return self


# --------------------------------------------------------------------------- #
#  loading
# --------------------------------------------------------------------------- #
def _format_errors(exc: ValidationError) -> str:
    lines = ["invalid binky.toml:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  • {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config_data(data: dict) -> BinkyConfig:
    """Validate already-parsed toml data. Raises :class:`ConfigError`."""
    try:
        return BinkyConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from exc


def security_warnings(cfg: BinkyConfig) -> list[str]:
    """Non-fatal footguns worth flagging (e.g. a secret written inline in the toml)."""
    out: list[str] = []
    for name, svc in cfg.services.items():
        inline = svc.adapter and svc.admin_url and "${" not in svc.admin_url
        if inline and urlparse(svc.admin_url).password:
            out.append(
                f"service '{name}': admin_url has an inline credential — prefer "
                "${env.NAME} or an env_file so the secret isn't committed"
            )
    return out


_SELF_TOKEN = re.compile(r"\$\{self\.[^}]*\}")


def _token_sites(svc: Service) -> Iterator[str]:
    """Every string of a service that goes through token resolution."""
    yield svc.cmd or ""
    yield from svc.env.values()
    if isinstance(svc.health, str):
        yield svc.health
    elif svc.health is not None:
        yield from (probe for probe in (svc.health.http, svc.health.tcp, svc.health.cmd) if probe)


def config_warnings(cfg: BinkyConfig) -> list[str]:
    """Everything ``binky check`` flags: the security footguns plus config that can't work."""
    out = security_warnings(cfg)
    for name, svc in cfg.services.items():
        # Asking for a port is explicit: `ports`, or a `url` that needs one to point at.
        # A service with neither is allocated none, which is right for a worker and wrong
        # the moment it writes ${self.…}. Unknown tokens are left verbatim rather than
        # blanked, so nothing fails at resolve time — the literal `${self.port}` reaches
        # the shell and the process dies parsing its own arguments. Catch it here instead.
        if svc.ports is not None or svc.url:
            continue
        if any(_SELF_TOKEN.search(text) for text in _token_sites(svc)):
            out.append(
                f"service '{name}': uses ${{self....}} but declares neither `ports` nor "
                "`url`, so it gets no port and the token is left unresolved"
            )
    return out


def load_config(path: str | Path) -> BinkyConfig:
    """Read and validate a ``binky.toml`` file. Raises :class:`ConfigError`."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"no binky.toml found at {p}")
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} is not valid TOML: {exc}") from exc
    return load_config_data(data)

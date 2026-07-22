"""Generate a starting ``binky.toml`` from a project's docker-compose.yml and .env.

This is a *bootstrap*, not a converter. It reads what it can recognise, writes a
file with the reasoning left in as comments, and reports what it skipped — the
output is meant to be read and edited, not trusted blindly. Two rules it never
breaks:

- **the generated file is always valid.** Anything it can't express becomes a
  commented-out block with a TODO, never a half-filled service that would make
  ``binky check`` fail on a brand-new project.
- **no secret is ever copied.** A password sitting inline in a compose file
  becomes an ``${env.NAME}`` reference and a line in the report telling you what
  to export. The ``.env`` file is read for its *key names* only; its values are
  never opened, let alone written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

COMPOSE_NAMES = (
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
)

# Image basename → the adapter name Binky registers. Several images map to the
# same adapter (postgis and pgvector are Postgres with an extension preinstalled),
# and several adapters share one implementation — that's `adapters._LAZY`'s job,
# not ours. Order matters: the first substring match wins, so longer and more
# specific names come first.
IMAGE_ADAPTERS: tuple[tuple[str, str], ...] = (
    ("pgvector", "postgres"),
    ("postgis", "postgres"),
    ("timescale", "postgres"),
    ("postgres", "postgres"),
    ("mariadb", "mariadb"),
    ("percona", "percona"),
    ("mysql", "mysql"),
    ("mongo", "mongodb"),
    ("valkey", "valkey"),
    ("redis", "redis"),
    ("minio", "s3"),
    ("rabbitmq", "rabbitmq"),
    ("redpanda", "redpanda"),
    ("kafka", "kafka"),
    ("qdrant", "qdrant"),
    ("clickhouse", "clickhouse"),
    ("opensearch", "opensearch"),
    ("elasticsearch", "elasticsearch"),
)

# The env var each adapter's admin_url is looked for under, and the scheme to
# suggest when the project has no such variable yet.
ADAPTER_HINTS: dict[str, tuple[tuple[str, ...], str]] = {
    "postgres": (("DATABASE_URL", "POSTGRES_URL", "PG_ADMIN_URL"), "postgres://user:pw@host:5432/postgres"),
    "mysql": (("DATABASE_URL", "MYSQL_URL", "MYSQL_ADMIN_URL"), "mysql://root:pw@host:3306"),
    "mariadb": (("DATABASE_URL", "MYSQL_URL", "MARIADB_ADMIN_URL"), "mysql://root:pw@host:3306"),
    "percona": (("DATABASE_URL", "MYSQL_URL", "MYSQL_ADMIN_URL"), "mysql://root:pw@host:3306"),
    "mongodb": (("MONGO_URL", "MONGODB_URI", "MONGO_ADMIN_URL"), "mongodb://host:27017"),
    "redis": (("REDIS_URL", "REDIS_ADMIN_URL"), "redis://host:6379"),
    "valkey": (("REDIS_URL", "VALKEY_URL"), "redis://host:6379"),
    "s3": (("S3_ENDPOINT", "AWS_ENDPOINT_URL", "MINIO_URL"), "http://host:9000"),
    "rabbitmq": (("RABBITMQ_URL", "RABBITMQ_ADMIN_URL"), "http://user:pw@host:15672"),
    "kafka": (("KAFKA_URL", "KAFKA_BROKERS"), "kafka://host:9092"),
    "redpanda": (("KAFKA_URL", "REDPANDA_URL"), "kafka://host:9092"),
    "qdrant": (("QDRANT_URL",), "http://host:6333"),
    "clickhouse": (("CLICKHOUSE_URL",), "http://default:@host:8123"),
    "elasticsearch": (("ELASTICSEARCH_URL", "ES_URL"), "http://host:9200"),
    "opensearch": (("OPENSEARCH_URL", "ES_URL"), "http://host:9200"),
}

# Adapters that can copy a prepared slice, so `clone_from` is worth suggesting.
# Kept here rather than asked of the adapter, because `init` runs before anything
# is installed and must not import a driver to write a comment.
CLONE_CAPABLE = frozenset({"postgres", "mysql", "mariadb", "percona", "mongodb"})

# Compose keys Binky has no equivalent for. Named so the report can say what it
# dropped — silence here is how a generated file earns undeserved trust.
IGNORED_KEYS = ("volumes", "networks", "healthcheck", "deploy", "profiles")

_SAFE_NAME = re.compile(r"[^a-z0-9_]+")


class InitError(Exception):
    """Raised when there is nothing to generate from."""


@dataclass
class Finding:
    """One compose service, classified."""

    name: str
    adapter: str | None = None
    cmd: str | None = None
    has_ports: bool = False
    depends_on: list[str] = field(default_factory=list)
    image: str | None = None
    ignored: list[str] = field(default_factory=list)


@dataclass
class Result:
    toml: str
    notes: list[str] = field(default_factory=list)


def _identifier(raw: str) -> str:
    """A service/project name Binky and TOML are both happy with."""
    name = _SAFE_NAME.sub("_", raw.strip().lower()).strip("_")
    return name or "app"


def find_compose(root: Path) -> Path | None:
    for candidate in COMPOSE_NAMES:
        path = root / candidate
        if path.is_file():
            return path
    return None


def env_keys(root: Path) -> list[str]:
    """The key names defined in ``.env`` — names only, never values.

    Reading the values would mean a secret could reach the generated file by
    accident, so this deliberately splits and discards the right-hand side.
    """
    path = root / ".env"
    if not path.is_file():
        return []
    keys = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].removeprefix("export ").strip()
        if key:
            keys.append(key)
    return keys


def adapter_for(image: str) -> str | None:
    base = image.split("/")[-1].split(":")[0].lower()
    for needle, adapter in IMAGE_ADAPTERS:
        if needle in base:
            return adapter
    return None


def _command_of(service: dict) -> str | None:
    """Compose's ``command:``, in either its string or list form."""
    command = service.get("command")
    if isinstance(command, str):
        return command.strip() or None
    if isinstance(command, list):
        return " ".join(str(part) for part in command) or None
    return None


def read_compose(path: Path) -> list[Finding]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise InitError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise InitError(f"{path.name} does not look like a compose file")

    services = data.get("services")
    if not isinstance(services, dict):
        raise InitError(f"{path.name} has no `services:` block")

    findings = []
    for raw_name, service in services.items():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        depends = service.get("depends_on")
        # depends_on has a short list form and a long mapping form; both are keyed
        # by service name, so iterating reads correctly for either.
        depends_names = (
            [_identifier(str(d)) for d in depends] if isinstance(depends, (list, dict)) else []
        )
        findings.append(
            Finding(
                name=_identifier(str(raw_name)),
                adapter=adapter_for(str(image)) if isinstance(image, str) else None,
                cmd=_command_of(service),
                has_ports=bool(service.get("ports")),
                depends_on=depends_names,
                image=image if isinstance(image, str) else None,
                ignored=[key for key in IGNORED_KEYS if key in service],
            )
        )
    return findings


def _admin_url_line(adapter: str, keys: list[str]) -> tuple[str, str | None]:
    """The ``admin_url`` value for *adapter*, plus a note if the user must export it."""
    candidates, example = ADAPTER_HINTS.get(adapter, ((), "scheme://host:port"))
    for candidate in candidates:
        if candidate in keys:
            return f'"${{env.{candidate}}}"', None
        # A prefixed variant (`BINKY_DATABASE_URL`) counts too.
        matches = [k for k in keys if k.endswith(candidate)]
        if matches:
            return f'"${{env.{matches[0]}}}"', None
    fallback = candidates[-1] if candidates else f"{adapter.upper()}_ADMIN_URL"
    return f'"${{env.{fallback}}}"', f"export {fallback}={example}"


def render(project: str, findings: list[Finding], keys: list[str], source: str) -> Result:
    notes: list[str] = []
    lines = [
        f"# Generated by `binky init` from {source}.",
        "# Read it before you run it — this is a starting point, not a conversion.",
        "[project]",
        f'name = "{project}"',
        "",
    ]

    adapters = [f for f in findings if f.adapter]
    processes = [f for f in findings if not f.adapter]

    if adapters:
        lines += [
            "# Backing services. Binky gives each worktree its own slice of these —",
            "# a database, a key prefix, a bucket — on one shared server.",
            "",
        ]
    suggested_clone = False
    for finding in adapters:
        value, note = _admin_url_line(finding.adapter, keys)
        if note:
            notes.append(f"service '{finding.name}': set the admin URL — {note}")
        lines += [
            f"[services.{finding.name}]",
            f'adapter = "{finding.adapter}"',
            f"admin_url = {value}",
        ]
        if finding.depends_on:
            lines.append(f"depends_on = {_toml_list(finding.depends_on)}")
        # Suggested once, on the first adapter that can do it: repeating this on
        # every service would turn the most useful comment in the file into noise.
        if finding.adapter in CLONE_CAPABLE and not suggested_clone:
            suggested_clone = True
            lines += [
                "#   Migrating and seeding every worktree from scratch gets slow. Fill",
                "#   these in and Binky runs them once, into a template it copies:",
                '# migrate = "npm run migrate"',
                '# seed = "npm run seed"',
                '# clone_from = "golden"',
            ]
        lines.append("")

    for finding in processes:
        if finding.cmd:
            lines += [f"[services.{finding.name}]", f'cmd = "{_escape(finding.cmd)}"']
            if finding.has_ports:
                lines.append(f'url = "{finding.name}"')
            if finding.depends_on:
                lines.append(f"depends_on = {_toml_list(finding.depends_on)}")
            wiring = _env_line(finding, adapters)
            if wiring:
                lines.append(wiring)
            lines.append("")
        else:
            # Compose says how to run this in a container; Binky runs it on the
            # host, and those are rarely the same command. Guessing here would
            # produce a binky.toml that looks right and doesn't work, so the
            # block is commented out with the one thing only you can fill in.
            notes.append(f"service '{finding.name}': needs a `cmd` — filled in as a TODO")
            lines += [
                f"# [services.{finding.name}]",
                "#   compose builds this from source and declares no command; Binky",
                "#   runs it on the host, so only you know what that command is.",
                "#   Uncomment and fill it in:",
                '# cmd = "..."   # TODO',
            ]
            if finding.has_ports:
                lines.append(f'# url = "{finding.name}"')
            if finding.depends_on:
                lines.append(f"# depends_on = {_toml_list(finding.depends_on)}")
            lines.append("")

    if not adapters and not processes:
        raise InitError("no services found in the compose file")

    exposed = [f.name for f in findings if f.adapter or f.cmd]
    if exposed:
        lines += [
            "# What `binky env <worktree>` hands to an agent (or to you).",
            "[agents]",
            f"expose = {_toml_list(exposed)}",
            "",
        ]

    return Result(toml="\n".join(lines).rstrip() + "\n", notes=notes)


def _env_line(finding: Finding, adapters: list[Finding]) -> str | None:
    """Wire a process service to the adapters it declares a dependency on."""
    names = {a.name for a in adapters}
    wired = [d for d in finding.depends_on if d in names]
    if not wired:
        return None
    pairs = ", ".join(f'{d.upper()}_URL = "${{{d}.url}}"' for d in wired)
    return f"env = {{ {pairs} }}"


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def generate(root: Path) -> Result:
    """Build a ``binky.toml`` for the project rooted at *root*."""
    compose = find_compose(root)
    if compose is None:
        raise InitError(
            "no compose file found — looked for "
            + ", ".join(COMPOSE_NAMES)
            + f" in {root}"
        )
    findings = read_compose(compose)
    keys = env_keys(root)
    result = render(_identifier(root.resolve().name), findings, keys, compose.name)
    # In IGNORED_KEYS order, not alphabetical — it reads as a stable list rather
    # than one that reshuffles as a project's compose file grows.
    seen = {key for finding in findings for key in finding.ignored}
    dropped = [key for key in IGNORED_KEYS if key in seen]
    if dropped:
        result.notes.append(
            f"{compose.name}: {', '.join(dropped)} not carried over — Binky has no "
            "equivalent, so they stay yours to manage"
        )
    if keys:
        result.notes.append(f".env: read {len(keys)} key name(s); no value was copied")
    return result

"""Tests for the binky.toml config model + loader."""

from pathlib import Path

import pytest

from binky.config import ConfigError, SyncMode, config_warnings, load_config, load_config_data

VALID = """
[project]
name = "acme"
domain = "acme.localhost"
port_range = [4000, 4999]

[defaults]
restart = "on-failure"
limits = { cpu = "1.0", memory = "1Gi" }

[services.web]
workdir = "apps/web"
cmd = "pnpm dev --port ${self.port}"
setup = "pnpm install"
url = "web"
depends_on = ["api"]
health = "http://localhost:${self.port}"

[services.web.env]
NEXT_PUBLIC_API = "${api.url}"

[services.api]
cmd = "go run ./cmd/server"
ports = { http = "auto", grpc = "auto" }
url = { http = "api" }
depends_on = ["db"]
health = { tcp = "${self.grpc}", timeout = "30s" }

[services.db]
adapter = "postgres@16"
admin_url = "${env.PG_ADMIN}"
migrate = "make migrate"
clone_from = "snapshot:nightly"

[services.mail]
cmd = "mailhog"
url = "mail"
shared = true

[groups]
backend = ["api", "db"]
full = "*"

[agents]
harness = "claude-code"
verify = ["pnpm test"]

[hooks]
post_up = "echo ${worktree}"
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "binky.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_full_schema(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.project.name == "acme"
    assert cfg.project.port_range == (4000, 4999)
    assert set(cfg.services) == {"web", "api", "db", "mail"}
    assert cfg.services["db"].adapter == "postgres@16"
    assert cfg.services["web"].env["NEXT_PUBLIC_API"] == "${api.url}"
    assert cfg.groups["full"] == "*"


def test_unknown_field_rejected() -> None:
    with pytest.raises(ConfigError, match="whatever"):
        load_config_data({"project": {"name": "x"}, "services": {"a": {"cmd": "x", "whatever": 1}}})


def test_service_needs_cmd_xor_adapter() -> None:
    with pytest.raises(ConfigError, match="exactly one of"):
        load_config_data({"project": {"name": "x"}, "services": {"a": {}}})
    with pytest.raises(ConfigError, match="exactly one of"):
        load_config_data(
            {"project": {"name": "x"}, "services": {"a": {"cmd": "x", "adapter": "postgres"}}}
        )


def test_resource_field_requires_adapter() -> None:
    with pytest.raises(ConfigError, match="migrate"):
        load_config_data(
            {"project": {"name": "x"}, "services": {"a": {"cmd": "x", "migrate": "make m"}}}
        )


def test_sync_requires_shared() -> None:
    with pytest.raises(ConfigError, match="requires `shared"):
        load_config_data(
            {"project": {"name": "x"}, "services": {"db": {"adapter": "redis", "sync": "reactive"}}}
        )


def test_depends_on_unknown_service() -> None:
    with pytest.raises(ConfigError, match="unknown service 'ghost'"):
        load_config_data(
            {"project": {"name": "x"}, "services": {"a": {"cmd": "x", "depends_on": ["ghost"]}}}
        )


def test_group_unknown_member() -> None:
    with pytest.raises(ConfigError, match="group 'g'"):
        load_config_data(
            {"project": {"name": "x"}, "services": {"a": {"cmd": "x"}}, "groups": {"g": ["ghost"]}}
        )


def test_bad_port_range() -> None:
    with pytest.raises(ConfigError, match="low < high"):
        load_config_data({"project": {"name": "x", "port_range": [5000, 4000]}})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"no binky\.toml"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(_write(tmp_path, "this is = = not toml"))


def test_sync_default_off() -> None:
    cfg = load_config_data({"project": {"name": "x"}, "services": {"a": {"cmd": "x"}}})
    assert cfg.services["a"].sync is SyncMode.off


def test_self_token_without_a_port_is_warned() -> None:
    """The exact config the landing page shipped, which could never have worked.

    `api` asks for no port — neither `ports` nor `url` — so ${self.port} resolves to
    nothing. Unknown tokens are left verbatim rather than blanked, so the literal
    string reaches the shell and the process dies parsing its own arguments. Nothing
    else in the pipeline complains, which is what makes this `check`'s job.
    """
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {
                "api": {
                    "cmd": "uv run api --port ${self.port}",
                    "health": "http://localhost:${self.port}/healthz",
                }
            },
        }
    )
    warnings = config_warnings(cfg)
    assert any("api" in w and "no port" in w for w in warnings), warnings


@pytest.mark.parametrize(
    "extra",
    [
        {"url": "api"},  # a url needs a port to point at
        {"url": True},
        {"ports": ["http"]},  # ${self.port} falls back to the first declared port
        {"ports": {"http": 0}},
    ],
    ids=["url-name", "url-true", "ports-list", "ports-table"],
)
def test_a_service_that_asks_for_a_port_is_not_warned(extra: dict) -> None:
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {"api": {"cmd": "uv run api --port ${self.port}", **extra}},
        }
    )
    assert config_warnings(cfg) == []


def test_a_portless_service_without_the_token_is_left_alone() -> None:
    """No port is the right answer for a worker; only the unresolvable token is a bug."""
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {"worker": {"cmd": "celery -A app worker", "env": {"Q": "${worktree}"}}},
        }
    )
    assert config_warnings(cfg) == []


def test_the_token_is_found_in_env_and_in_a_health_table() -> None:
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {
                "a": {"cmd": "run", "env": {"SELF": "http://localhost:${self.port}"}},
                "b": {"cmd": "run", "health": {"tcp": "localhost:${self.grpc}"}},
            },
        }
    )
    warned = {w.split("'")[1] for w in config_warnings(cfg)}
    assert warned == {"a", "b"}

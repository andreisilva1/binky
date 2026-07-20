"""Tests for token resolution + environment building."""

from warren import resolve
from warren.config import load_config_data

PORTS = {("web", ""): 4110, ("api", "http"): 4111, ("api", "grpc"): 4200}


def test_resolve_tokens() -> None:
    assert resolve.resolve("--port ${self.port}", "web", PORTS, "login-fix") == "--port 4110"
    assert resolve.resolve("${api.url}", "web", PORTS, "x") == "http://localhost:4111"
    assert resolve.resolve("${api.port}", "web", PORTS, "x") == "4111"
    assert resolve.resolve("${self.grpc}", "api", PORTS, "x") == "4200"
    assert resolve.resolve("wt=${worktree}", "web", PORTS, "login-fix") == "wt=login-fix"


def test_unknown_token_left_untouched() -> None:
    assert resolve.resolve("${nope.thing}", "web", PORTS, "x") == "${nope.thing}"
    assert resolve.resolve("${self.missing}", "web", PORTS, "x") == "${self.missing}"


def _cfg():
    return load_config_data(
        {
            "project": {"name": "acme"},
            "services": {
                "web": {
                    "cmd": "srv --port ${self.port}",
                    "url": "web",
                    "env": {"API": "${api.url}"},
                },
                "api": {"cmd": "run", "ports": ["http", "grpc"]},
                "worker": {"cmd": "run"},
            },
        }
    )


def test_build_env() -> None:
    cfg = _cfg()
    env = resolve.build_env("web", cfg.services["web"], PORTS, "login-fix")
    assert env["PORT"] == "4110"
    assert env["API"] == "http://localhost:4111"
    assert env["WARREN_WORKTREE"] == "login-fix"

    env_api = resolve.build_env("api", cfg.services["api"], PORTS, "login-fix")
    assert env_api["PORT"] == "4111"  # primary = first declared port
    assert env_api["PORT_HTTP"] == "4111"
    assert env_api["PORT_GRPC"] == "4200"


def test_resolve_command() -> None:
    cfg = _cfg()
    cmd = resolve.resolve_command("web", cfg.services["web"], PORTS, "wt")
    assert cmd == "srv --port 4110"


def test_port_requests() -> None:
    cfg = _cfg()
    assert resolve.port_requests("web", cfg.services["web"]) == [("web", "")]
    assert resolve.port_requests("api", cfg.services["api"]) == [("api", "http"), ("api", "grpc")]
    assert resolve.port_requests("worker", cfg.services["worker"]) == []

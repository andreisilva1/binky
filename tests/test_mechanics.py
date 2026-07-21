"""Tests for config mechanics: groups, env_file, setup idempotency."""

import sys
from pathlib import Path

import pytest

from warren import ops
from warren.config import load_config_data
from warren.rpc import RpcFault


def test_select_services_group_closure() -> None:
    cfg = load_config_data(
        {
            "project": {"name": "x"},
            "services": {
                "web": {"cmd": "r", "depends_on": ["api"]},
                "api": {"cmd": "r", "depends_on": ["db"]},
                "db": {"adapter": "postgres", "admin_url": "x"},
                "worker": {"cmd": "r"},
            },
            "groups": {"front": ["web"], "all": "*"},
        }
    )
    assert set(ops._select_services(cfg, "front")) == {"web", "api", "db"}  # transitive deps
    assert set(ops._select_services(cfg, "all")) == {"web", "api", "db", "worker"}
    assert set(ops._select_services(cfg, None)) == {"web", "api", "db", "worker"}


def test_select_services_unknown_group() -> None:
    cfg = load_config_data({"project": {"name": "x"}, "services": {"a": {"cmd": "r"}}})
    with pytest.raises(RpcFault, match="no group"):
        ops._select_services(cfg, "nope")


def test_load_env_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('A=1\nB="two"\n# comment\nBAD_LINE\n', encoding="utf-8")
    assert ops._load_env_files([".env"], str(tmp_path)) == {"A": "1", "B": "two"}
    assert ops._load_env_files(["missing.env"], str(tmp_path)) == {}


def test_run_setup_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    counter = tmp_path / "count.txt"
    (tmp_path / "mark.py").write_text(f"open(r'{counter}', 'a').write('x')", encoding="utf-8")
    cfg = load_config_data(
        {
            "project": {"name": "demo"},
            "services": {"x": {"cmd": "r", "setup": f'"{sys.executable}" mark.py'}},
        }
    )
    svc = cfg.services["x"]
    ops._run_setup(svc, "x", "demo", "feat", str(tmp_path), {})
    ops._run_setup(svc, "x", "demo", "feat", str(tmp_path), {})  # same command → skipped
    assert counter.read_text() == "x"  # ran exactly once

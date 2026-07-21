"""Tests for the resource-adapter contract and its wiring into up/remove.

The Postgres adapter itself only runs with a real server (set ``WARREN_TEST_PG``
to an admin URL); the wiring is proven with an in-process fake adapter.
"""

import os
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from warren import adapters, ops, resolve
from warren.config import load_config_data
from warren.registry import Registry
from warren.supervisor import ProcessManager


class FakeAdapter(adapters.Adapter):
    store: ClassVar[set[str]] = set()

    def provision(self, slice_name: str) -> None:
        FakeAdapter.store.add(slice_name)

    def resolve(self, slice_name: str) -> str:
        return f"fake://{slice_name}"

    def teardown(self, slice_name: str) -> None:
        FakeAdapter.store.discard(slice_name)


@pytest.fixture(autouse=True)
def _fake_adapter():
    adapters.register("fake", FakeAdapter)
    FakeAdapter.store.clear()
    yield


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_adapter_provision_and_teardown(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "warren.toml").write_text(
        '[project]\nname = "acme"\n[services.db]\nadapter = "fake"\n', encoding="utf-8"
    )
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "i")

    reg = Registry.connect(tmp_path / "home" / "warren.db")
    pm = ProcessManager()
    try:
        ops.up(reg, pm, {"root": str(proj), "worktree": "feat"})
        assert "acme_feat" in FakeAdapter.store  # slice provisioned
        wt = reg.get_worktree("acme", "feat")
        slices = reg.slices_for(wt.id)
        assert len(slices) == 1
        assert slices[0].service == "db"
        assert slices[0].address == "fake://acme_feat"

        ops.remove(reg, pm, {"root": str(proj), "worktree": "feat"})
        assert "acme_feat" not in FakeAdapter.store  # slice torn down
        assert reg.get_worktree("acme", "feat") is None
    finally:
        pm.stop_all()
        reg.close()


def test_adapter_address_injected_into_env() -> None:
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {
                "db": {"adapter": "fake"},
                "api": {"cmd": "run", "env": {"DATABASE_URL": "${db.url}"}},
            },
        }
    )
    env = resolve.build_env("api", cfg.services["api"], {}, "feat", {"db": "fake://acme_feat"})
    assert env["DATABASE_URL"] == "fake://acme_feat"


def test_env_token(monkeypatch) -> None:
    monkeypatch.setenv("WARREN_SECRET", "sekret")
    assert resolve.resolve("${env.WARREN_SECRET}", "s", {}, "w") == "sekret"


def test_unknown_adapter() -> None:
    with pytest.raises(adapters.AdapterError, match="unknown adapter"):
        adapters.get("nope", {})


PG_URL = os.environ.get("WARREN_TEST_PG")


@pytest.mark.skipif(not PG_URL, reason="set WARREN_TEST_PG to a Postgres admin URL to run")
def test_postgres_adapter_roundtrip() -> None:
    adapter = adapters.get("postgres", {"admin_url": PG_URL})
    name = "warren_test_slice_zzz"
    adapter.teardown(name)  # clean slate
    adapter.provision(name)
    assert name in adapter.resolve(name)
    adapter.provision(name)  # idempotent
    adapter.teardown(name)

"""Tests for the resource-adapter contract and its wiring into up/remove.

The Postgres adapter itself only runs with a real server (set ``WARREN_TEST_PG``
to an admin URL); the wiring is proven with an in-process fake adapter.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from warren import adapters, ops, resolve
from warren.config import load_config_data
from warren.registry import Registry
from warren.supervisor import ProcessManager

_FILESTORE = Path(__file__).resolve().parents[1] / "examples" / "adapters" / "filestore.py"


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


def _init_git(proj: Path) -> None:
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "i")


def test_clone_from_builds_golden_once_then_clones(tmp_path, monkeypatch) -> None:
    """The golden is built once (migrate runs once); each worktree is a clone of it."""
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    counter = tmp_path / "migrate_runs.txt"
    monkeypatch.setenv("COUNTER", str(counter))

    provisioned: list[str] = []
    cloned: list[tuple[str, str]] = []

    class CloneFake(adapters.Adapter):
        CAPABILITIES: ClassVar[dict] = {"clone": True}
        store: ClassVar[set[str]] = set()

        def provision(self, slice_name: str) -> None:
            provisioned.append(slice_name)
            CloneFake.store.add(slice_name)

        def resolve(self, slice_name: str) -> str:
            return f"fake://{slice_name}"

        def teardown(self, slice_name: str) -> None:
            CloneFake.store.discard(slice_name)

        def clone(self, source_slice: str, dest_slice: str) -> None:
            cloned.append((source_slice, dest_slice))
            CloneFake.store.add(dest_slice)

    adapters.register("clonefake", CloneFake)
    CloneFake.store.clear()

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "bump.py").write_text(
        "import os\nopen(os.environ['COUNTER'], 'a').write('x')\n", encoding="utf-8"
    )
    py = sys.executable
    (proj / "warren.toml").write_text(
        '[project]\nname = "acme"\n'
        '[services.db]\n'
        'adapter = "clonefake"\n'
        'clone_from = "golden"\n'
        f"migrate = '\"{py}\" bump.py'\n",
        encoding="utf-8",
    )
    _init_git(proj)

    reg = Registry.connect(tmp_path / "home" / "warren.db")
    pm = ProcessManager()
    try:
        ops.up(reg, pm, {"root": str(proj), "worktree": "a"})
        ops.up(reg, pm, {"root": str(proj), "worktree": "b"})

        golden = "acme_golden_golden"
        assert provisioned == [golden]  # only the golden is provisioned
        assert cloned == [(golden, "acme_a"), (golden, "acme_b")]  # each worktree cloned from it
        assert counter.read_text(encoding="utf-8") == "x"  # migrate ran once, into the golden
    finally:
        pm.stop_all()
        reg.close()


def test_clone_from_falls_back_when_adapter_cannot_clone(tmp_path, monkeypatch) -> None:
    """`clone_from` on a clone-less adapter degrades to per-worktree provision+migrate."""
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    counter = tmp_path / "migrate_runs.txt"
    monkeypatch.setenv("COUNTER", str(counter))

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "bump.py").write_text(
        "import os\nopen(os.environ['COUNTER'], 'a').write('x')\n", encoding="utf-8"
    )
    py = sys.executable
    (proj / "warren.toml").write_text(
        '[project]\nname = "acme"\n'
        '[services.db]\n'
        'adapter = "fake"\n'  # FakeAdapter has no clone capability
        'clone_from = "golden"\n'
        f"migrate = '\"{py}\" bump.py'\n",
        encoding="utf-8",
    )
    _init_git(proj)

    reg = Registry.connect(tmp_path / "home" / "warren.db")
    pm = ProcessManager()
    try:
        ops.up(reg, pm, {"root": str(proj), "worktree": "a"})
        assert "acme_a" in FakeAdapter.store  # provisioned per worktree (no golden)
        assert "acme_golden_golden" not in FakeAdapter.store
        assert counter.read_text(encoding="utf-8") == "x"  # migrate ran on the worktree slice
    finally:
        pm.stop_all()
        reg.close()


def test_clone_from_falls_back_when_the_clone_itself_fails(tmp_path, monkeypatch) -> None:
    """A clone that *claims* to work but fails must degrade, not fail `up`.

    `capabilities()` describes the adapter; it cannot know the machine. A client
    binary too old for the server, a missing grant, a full disk — all surface here.
    The world Warren hands back must still be correct, just built the slow way, and
    the dest slice must be torn down first so migrate never runs into a half-copy.
    """
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    counter = tmp_path / "migrate_runs.txt"
    monkeypatch.setenv("COUNTER", str(counter))

    torn_down: list[str] = []

    class BrokenCloneFake(adapters.Adapter):
        CAPABILITIES: ClassVar[dict] = {"clone": True}  # promises, then can't deliver
        store: ClassVar[set[str]] = set()

        def provision(self, slice_name: str) -> None:
            BrokenCloneFake.store.add(slice_name)

        def resolve(self, slice_name: str) -> str:
            return f"broken://{slice_name}"

        def teardown(self, slice_name: str) -> None:
            torn_down.append(slice_name)
            BrokenCloneFake.store.discard(slice_name)

        def clone(self, source_slice: str, dest_slice: str) -> None:
            BrokenCloneFake.store.add(dest_slice)  # a half-copy, like a failed dump
            raise adapters.AdapterError("mysqldump failed: client too old")

    adapters.register("brokenclone", BrokenCloneFake)
    BrokenCloneFake.store.clear()

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "bump.py").write_text(
        "import os\nopen(os.environ['COUNTER'], 'a').write('x')\n", encoding="utf-8"
    )
    py = sys.executable
    (proj / "warren.toml").write_text(
        '[project]\nname = "acme"\n'
        '[services.db]\n'
        'adapter = "brokenclone"\n'
        'clone_from = "golden"\n'
        f"migrate = '\"{py}\" bump.py'\n",
        encoding="utf-8",
    )
    _init_git(proj)

    reg = Registry.connect(tmp_path / "home" / "warren.db")
    pm = ProcessManager()
    try:
        ops.up(reg, pm, {"root": str(proj), "worktree": "a"})  # must not raise

        assert "acme_a" in BrokenCloneFake.store  # the worktree still got its slice
        assert "acme_a" in torn_down  # the half-copy was cleared before the retry
        # migrate ran twice: once into the golden, once into the worktree it fell back to
        assert counter.read_text(encoding="utf-8") == "xx"

        events = (tmp_path / "home" / "events.log").read_text(encoding="utf-8")
        assert "clone_fallback" in events  # degraded loudly, not silently
        assert "client too old" in events  # and says why
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


def test_in_process_capabilities() -> None:
    assert FakeAdapter({}).capabilities() == {}

    class Cloner(adapters.Adapter):
        CAPABILITIES: ClassVar[dict] = {"clone": True}

        def provision(self, slice_name: str) -> None: ...
        def resolve(self, slice_name: str) -> str:
            return ""
        def teardown(self, slice_name: str) -> None: ...

    assert Cloner({}).capabilities() == {"clone": True}


def test_external_adapter_protocol_roundtrip(tmp_path) -> None:
    """Drive the example external adapter over subprocess+JSON — proving the
    protocol, the SDK's run(), and PATH-less discovery all work end-to-end."""
    base = tmp_path / "store"
    adapter = adapters.ExternalAdapter({"base": str(base)}, [sys.executable, str(_FILESTORE)])

    assert adapter.capabilities() == {"clone": True}
    adapter.provision("acme_feat")
    assert (base / "acme_feat").is_dir()
    assert adapter.resolve("acme_feat").startswith("file://")
    adapter.provision("acme_feat")  # idempotent
    adapter.teardown("acme_feat")
    assert not (base / "acme_feat").exists()


def test_external_adapter_clone(tmp_path) -> None:
    """The clone verb round-trips through the protocol (source → dest copy)."""
    base = tmp_path / "store"
    adapter = adapters.ExternalAdapter({"base": str(base)}, [sys.executable, str(_FILESTORE)])
    adapter.provision("golden")
    (base / "golden" / "seed.txt").write_text("realistic data", encoding="utf-8")

    adapter.clone("golden", "acme_feat")
    assert (base / "acme_feat" / "seed.txt").read_text(encoding="utf-8") == "realistic data"


def test_get_discovers_external_on_path(monkeypatch, tmp_path) -> None:
    fake_bin = tmp_path / "warren-adapter-thing"
    fake_bin.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        adapters.shutil, "which",
        lambda name: str(fake_bin) if name == "warren-adapter-thing" else None,
    )
    adapter = adapters.get("thing", {})
    assert isinstance(adapter, adapters.ExternalAdapter)


def test_external_adapter_surfaces_failure() -> None:
    bad = adapters.ExternalAdapter(
        {}, [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"]
    )
    with pytest.raises(adapters.AdapterError, match="failed"):
        bad.provision("x")


PG_URL = os.environ.get("WARREN_TEST_PG")


@pytest.mark.skipif(not PG_URL, reason="set WARREN_TEST_PG to a Postgres admin URL to run")
def test_postgres_adapter_roundtrip() -> None:
    adapter = adapters.get("postgres", {"admin_url": PG_URL})
    name = "warren_test_slice_zzz"
    clone = name + "_clone"
    adapter.teardown(name)  # clean slate
    adapter.teardown(clone)
    adapter.provision(name)
    assert name in adapter.resolve(name)
    adapter.provision(name)  # idempotent

    adapter.clone(name, clone)  # TEMPLATE copy
    assert clone in adapter.resolve(clone)

    adapter.teardown(clone)
    adapter.teardown(name)

"""Regression for I8: a worktree's `setup`-done marker must die with the worktree.

`setup` (e.g. `npm install`) runs once per worktree, gated by a marker under ~/.binky/setup/.
That marker lives *outside* the checkout, so destroying the worktree — via `remove`, or via a
failed `up`'s rollback — used to leave it behind. The next `up` of the same name then skipped
`setup` against the node_modules/venv it had just deleted, and the service died with a baffling
'command not found'. These pin the marker to the lifetime of the checkout it describes.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from binky import ops, paths
from binky.registry import Registry
from binky.rpc import RpcFault
from binky.supervisor import ProcessManager


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_project(tmp_path: Path, counter: Path, *, add_failing_service: bool = False) -> Path:
    """A project whose `app` service has both `setup` and `cmd`; `app.setup` appends to an
    absolute counter file (countable even after the checkout is deleted). With
    ``add_failing_service`` a second service `boom` (ordered after `app`) has a `setup` that
    exits non-zero, so `up` runs `app.setup` and *then* fails — exercising rollback."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "setup.py").write_text(
        f"from pathlib import Path\nPath(r{str(counter)!r}).open('a', encoding='utf-8').write('x')\n",
        encoding="utf-8",
    )
    (proj / "run.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    (proj / "boom.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    py = sys.executable
    toml = (
        '[project]\nname = "acme"\n'
        '[services.app]\n'
        f"setup = '\"{py}\" setup.py'\n"
        f"cmd = '\"{py}\" run.py'\n"
    )
    if add_failing_service:
        toml += (
            '[services.boom]\n'
            f"setup = '\"{py}\" boom.py'\n"
            f"cmd = '\"{py}\" run.py'\n"
            'depends_on = ["app"]\n'  # so app.setup runs (and marks) before boom fails
        )
    (proj / "binky.toml").write_text(toml, encoding="utf-8")
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "i")
    return proj


def test_remove_clears_setup_marker_so_recreate_reruns_setup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    counter = tmp_path / "setup_runs.txt"
    proj = _make_project(tmp_path, counter)
    marker = paths.binky_home() / "setup" / "acme" / "feat" / "app"

    reg = Registry.connect(tmp_path / "home" / "binky.db")
    pm = ProcessManager()
    try:
        ops.up(reg, pm, {"root": str(proj), "worktree": "feat"})
        assert marker.exists()
        assert counter.read_text(encoding="utf-8") == "x"  # setup ran exactly once

        ops.remove(reg, pm, {"root": str(proj), "worktree": "feat"})
        assert not marker.exists()  # the fix: the marker died with the worktree

        # re-creating the same name must RE-RUN setup, not skip it against a deleted tree
        ops.up(reg, pm, {"root": str(proj), "worktree": "feat"})
        assert counter.read_text(encoding="utf-8") == "xx"  # setup ran a second time
    finally:
        pm.stop_all()
        reg.close()


def test_failed_up_rollback_clears_setup_marker(tmp_path, monkeypatch) -> None:
    """A first `up` runs app.setup (marker written) then `boom` fails → rollback deletes the
    checkout. A retry must re-run app.setup, not skip it against the tree rollback removed."""
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    counter = tmp_path / "setup_runs.txt"
    proj = _make_project(tmp_path, counter, add_failing_service=True)
    marker = paths.binky_home() / "setup" / "acme" / "feat" / "app"

    reg = Registry.connect(tmp_path / "home" / "binky.db")
    pm = ProcessManager()
    try:
        with pytest.raises(RpcFault):
            ops.up(reg, pm, {"root": str(proj), "worktree": "feat"})
        assert counter.read_text(encoding="utf-8") == "x"  # app.setup ran on the first attempt
        assert not marker.exists()  # rollback cleared it (checkout is gone)
        assert reg.get_worktree("acme", "feat") is None  # fully rolled back

        with pytest.raises(RpcFault):
            ops.up(reg, pm, {"root": str(proj), "worktree": "feat"})
        assert counter.read_text(encoding="utf-8") == "xx"  # app.setup RE-RAN, didn't skip
    finally:
        pm.stop_all()
        reg.close()

"""Integration tests for the daemon lifecycle (spawns a real detached process)."""

import asyncio
import contextlib
import os
import stat
import threading

import pytest

from binky import daemon, ipc, paths
from binky.registry import WorktreeStatus
from binky.supervisor import TickReport

unix_only = pytest.mark.skipif(os.name == "nt", reason="Windows uses a loopback TCP port")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BINKY_HOME", str(tmp_path))
    monkeypatch.setenv("BINKY_PROXY_PORT", "0")
    yield
    # best-effort: never leave a spawned daemon behind
    with contextlib.suppress(Exception):
        daemon.stop(timeout=5)


def test_not_running_initially() -> None:
    assert daemon.is_running() is False
    assert ipc.read_daemon_info() is None
    with pytest.raises(ipc.DaemonNotRunning):
        ipc.call("ping")


def test_a_daemon_that_dies_on_startup_says_why(monkeypatch) -> None:
    """A failed start must report the cause, not just that time ran out.

    With stderr going to DEVNULL these two were the same event to the caller,
    which is what made a dead daemon look like a slow one. An unparseable proxy
    port kills the daemon before it writes its endpoint file, so this exercises
    the path where there is nothing to ping and never will be.
    """
    monkeypatch.setenv("BINKY_PROXY_PORT", "not-a-port")

    with pytest.raises(RuntimeError) as excinfo:
        daemon.start(timeout=30)

    message = str(excinfo.value)
    assert "exited with code" in message, message  # fast-failed, didn't wait out the timeout
    assert "not-a-port" in message  # and carried the daemon's own traceback back
    assert paths.daemon_log_path().exists()


def test_start_ping_stop() -> None:
    daemon.start(timeout=20)
    assert daemon.is_running()

    result = ipc.call("ping")
    assert result["pong"] is True
    assert isinstance(result["pid"], int)

    with pytest.raises(ipc.RpcError, match="unknown method") as excinfo:
        ipc.call("does-not-exist")
    assert excinfo.value.code == "unknown_method"

    # introspection methods over the wire
    assert set(ipc.call("methods")["methods"]) >= {"ping", "shutdown", "methods", "version"}
    assert ipc.call("version")["version"]

    assert daemon.stop(timeout=10) is True
    assert daemon.is_running() is False
    assert ipc.read_daemon_info() is None


def test_start_is_idempotent() -> None:
    daemon.start(timeout=20)
    first = ipc.read_daemon_info()
    daemon.start(timeout=20)  # already running → no-op
    second = ipc.read_daemon_info()
    assert first.pid == second.pid


@unix_only
def test_a_deep_home_still_gets_a_socket_that_fits(tmp_path, monkeypatch) -> None:
    """`sun_path` is 104 bytes on macOS, and a macOS $TMPDIR spends half of that
    before Binky appends anything — so a deep BINKY_HOME used to kill the daemon
    at bind() with a bare "AF_UNIX path too long"."""
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40)
    monkeypatch.setenv("BINKY_HOME", str(deep))

    sock = paths.socket_path()
    assert len(str(sock).encode()) <= 100, sock
    assert not str(sock).startswith(str(deep))  # fell back, rather than truncating

    info = sock.parent.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o700  # /tmp is shared; this dir is not


@unix_only
def test_a_deep_home_is_keyed_to_its_own_socket(tmp_path, monkeypatch) -> None:
    """Two homes must not land on the same socket just because both were long."""
    long_tail = "g" * 60
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "one" / long_tail))
    first = paths.socket_path()
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "two" / long_tail))
    assert paths.socket_path() != first


@unix_only
def test_the_daemon_comes_up_under_a_deep_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / ("h" * 40) / ("i" * 40) / ("j" * 40)))
    daemon.start(timeout=20)
    assert ipc.call("ping")["pong"] is True
    assert daemon.stop(timeout=10) is True


def test_clear_daemon_info_spares_another_daemons_endpoint() -> None:
    """A daemon can take seconds to finish dying — long enough for a replacement to
    have written its own endpoint. Deleting that one leaves a daemon nobody can find."""
    mine = ipc.DaemonInfo(transport="tcp", token="t", pid=os.getpid(), host="127.0.0.1", port=1)
    theirs = ipc.DaemonInfo(
        transport="tcp", token="t", pid=os.getpid() + 1, host="127.0.0.1", port=2
    )

    ipc.write_daemon_info(theirs)
    ipc.clear_daemon_info()
    assert ipc.read_daemon_info() == theirs  # not ours to delete

    ipc.write_daemon_info(mine)
    ipc.clear_daemon_info()
    assert ipc.read_daemon_info() is None

    paths.daemon_info_path().write_text("{not json", encoding="utf-8")
    ipc.clear_daemon_info()
    assert not paths.daemon_info_path().exists()  # unreadable is a leftover either way


def test_apply_report_keeps_registry_io_off_the_event_loop() -> None:
    """A supervision tick's registry writes are blocking SQLite that can wait out the
    busy-timeout; if they ran on the loop they'd freeze the proxy and RPC. They must
    run off it, while the admission/route bookkeeping stays on the loop — and the
    worktree still ends up marked down with its slot released."""
    d = daemon.Daemon()
    try:
        wt = d.registry.create_worktree(
            "proj", "alpha", "/tmp/x", "alpha", status=WorktreeStatus.ready
        )
        # mirror a live `up`: the worktree holds an admission slot and a proxy route
        d._slots[("proj", "alpha")] = 1
        d._routes_by_wt[("proj", "alpha")] = {"alpha.proj.test": 4000}

        seen: dict[str, threading.Thread] = {}
        real_set = d.registry.set_worktree_status

        def recording_set(worktree_id, status):
            seen["thread"] = threading.current_thread()
            return real_set(worktree_id, status)

        d.registry.set_worktree_status = recording_set  # type: ignore[method-assign]
        report = TickReport(worktrees_down=[wt.id])

        async def run():
            loop_thread = threading.current_thread()
            await d._apply_report(report)
            return loop_thread

        loop_thread = asyncio.run(run())

        assert seen["thread"] is not loop_thread  # the write ran off the event loop
        assert d.registry.get_worktree_by_id(wt.id).status == "stopped"  # ...and still applied
        assert ("proj", "alpha") not in d._slots  # slot freed on the loop
        assert d.routes == {}  # route dropped on the loop
    finally:
        d.registry.close()


def test_bad_token_rejected() -> None:
    daemon.start(timeout=20)
    info = ipc.read_daemon_info()
    tampered = ipc.DaemonInfo(
        transport=info.transport,
        token="wrong-token",
        pid=info.pid,
        path=info.path,
        host=info.host,
        port=info.port,
    )
    ipc.write_daemon_info(tampered)
    with pytest.raises(ipc.RpcError, match="bad token"):
        ipc.call("ping")
    # restore the real token so the fixture teardown can shut the daemon down
    ipc.write_daemon_info(info)

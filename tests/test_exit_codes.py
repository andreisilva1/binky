"""A daemon-busy `timeout` is 'pending', not 'failed' — it gets its own exit code.

A command queued behind a slow op on the same worktree lock (e.g. `down` behind a `reload`)
times out client-side at 5s while the daemon keeps working and converges. That must not read as
a failure to a script: it exits ``EXIT_PENDING`` (75, EX_TEMPFAIL) so the caller can poll
`binky status` instead of treating a successful op as an error. Every other RPC error still exits 1.
"""

import pytest
from typer.testing import CliRunner

from binky import cli
from binky.cli import EXIT_PENDING, app

runner = CliRunner()


@pytest.fixture
def daemon_up(monkeypatch):
    monkeypatch.setattr(cli._daemon, "is_running", lambda: True)
    # `up`/`reload` call ensure(), not is_running
    monkeypatch.setattr(cli._daemon, "ensure", lambda: None)


def _raise(exc):
    def _call(*_a, **_k):
        raise exc
    return _call


def test_a_busy_daemon_timeout_exits_pending_not_failure(daemon_up, monkeypatch) -> None:
    monkeypatch.setattr(
        cli.ipc, "call",
        _raise(cli.ipc.RpcError("timeout", "'down' is taking longer than 5s")),
    )
    result = runner.invoke(app, ["down", "feat"])
    assert result.exit_code == EXIT_PENDING == 75  # a distinct 'pending, poll status' code
    assert "taking longer than 5s" in result.output


def test_a_real_rpc_error_still_exits_one(daemon_up, monkeypatch) -> None:
    monkeypatch.setattr(
        cli.ipc, "call",
        _raise(cli.ipc.RpcError("not_found", "no worktree 'feat'")),
    )
    result = runner.invoke(app, ["down", "feat"])
    assert result.exit_code == 1  # genuine failures are unchanged
    assert "no worktree" in result.output


def test_timeout_on_a_long_command_also_exits_pending(daemon_up, monkeypatch) -> None:
    """Not just `down` — the shared helper means any RPC command reports a busy daemon
    the same way."""
    monkeypatch.setattr(
        cli.ipc, "call",
        _raise(cli.ipc.RpcError("timeout", "'reload' is taking longer than 120s")),
    )
    result = runner.invoke(app, ["reload", "feat"])
    assert result.exit_code == EXIT_PENDING


def test_down_and_remove_get_a_stop_sized_client_timeout(daemon_up, monkeypatch) -> None:
    """A graceful stop (SIGTERM → drain → SIGKILL) can take several seconds per service, so
    `down`/`remove` are lifecycle ops like up/reload — not quick queries. Left on the 5s default,
    a perfectly normal stop that took ~6s would spuriously exit `EXIT_PENDING`; they must ask for
    a budget generous enough to cover the bounded stop work (a real squatter/deadlock still
    eventually reports pending, just not on every ordinary removal)."""
    seen: dict[str, float] = {}

    def _capture(method, params=None, *, timeout=5.0):
        seen[method] = timeout
        return {"stopped": 0, "worktree": (params or {}).get("worktree")}

    monkeypatch.setattr(cli.ipc, "call", _capture)
    assert runner.invoke(app, ["down", "feat"]).exit_code == 0
    assert runner.invoke(app, ["remove", "feat", "--force"]).exit_code == 0
    # comfortably above the ~15s worst-case daemon-side stop+teardown, and above the 5s default
    assert seen["down"] >= 30
    assert seen["remove"] >= 30

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

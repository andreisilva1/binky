"""Observability: point-in-time metrics + the structured event log."""

import json
from pathlib import Path

from warren import observability
from warren.registry import Registry, WorktreeStatus


class _FakeProc:
    def is_running(self, worktree_id: int) -> bool:
        return False


class _FakeAdmission:
    max_parallel = 8
    in_use = 1
    queued = 2


def test_collect_metrics(tmp_path: Path) -> None:
    reg = Registry.connect(tmp_path / "warren.db")
    wt = reg.create_worktree("acme", "a", "/x", "a")
    reg.set_worktree_status(wt.id, WorktreeStatus.ready)
    reg.set_verify_status(wt.id, "passing")
    reg.create_worktree("acme", "b", "/y", "b")  # stays provisioning

    m = observability.collect_metrics(reg, _FakeProc(), _FakeAdmission())
    assert m["worktrees"]["total"] == 2
    assert m["worktrees"]["ready"] == 1
    assert m["worktrees"]["provisioning"] == 1
    assert m["verify"]["passing"] == 1
    assert m["admission"] == {"capacity": 8, "in_use": 1, "queued": 2}
    reg.close()


def test_log_event_writes_one_json_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    observability.log_event("up", worktree="feat", services=3)

    line = observability.events_path().read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["event"] == "up"
    assert record["worktree"] == "feat"
    assert record["services"] == 3
    assert "ts" in record


def test_events_log_rotates_by_size(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(observability, "MAX_EVENTS_BYTES", 200)
    for i in range(50):
        observability.log_event("tick", i=i, pad="x" * 20)

    events = observability.events_path()
    assert events.exists()
    assert (events.parent / "events.log.1").exists()  # rolled at least once

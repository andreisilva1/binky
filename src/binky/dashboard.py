"""The live dashboard (Textual TUI) — one screen for every worktree.

Polls the daemon's ``status`` and shows a table of worktrees (project, name,
state, services + ports). Thin: the poll is a quick localhost call on the UI
loop; a real streaming/log pane can grow from here.
"""

from __future__ import annotations

import contextlib
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header

from binky import ipc


class Dashboard(App):
    TITLE = "binky"
    BINDINGS: ClassVar = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("project", "worktree", "state", "services")
        self.set_interval(1.0, self.action_refresh)
        self.action_refresh()

    def action_refresh(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        worktrees: list[dict] = []
        with contextlib.suppress(Exception):
            worktrees = ipc.call("status")["worktrees"]
        for wt in worktrees:
            if wt["running"]:
                state = "unhealthy" if wt.get("health") == "unhealthy" else "running"
            else:
                state = wt["status"]
            services = ", ".join(f"{p['service']}:{p['port']}" for p in wt["ports"]) or "-"
            table.add_row(wt["project"], wt["worktree"], state, services)


def run() -> None:
    Dashboard().run()

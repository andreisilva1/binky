"""Smoke test for the Textual dashboard (mounts + builds the table headless)."""

import asyncio

from binky.dashboard import Dashboard


def test_dashboard_mounts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path))  # no daemon → status call fails, caught

    async def scenario() -> None:
        app = Dashboard()
        async with app.run_test() as pilot:
            from textual.widgets import DataTable

            table = app.query_one(DataTable)
            assert len(table.columns) == 4
            await pilot.press("r")  # manual refresh must not raise

    asyncio.run(scenario())

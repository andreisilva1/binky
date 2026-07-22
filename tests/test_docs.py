"""The README makes countable claims about the adapter matrix. Keep them true.

A number in a README rots the moment an adapter is added, and it rots silently —
nobody re-counts a table while writing a feature. These tests fail instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from warren import adapters

README = Path(__file__).resolve().parent.parent / "README.md"


def test_the_readme_states_the_real_adapter_counts():
    text = README.read_text(encoding="utf-8")
    match = re.search(r"\*\*(\d+) names, (\d+) implementations", text)
    assert match, "README no longer states the adapter matrix size"

    names = adapters.available()
    classes = {type(adapters.get(n, {"admin_url": "scheme://host"})).__name__ for n in names}
    assert int(match.group(1)) == len(names)
    assert int(match.group(2)) == len(classes)


def test_every_adapter_appears_in_the_readme_table():
    """A shipped adapter nobody can find is a shipped adapter nobody uses."""
    table = README.read_text(encoding="utf-8").split("### `clone_from`")[0]
    listed = set(re.findall(r"`([a-z0-9]+)`", table))
    missing = sorted(set(adapters.available()) - listed)
    assert not missing, f"adapters missing from the README table: {missing}"

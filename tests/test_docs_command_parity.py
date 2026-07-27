"""The website's CLI reference must document every command the README documents.

B6 (the internal battery) already checks the README's command table against the real CLI,
but nothing guarded the *website* — so new commands (``gc``, ``reserve``, ``port``,
``config``) landed in the README and the CLI while ``site/docs.html`` still listed an older
surface, and no test noticed. This pins the two together: every ``binky <cmd>`` the README
table documents must also appear on the docs page, so front drift fails CI instead of
shipping silently.

Parity is one-directional on purpose — the front must *cover* the README, but may also show
a command in prose the README's table doesn't (so extra tokens on the page are fine).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _readme_commands() -> set[str]:
    """Commands documented in the README's CLI table.

    Rows look like ``| `binky <cmd> …` | … |`` — anchor on that shape so prose mentions of
    "binky" (``binky is``, ``binky runs``) never leak in as fake commands.
    """
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\|\s*`binky\s+([a-z]+)", text, re.MULTILINE))


def _front_commands() -> set[str]:
    """Every ``binky <cmd>`` token that appears on the docs page (in any code/prose span)."""
    text = (_ROOT / "site" / "docs.html").read_text(encoding="utf-8")
    return set(re.findall(r"binky\s+([a-z]+)", text))


def test_website_documents_every_readme_command() -> None:
    readme = _readme_commands()
    # a sanity floor: if the table parser drifts and matches almost nothing, fail loudly here
    # rather than pass a vacuous subset check.
    assert len(readme) >= 15, f"README command-table parser drift — only found {sorted(readme)}"

    missing = readme - _front_commands()
    assert not missing, (
        f"site/docs.html is missing commands the README documents: {sorted(missing)}. "
        "Add them to the CLI reference table so the website doesn't lag the CLI."
    )

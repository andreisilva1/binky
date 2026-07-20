"""Thin git-worktree helpers (THESIS §8.8).

`warren up <name>` uses the branch if it exists, else creates it off HEAD, and
checks it out into its own worktree directory. Warren never commits — the agent
or the human does; these helpers only manage worktrees.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    """A git command failed."""


def _run(args: list[str], cwd: str | Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GitError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def is_repo(root: str | Path) -> bool:
    try:
        return _run(["rev-parse", "--is-inside-work-tree"], root) == "true"
    except GitError:
        return False


def branch_exists(root: str | Path, branch: str) -> bool:
    try:
        _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root)
        return True
    except GitError:
        return False


def add_worktree(root: str | Path, path: str | Path, branch: str) -> None:
    """Check *branch* out into a new worktree at *path* (creating the branch off HEAD if new)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        _run(["worktree", "add", str(path), branch], root)
    else:
        _run(["worktree", "add", "-b", branch, str(path)], root)


def remove_worktree(root: str | Path, path: str | Path, *, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    _run(args, root)


def is_dirty(path: str | Path) -> bool:
    """True if the worktree has uncommitted changes (tracked or untracked)."""
    return bool(_run(["status", "--porcelain"], path).strip())

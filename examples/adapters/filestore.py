#!/usr/bin/env python
"""Example external adapter: an isolated directory per worktree.

A dependency-free reference implementation of the external adapter protocol — the
shape a community adapter (Redis, S3, a queue) follows. It gives each worktree its
own directory carved out of a shared base (``bucket-prefix per worktree``, the S3
idea in miniature) and hands back a ``file://`` address.

Try it:  install this file as ``warren-adapter-filestore`` on your PATH, then in
``warren.toml``::

    [services.store]
    adapter = "filestore"

Warren discovers ``warren-adapter-filestore`` on PATH and drives it over the
subprocess+JSON protocol. Everything below the imports is just the three verbs.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import ClassVar

from warren.adapter_sdk import Adapter, run


def _base(config: dict) -> Path:
    root = config.get("base") or os.environ.get("WARREN_FILESTORE_BASE")
    return Path(root) if root else Path(tempfile.gettempdir()) / "warren-filestore"


class FilestoreAdapter(Adapter):
    CAPABILITIES: ClassVar[dict] = {"clone": True}

    def provision(self, slice_name: str) -> None:
        (_base(self.config) / slice_name).mkdir(parents=True, exist_ok=True)

    def resolve(self, slice_name: str) -> str:
        return (_base(self.config) / slice_name).as_uri()

    def teardown(self, slice_name: str) -> None:
        shutil.rmtree(_base(self.config) / slice_name, ignore_errors=True)

    def clone(self, source_slice: str, dest_slice: str) -> None:
        base = _base(self.config)
        shutil.copytree(base / source_slice, base / dest_slice, dirs_exist_ok=True)


if __name__ == "__main__":
    raise SystemExit(run(FilestoreAdapter))

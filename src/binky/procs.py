"""Creation flags for the child processes the daemon spawns.

The daemon itself runs detached with no console (see ``daemon.start``). On
Windows a console-subsystem child launched from a console-less parent gets a
*new* console allocated for it — which flashes up on screen and vanishes, once
per service start, health probe, git call, adapter probe, and taskkill. Passing
``CREATE_NO_WINDOW`` suppresses that console without detaching the child (so the
supervisor still keeps it in a killable process group). The flag does not exist
off Windows, where ``creationflags=0`` is simply the default and a no-op.
"""

from __future__ import annotations

import subprocess

# 0 everywhere but Windows, so it can be passed (or OR'd) unconditionally.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

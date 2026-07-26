# Architecture

A map of how Binky fits together, for anyone working on it. For *what* Binky does and
*why*, read the README first; this document is about the internals.

## The shape

Binky is a **CLI** that talks to a single **machine-global daemon**. The CLI is a thin,
synchronous client: it sends one request and prints the result. All the real work —
supervising processes, allocating ports, provisioning resources, routing traffic —
happens in the daemon, which outlives any single command.

```
binky <cmd>  ──(one-shot RPC)──▶  daemon  ──▶  worktrees, ports, adapters, proxy
   cli.py                          daemon.py
   ipc.py  ◀──────────────────────  rpc.py
```

State lives in `~/.binky/` (`BINKY_HOME`): a SQLite registry plus per-worktree logs and
golden templates. Nothing runs in the cloud.

## The six subsystems

The lifecycle the README describes maps onto these modules:

| Subsystem | Modules | Responsibility |
| --- | --- | --- |
| **Worktree** | `git.py`, `ops.py` | Create/use a git worktree per name; guard destructive ops. |
| **Ports** | `allocator.py`, `resolve.py` | Assign every process a port *before anything starts* and build the full token map (`${self.port}`, `${api.url}`, …). |
| **Resources** | `adapters.py`, `builtin_adapters.py`, `adapter_sdk.py` | Carve an isolated slice of a shared server per worktree; tear it down on remove. |
| **Supervision** | `supervisor.py`, `health.py`, `procs.py` | Start services in `depends_on` order, wait for health, capture logs, restart per policy, reclaim dead worktrees. |
| **Proxy** | `proxy.py`, `https.py` | Route `<service>.<worktree>.<domain>` to the right port (WebSockets/HMR included); optionally front with Caddy for TLS. |
| **Admission** | `admission.py` | A FIFO queue bounded by `max_parallel` and free RAM, so a swarm doesn't melt the box. |

Supporting modules: `config.py` (the `binky.toml` model), `registry.py` + `paths.py`
(state), `observability.py` (metrics/events), `cli.py` / `init_cmd.py` / `dashboard.py` /
`mcp.py` (front ends), `daemon.py` / `rpc.py` / `ipc.py` / `ops.py` (the daemon and its
request handling).

## The daemon

- **One per machine.** Detached, asyncio-based, single event loop.
- **IPC transport is OS-specific:** a Unix-domain socket on posix, a loopback TCP port on
  Windows (asyncio has no Unix sockets there). The endpoint and a shared-secret token are
  written to `daemon.json`, owner-only (`0600`). The token is the guard on the loopback
  transport — see [SECURITY.md](SECURITY.md).
- **The event loop is sacred.** Blocking work (SQLite, process I/O) is offloaded with
  `asyncio.to_thread`; only asyncio-touching state (admission waiters, proxy routes) stays
  on the loop. Mixing the two is the bug class to watch for when editing `daemon.py`.
- **Startup convergence vs. live supervision** are separate: `ops.reconcile` cleans up
  whatever a previous daemon left behind at startup; `supervisor.tick()` handles exits,
  restarts, and reclamation *while things run*.

## The registry

- SQLite in WAL mode at `~/.binky/binky.db`.
- **Versioned, in-place migrations.** `SCHEMA_VERSION` + a `_MIGRATIONS` table drive
  forward migrations applied on connect, so an upgrade never asks the user to reset state.
  When you change the schema, bump the version and add a migration — don't edit past ones.

## Two invariants worth internalizing

- **Resolve-then-launch.** Ports are allocated and the *entire* token map is built before
  any process starts, so every process boots already knowing its own address and its
  dependencies'. Nothing is discovered at runtime, which is what makes N parallel worlds
  collision-free. (`allocator.py`, `resolve.py`)
- **`(pid, create_time)` process identity.** A bare pid is not a stable identity — the OS
  recycles pids across reboots, so a stored pid can later name an unrelated process.
  Binky pairs each pid with the process's creation time (`psutil`), and only acts on a pid
  when both match. This is why reconcile can safely kill *its* leftovers without ever
  SIGKILLing a stranger that inherited the number. (`procs.py`, `registry.py`)

## Testing

Unit tests are hermetic and fast. Adapter integration tests talk to real servers and skip
unless pointed at them via `BINKY_TEST_*` — see [CONTRIBUTING.md](CONTRIBUTING.md). The
README's adapter matrix is itself asserted by `tests/test_docs.py`, so the docs can't
silently drift from the code.

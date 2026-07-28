# Binky for agents

Binky gives each git worktree its own isolated local world — ports, env, a private
database slice, and stable URLs — so several agents can work on the same repo in
parallel without colliding on ports, databases, or `.env`. You *call* Binky; it never
drives you.

## When to reach for it

- You're about to run a branch's dev server / migrations while another branch is already up.
- You need a throwaway but *real* database for a task, not a mock.
- You're one of several agents pointed at the same repository.

If you only ever have one world up at a time, you don't need Binky.

## Install it as a tool

One line in Claude Code:

```bash
claude mcp add binky -- binky mcp
```

Or with nothing installed, via `uvx`:

```bash
claude mcp add binky -- uvx --from "binky[mcp]" binky mcp
```

Any MCP client takes the same stdio server as a config block:

```json
{ "mcpServers": { "binky": { "command": "uvx", "args": ["--from", "binky[mcp]", "binky", "mcp"] } } }
```

Binky is also in the [MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.andreisilva1/binky`, for clients that resolve servers from it.

## The tools

`binky mcp` exposes the same verbs the CLI calls, against the same machine-global daemon:

| tool | signature | does |
|---|---|---|
| `up` | `up(worktree, root="", group="")` | Bring a worktree's isolated world online (starts the daemon if needed). Returns its services, ports, and URLs. |
| `down` | `down(worktree, root="")` | Stop a worktree's processes; keeps its files and ports. |
| `status` | `status()` | List every worktree on this machine with its state, health, and ports. |
| `env` | `env(worktree, root="")` | A worktree's live coordinates — service URLs and adapter addresses — to hand to a process or a test run. |
| `verify` | `verify(worktree, root="")` | Run the project's `agents.verify` checks against a worktree; returns the pass/fail verdict. |

`root` defaults to the current working directory — pass it when you're operating on a
project elsewhere.

## A typical loop

1. `up("my-task")` → the branch is live; read the returned ports/URLs.
2. Do the work; hit the service through its URL (or `binky curl` from a shell).
3. `verify("my-task")` → gate on the project's own checks.
4. `down("my-task")` to pause, or ask a human to `binky remove my-task` to destroy it.

## The rest

The config format (`binky.toml`), the full CLI, the adapter matrix, and how the daemon
works are in [README.md](README.md). Binky runs your commands as you, on your machine —
it is a developer tool, not a sandbox; see [SECURITY.md](SECURITY.md).

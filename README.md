# 🐇 Warren

**Give every git worktree its own isolated local world — ports, env, database, URLs — so a swarm of coding agents runs in parallel on one machine without anything colliding.**

> Status: **pre-alpha**, under active construction. Not usable yet.

```bash
warren up login-fix     # (soon) bring a worktree's isolated world online
```

## Development

```bash
uv sync                 # create the venv + install deps
uv run warren --version
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE).

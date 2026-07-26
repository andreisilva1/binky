# Changelog

All notable changes to Binky are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While Binky is in `0.x`, the config format is stable but the CLI surface may still change
between minor versions.

## [Unreleased]

### Added
- Machine-global settings store (`~/.binky/settings.json`) with `binky config` to show it and
  `binky reserve` / `binky port` to change it.
- `binky gc` — sweep orphaned adapter slices left half-provisioned by a crash.
- Disk headroom in admission: a worktree is admitted only when a fixed amount of free disk
  (default 5 GB, tunable with `binky reserve --disk`) remains, alongside the existing RAM reserve.
- Pressure eviction: when a running swarm crosses the RAM reserve, the daemon suspends the newest
  worktree (new `suspended` status) and resumes it in fairness order once room returns.
- Per-service log rotation, so a chatty service can't fill the disk.

### Changed
- Relicensed from MIT to Apache-2.0.
- The RAM reserve is now a percentage of total memory and the disk reserve a fixed GB headroom
  (a percentage of a large disk would demand more free space than a nearly-full disk can offer).
- A daemon restart now **adopts** still-live worktree processes instead of killing them; adopted
  processes are supervised for liveness and can be stopped, and regain restart/health on the next `up`.

### Fixed
- `up` refuses to reuse a port that an outside process grabbed while the worktree was down,
  reporting it instead of crash-looping the service.
- `stop` escalates a SIGKILL to the whole process group, so a child that ignores SIGTERM can't
  orphan its descendants.
- Bind failures are surfaced by `up` and `doctor` instead of failing silently.

## [0.0.1] - 2026-07-25

### Added
- Initial alpha release: per-worktree isolation (ports, env, database slice, URLs), a
  machine-global supervision daemon, a global port allocator, RAM-bounded FIFO admission,
  an embedded reverse proxy with optional Caddy HTTPS, and 16 adapter names across 10
  implementations.
- `binky` CLI (`init`, `check`, `up`, `down`, `remove`, `status`, `env`, `verify`, `logs`,
  `dashboard`, `doctor`, `metrics`, `mcp`, `daemon`).
- MCP server (`binky mcp`) exposing `up`, `down`, `status`, `env`, and `verify` to agents.

[Unreleased]: https://github.com/andreisilva1/binky/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/andreisilva1/binky/releases/tag/v0.0.1

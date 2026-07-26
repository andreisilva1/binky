# Changelog

All notable changes to Binky are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While Binky is in `0.x`, the config format is stable but the CLI surface may still change
between minor versions.

## [Unreleased]

### Changed
- Relicensed from MIT to Apache-2.0.

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

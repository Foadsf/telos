# Changelog

## Unreleased

### Added

- `integrations/pi/telos.ts`: a pi extension with the Telos implementer tools as native pi tools, a footer status line, and `/telos`, `/telos-schedule` and `/telos-next`.
- `integrations/opencode/`: `/telos` and `/telos-next` commands for every OpenCode client (TUI, web, desktop), and a server plugin that shows Telos status toasts in the TUI.
- `checkout` without a task id leases the top ready task that has a locked contract, or resumes the agent's own lease; every checkout gate still applies.
- `verify --next` / `telos_submit_verification` `next: true`: a pass also leases the following task, through the same checkout gates; a failure leases nothing.
- `pulse` lists blocked tasks with the reason (awaiting a contract, waiting on a prerequisite, quarantined, seal violation), done tasks, and the last failure of each active lease.

### Changed

- Fewer agent round trips and shorter agent context: compact toon views for pulse, checkout and verify (empty fields, estimates, verifier ids, git refs and the token budget left out; `--format json` unchanged), terse MCP tool descriptions, and a shorter `AGENTS.md`/`CLAUDE.md` block that no longer asks for a heartbeat per task. Measured on the bench: see `bench/README.md`.
- CLI gate refusals print one line on stderr (exit 2) instead of a traceback.

## 0.5.0 — 2026-10-16

First public release.

### Security

The v0.4 anti-reward-hacking gates could be opened by the agent they were meant to police. All of the following are now closed, each with a regression test:

- An implementer locking its own contract: the implementer MCP surface has no `telos_contract_lock`, and contracts lock only before any lease.
- A test that cannot fail: contracts must fail on the current tree (negative control).
- Editing `.telos/` state, contracts, edges, config or leases by hand: these are HMAC-sealed and fail closed.
- Self-release from quarantine: `unquarantine` is human-only (interactive CLI or TUI).
- Readable signing key and unchecked receipts: the key lives outside the workspace and receipts are verified wherever DONE is trusted.
- Rigged runs: the harness surface (conftest, pytest config, shadow modules) is locked, the `PYTHON*`/`PYTEST_*` environment is dropped, and pytest evidence comes from a JUnit report.
- Destructive git operations: Telos only resets or removes worktrees it created, and never acts on an enclosing repository.

### Added

- `attest-done` and `reseal` (human-only), signed attestations, and `integrity_violations` in `pulse`.
- `telos-mcp --role architect` as a separate surface.
- A merge-safe `bootstrap-agents` for Claude Code (`.mcp.json`), OpenCode (`opencode.json`) and an `AGENTS.md`/`CLAUDE.md` block with CLI equivalents for harnesses without MCP.

### Changed

- A Mikado prerequisite is no longer auto-leased; it waits for a locked contract.
- `zwicky-commit` is idempotent.
- The CLI splits on `--` itself, so `contract-lock … -- <command>` parses the same on Python 3.12 and 3.13+.

## 0.4.0 and earlier

Unreleased development versions.

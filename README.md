# Telos

**Telos** is a task harness for AI coding agents and the humans who direct them. The rule it enforces: an agent never decides on its own that its work is done. Every task is leased against a test that a human or architect locked *before* the work started. The task closes only when Telos itself runs that test and signs a receipt.

It also plans: backward (Mikado) decomposition, V-Model traceability, PERT/CPM with Monte Carlo schedules, and Zwicky morphological analysis for design choices. Agents reach it over MCP (Claude Code, OpenCode, Codex and other MCP clients) or through the `telos` CLI (for harnesses without MCP, such as pi). Python 3.12+.

**Status:** alpha (0.5.0). Tested on Windows 11 (Python 3.14) and Ubuntu 24.04 (Python 3.12). Read the Trust Model below before relying on it.

## What it does

- **Locked, failing tests before any work.** A contract (test command plus SHA-256 digests of the test files) is locked before a task can be leased. The command must fail on the current tree, so a test that cannot fail is rejected.
- **Verification Telos runs itself.** `verify` re-checks the locked digests and the test harness surface, scans for skip/exit tricks, runs the command in a scrubbed environment, reads pytest's JUnit report, and signs a receipt. Non-pytest commands fall back to the exit code and printed counts, which is weaker evidence.
- **Leases with fencing tokens and a 3-strike circuit breaker.** Stale agents cannot overwrite newer work. Repeated failures or tampering quarantine the task, and only a human releases it.
- **Mikado method.** When an agent hits a missing prerequisite, it records it. Telos snapshots the attempt into a git ref and blocks the task on the new prerequisite.
- **V-Model traceability.** Left-wing nodes (L0 requirement to L3 implementation) are verified by right-wing nodes (R3 unit to R0 acceptance). A parent closes only after its children.
- **PERT/CPM and Monte Carlo.** Deterministic critical path plus Beta-PERT simulation (P50/P80/P95 duration and token cost, criticality index). Parallel execution waves for several agents.
- **Zwicky morphological analysis.** Design dimensions, pairwise cross-consistency (`unassessed`, `incompatible`, `neutral`, `synergistic`), ranked conflict-free configurations, and committed decisions that spawn traced tasks.
- **Git-friendly state.** One canonical `.toml` file per node plus append-only `.jsonl` ledgers in `.telos/`. A SQLite index and the active leases live in `.telos/.cache/`, which is not committed; deleting it revokes open leases.
- **Interfaces.** MCP server (stdio or Streamable HTTP), argparse CLI with `rich`/`json`/`toon` output, a Textual TUI, and a static HTML dashboard.
- **Google Tasks projection (experimental).** Push and reconcile only. A completed checkbox can never close a task that has a verification gate. Push has not been tested against the live API.

## Trust Model (v0.5)

The agent being verified must not be able to open its own gates. Telos enforces that structurally, not by asking the agent who it is.

| Gate | How it holds |
|---|---|
| Who writes the exam | The default MCP server (`telos-mcp`) is the **implementer** surface: no `telos_contract_lock`, no unquarantine. Contracts are locked by a human (`telos contract-lock`) or a separate architect server (`telos-mcp --role architect`), and only **before** any lease. A task without a locked contract cannot be leased. |
| A test that cannot fail | **Negative control** at lock time: the command must fail on the current tree (red before green). A green baseline is accepted only with a signed human attestation (`--allow-green-baseline`). |
| Rigging the run | Locked test digests, plus a sealed **harness surface**: every `conftest.py`, `sitecustomize`/`usercustomize`, top-level `.pth`, pytest sections of `pytest.ini`/`pyproject.toml`/`setup.cfg`/`tox.ini`, scripts named in the command, and new top-level modules that shadow stdlib/installed ones (e.g. a fake `pytest.py`). `PYTHON*`/`PYTEST_*` environment variables never reach the run. Pytest results come from a JUnit report Telos owns; a run that aborts before writing it is a failure. |
| Editing state files | Gate fields of nodes, contracts, edges, `config.toml` and leases carry an HMAC seal. A broken seal fails closed: the node reads as QUARANTINED and is listed under `integrity_violations` in `telos_pulse`. DONE is persisted only with a signed passing receipt or a signed human attestation. |
| Forging receipts | The key lives outside the workspace (`%LOCALAPPDATA%\telos\keys` / `~/.local/share/telos/keys`, override `TELOS_KEY_DIR`). Receipts and attestations are verified wherever DONE is trusted. A missing key does not mint a new trust root; a human runs `telos reseal`. |
| Releasing quarantine | Human-only: `telos unquarantine` (interactive terminal + typed confirmation) or the TUI. Every release is a signed attestation. |
| Destructive git operations | Telos resets, harvests, or deletes only worktrees it created under `.telos/.cache/worktrees`, and uses git plumbing only when the workspace is the repository top level. |

**Residual risks (by design, stated plainly):**

- An agent that can run arbitrary code **as the same OS user** can read the key and call the library directly. Closing that needs the verifier and key under a separate OS principal (service account or container).
- The human gate (interactive terminal + typed confirmation) stops agents acting through MCP or non-interactive shells. It does not stop an agent that drives a real terminal.
- Code under test runs inside the test process and can still subvert a run in ways no static check sees. The negative control proves a test *can* fail. It does not prove the test is strong. Test quality remains the architect's job.
- Token heartbeats are self-reported.

Upgrading a v0.4 workspace: review it, then run `telos reseal --rationale "..."` once. Unverifiable DONE tasks are kept DONE through an explicit `reseal-done` attestation, and open leases are revoked.

Report security problems as described in [SECURITY.md](SECURITY.md).

## Installation

As a tool, which puts `telos` and `telos-mcp` on PATH:

```bash
uv tool install git+https://github.com/Foadsf/telos
```

For development:

```bash
git clone https://github.com/Foadsf/telos && cd telos
uv venv && uv pip install -e ".[dev]"
pytest
```

## Quickstart

```bash
telos init --name "MyProject"

# Add a task
telos add "Fix the median helper" --spec "tests/test_stats.py passes"

# Human architect: lock a test that FAILS today, before any agent leases the task
telos contract-lock <task_id> --path tests/test_stats.py -- python -m pytest -q tests/test_stats.py

# Register Telos for agents in this repo (merges .mcp.json, opencode.json and an AGENTS.md/CLAUDE.md block)
telos bootstrap-agents

telos pulse        # what is ready, leased, quarantined
telos schedule     # CPM and Monte Carlo
telos tui          # interactive board
```

An agent then runs `pulse`, leases the task (`telos_checkout_leaf` over MCP, or `telos checkout <id> --agent <name>`), changes the code, and closes it with `telos_submit_verification` / `telos verify`. Check the outcome in Telos, not in the agent's summary: `telos --format json pulse`, and the receipt in `.telos/ledger/receipts.jsonl`.

## Testing

```bash
pytest
```

The suite replays known bypasses (self-locked contracts, tests that cannot fail, rigged conftest/pytest/environment, edited state files, forged receipts, self-release from quarantine) and checks that each fails closed.

## License

Apache-2.0. See [LICENSE](LICENSE).

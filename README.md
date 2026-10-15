# Telos AI Project Management Harness

**Telos** is a cross-platform, hardware-agnostic, deterministic cognitive exoskeleton and project/task management harness in Python 3.12+ for AI agents (Claude Code, OpenAI ChatGPT/Codex CLI, Google Antigravity, Pi, OpenCode) and human architects.

## Key Features

- **Mikado Method Engine (Inverse / Backward Planning):** Discover prerequisite blockers during execution, automatically capture dirty worktree snapshots into isolated refs, and resolve execution bottom-up from unblocked leaves.
- **V-Model Traceability Matrix ($M:N$):** Strict Left-Wing specifications ($L0 \dots L3$) paired with Right-Wing verification contracts ($R3 \dots R0$). Prevents AI reward hacking via cryptographic hash-locking and AST anti-cheat scans.
- **Two-Tier PERT / CPM & Monte Carlo Beta-PERT Scheduler:** Deterministic Critical Path Method ($TF=0$) combined with 2,000+ trial Monte Carlo simulation capturing parallel merge bias ($P_{50}, P_{80}, P_{95}$ duration and token distributions).
- **Zwicky Morphological Analysis & Cross-Consistency Assessment (CCA):** Formal multi-dimensional design space exploration with 4-state compatibility matrices (`unassessed`, `incompatible`, `neutral`, `synergistic`) and cost budgeting.
- **Fencing Tokens & 3-Strike Circuit Breaker:** Monotonically increasing lease generations preventing stale agent clobbering, with automated quarantine on repeated failures or tampering attempts. Quarantine is released only by a human (see Trust Model).
- **Git-Merge-Friendly Persistence:** Authoritative state stored as per-node canonical `.toml` files and append-only `.jsonl` ledgers. Ephemeral SQLite WAL query cache in `.telos/.cache/index.sqlite`.
- **Multi-Modal Interfaces:** Frugal Model Context Protocol (MCP) server for agents (`telos_pulse`, `telos_checkout_leaf`, `telos_mikado_discover`, `telos_submit_verification`, `telos_schedule_pert`, `telos_zwicky_solve`, `telos_decompose`), unified Typer/Rich CLI, and reactive keyboard-driven Textual TUI.
- **Google Tasks Projection:** Scoped two-way projection with verification gate safety (mobile checkboxes cannot bypass V-Model verification contracts).

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

## Installation

```bash
uv venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

uv pip install -e ".[dev]"
```

## Quickstart

```bash
# Initialize a workspace
telos init --name "MyProject"

# Add a task
telos add "Design System Architecture" --kind L1_architecture --opt-min 20 --likely-min 45 --pess-min 90

# Human architect: lock a test that FAILS today, before any agent leases the task
telos contract-lock <task_id> --path tests/test_feature.py -- python -m pytest tests/test_feature.py

# View agent radar
telos pulse

# Run CPM and Monte Carlo schedule analysis
telos schedule

# Launch interactive Textual TUI
telos tui

# Run MCP stdio server (implementer surface; add --role architect for a separate architect agent)
telos mcp
```

## Testing

```bash
pytest -v
```

## License

Apache-2.0

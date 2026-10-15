# Telos AI Project Management Harness

**Telos** is a cross-platform, hardware-agnostic, deterministic cognitive exoskeleton and project/task management harness in Python 3.12+ for AI agents (Claude Code, OpenAI ChatGPT/Codex CLI, Google Antigravity, Pi, OpenCode) and human architects.

## Key Features

- **Mikado Method Engine (Inverse / Backward Planning):** Discover prerequisite blockers during execution, automatically capture dirty worktree snapshots into isolated refs, and resolve execution bottom-up from unblocked leaves.
- **V-Model Traceability Matrix ($M:N$):** Strict Left-Wing specifications ($L0 \dots L3$) paired with Right-Wing verification contracts ($R3 \dots R0$). Prevents AI reward hacking via cryptographic hash-locking and AST anti-cheat scans.
- **Two-Tier PERT / CPM & Monte Carlo Beta-PERT Scheduler:** Deterministic Critical Path Method ($TF=0$) combined with 2,000+ trial Monte Carlo simulation capturing parallel merge bias ($P_{50}, P_{80}, P_{95}$ duration and token distributions).
- **Zwicky Morphological Analysis & Cross-Consistency Assessment (CCA):** Formal multi-dimensional design space exploration with 4-state compatibility matrices (`unassessed`, `incompatible`, `neutral`, `synergistic`) and cost budgeting.
- **Fencing Tokens & 3-Strike Circuit Breaker:** Monotonically increasing lease generations preventing stale agent clobbering, with automated quarantine on repeated failures or tampering attempts.
- **Git-Merge-Friendly Persistence:** Authoritative state stored as per-node canonical `.toml` files and append-only `.jsonl` ledgers. Ephemeral SQLite WAL query cache in `.telos/.cache/index.sqlite`.
- **Multi-Modal Interfaces:** Frugal Model Context Protocol (MCP) server for agents (`telos_pulse`, `telos_checkout_leaf`, `telos_mikado_discover`, `telos_submit_verification`, `telos_schedule_pert`, `telos_zwicky_solve`, `telos_decompose`), unified Typer/Rich CLI, and reactive keyboard-driven Textual TUI.
- **Google Tasks Projection:** Scoped two-way projection with verification gate safety (mobile checkboxes cannot bypass V-Model verification contracts).

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

# View agent radar
telos pulse

# Run CPM and Monte Carlo schedule analysis
telos schedule

# Launch interactive Textual TUI
telos tui

# Run MCP stdio server
telos mcp
```

## Testing

```bash
pytest -v
```

## License

Apache-2.0

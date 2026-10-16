---
description: Lease the top ready Telos task and work it through verification
---
Work the next Telos task in this repository with the telos MCP tools:

1. Lease it with `telos_checkout_leaf` and no `task_id` (the top ready task, or your own interrupted lease). Keep its `lease_id` and `fencing_token`.
2. Change only the code under test. Never edit `.telos/`, locked tests, `conftest.py` or pytest configuration.
3. Close it with `telos_submit_verification`.
4. Report the state and receipt Telos returns, not your own judgment of the code.

If nothing can be leased, Telos says why (no locked contract, quarantine, a lease held elsewhere): report that and stop.

$ARGUMENTS

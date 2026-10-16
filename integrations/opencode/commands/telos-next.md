---
description: Lease the top ready Telos task and work it through verification
---
Work the next Telos task in this repository with the telos MCP tools:

1. Call `telos_pulse`.
2. Lease the top ready task with `telos_checkout_leaf` and keep its `lease_id` and `fencing_token`.
3. Change only the code under test. Never edit `.telos/`, locked tests, `conftest.py` or pytest configuration.
4. Close it with `telos_submit_verification`.
5. Report the state and receipt Telos returns, not your own judgment of the code.

If nothing can be leased (no ready task, no locked contract, quarantine), say why and stop.

$ARGUMENTS

---
description: Telos state of this repository (ready, leased, quarantined, integrity)
---
This is the current Telos state of the repository, straight from `telos pulse`:

!`telos --format toon pulse --agent opencode`

Show it to me as a short list: ready tasks (id, title), active leases (id, title, fencing token, strikes, last failure), blocked tasks with the reason, done tasks, quarantined tasks, and any integrity violations. If the output above is an error or empty, say so plainly. Do not start any work, do not call tools, and do not edit files.

Human-only actions, which run in an interactive terminal and never from you: `telos contract-lock`, `telos unquarantine`, `telos attest-done`, `telos reseal`.

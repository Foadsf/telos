# Security Policy

Telos is a trust boundary between an AI agent and the decision that its work is done. A way for the agent being verified to close a task without passing its locked contract is a security bug, even when it looks like a clever test.

## In scope

- Closing a task, or making it look DONE, without a passing run of its locked contract.
- Locking, replacing, or weakening a contract from the implementer surface, or after a lease.
- Making a verification run report success for code that fails the locked tests (harness, environment, or runner tricks).
- Forging or replaying receipts, attestations, seals, or leases.
- Leaving quarantine without a human.
- Telos modifying or deleting files outside the workspace, or outside the worktrees it created.

## Known limits (not vulnerabilities)

These are documented in the README's Trust Model:

- An agent running arbitrary code as the same OS user can read the signing key.
- The interactive-terminal human gate does not stop an agent that drives a real terminal.
- Code under test can subvert a run from inside the test process in ways no static check sees.
- A failing baseline proves a test can fail, not that it is a strong test.
- Token heartbeats are self-reported.

A report that turns one of these into something cheaper or quieter than described is still welcome.

## Reporting

Please report privately through GitHub's **Report a vulnerability** button on this repository (Security → Advisories), not in a public issue. Include the Telos version, the platform, and a minimal reproduction: ideally a failing test in the style of `tests/test_v05_trust_boundary.py`.

Only the latest release receives fixes.

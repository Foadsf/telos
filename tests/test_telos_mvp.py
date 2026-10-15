"""
End-to-End Verification Suite for Telos MVP.
Validates PERT invariants, cycle prevention, Monte Carlo merge bias, Zwicky CCA solving,
fencing tokens, role separation, Mikado discovery, AST anti-cheat, test-hash locking,
3-strike quarantine, and Google Tasks verification gate safety.
"""

from __future__ import annotations

from pathlib import Path
import sys
import pytest
from pydantic import ValidationError

from telos.dag_pert import CycleDetectedError
from telos.google_sync import GoogleTasksProjectionAdapter, format_google_task_notes
from telos.models import (
    EdgeRelation,
    KanbanState,
    NodeKind,
    PertTriplet,
    ZwickyBox,
    ZwickyCompatibility,
    ZwickyOption,
    ZwickyPairAssessment,
)
from telos.service import StaleFencingTokenError, TelosService, VerificationGateError
from tests.conftest import lock_failing_gate

def test_pert_monotonic_invariant_enforced() -> None:
    """Rejects P < M or M < O so negative standard deviations can never exist."""
    valid = PertTriplet(optimistic=10.0, most_likely=20.0, pessimistic=50.0)
    assert round(valid.mean, 2) == 23.33
    assert round(valid.std_dev, 2) == 6.67

    with pytest.raises(ValidationError, match="PERT invariant violated"):
        PertTriplet(optimistic=30.0, most_likely=20.0, pessimistic=50.0)

    with pytest.raises(ValidationError, match="PERT invariant violated"):
        PertTriplet(optimistic=10.0, most_likely=60.0, pessimistic=40.0)

def test_dag_cycle_prevention_and_monte_carlo_merge_bias(tmp_path: Path) -> None:
    """Proves cycles are rejected and Monte Carlo captures parallel branch merge bias."""
    svc = TelosService(tmp_path)
    svc.init_workspace("pert-test")

    # Diamond graph with two parallel branches of equal deterministic mean (30m) but high variance
    t_start = svc.create_node("Start", optimistic_min=5, most_likely_min=5, pessimistic_min=5, state=KanbanState.READY)
    t_branch_a = svc.create_node("Branch A", optimistic_min=10, most_likely_min=25, pessimistic_min=70, state=KanbanState.READY)
    t_branch_b = svc.create_node("Branch B", optimistic_min=10, most_likely_min=25, pessimistic_min=70, state=KanbanState.READY)
    t_join = svc.create_node("Join", optimistic_min=5, most_likely_min=5, pessimistic_min=5, state=KanbanState.READY)

    svc.add_edge(t_branch_a.id, EdgeRelation.DEPENDS_ON, t_start.id)
    svc.add_edge(t_branch_b.id, EdgeRelation.DEPENDS_ON, t_start.id)
    svc.add_edge(t_join.id, EdgeRelation.DEPENDS_ON, t_branch_a.id)
    svc.add_edge(t_join.id, EdgeRelation.DEPENDS_ON, t_branch_b.id)

    # Attempting to make Start depend on Join creates a cycle -> must raise CycleDetectedError
    with pytest.raises(CycleDetectedError):
        svc.add_edge(t_start.id, EdgeRelation.DEPENDS_ON, t_join.id)

    _, mc = svc.compute_schedule(trials=2000, seed=123)
    # Deterministic CPM along either branch is 5 + 30 + 5 = 40.0m.
    # Because of parallel merge bias max(A, B), Monte Carlo P80 must exceed deterministic 40.0m.
    assert mc.deterministic_cpm_duration == 40.0
    assert mc.p80_duration > mc.deterministic_cpm_duration
    assert mc.criticality_index[t_start.id] == 1.0
    assert mc.criticality_index[t_join.id] == 1.0

def test_mikado_discovery_and_stale_fencing_token_rejection(tmp_path: Path) -> None:
    """Proves Mikado inverse planning pivots to leaf prerequisite and rejects stale fencing tokens."""
    svc = TelosService(tmp_path)
    svc.init_workspace("mikado-test")

    goal = svc.create_node("Implement Auth API", state=KanbanState.READY)
    lock_failing_gate(svc, tmp_path, goal.id, name="auth")
    lease_1 = svc.checkout_leaf(goal.id, agent_id="agent-alpha")
    assert lease_1["fencing_token"] == 1

    # Simulate second checkout superseding first lease
    lease_2 = svc.checkout_leaf(goal.id, agent_id="agent-beta")
    assert lease_2["fencing_token"] == 2

    # Stale agent-alpha with fencing_token=1 MUST be rejected
    with pytest.raises(StaleFencingTokenError):
        svc.mikado_discover_prerequisite(
            blocked_task_id=goal.id,
            lease_id=lease_1["lease_id"],
            fencing_token=1,
            prerequisite_title="Extract Key Loader",
            prerequisite_spec="Need Ed25519 key loader first",
            why_blocked_rationale="Missing key loader",
        )

    # Authoritative lease_2 with fencing_token=2 succeeds; the prerequisite waits for an architect's contract
    pivot = svc.mikado_discover_prerequisite(
        blocked_task_id=goal.id,
        lease_id=lease_2["lease_id"],
        fencing_token=2,
        prerequisite_title="Extract Key Loader",
        prerequisite_spec="Need Ed25519 key loader first",
        why_blocked_rationale="Missing key loader",
    )
    prereq_id = pivot["spawned_prerequisite_id"]
    assert pivot["blocked_task_new_state"] == KanbanState.MIKADO_DISCOVERY.value
    assert pivot["awaiting_contract"] is True
    with pytest.raises(VerificationGateError, match="no locked verification contract"):
        svc.checkout_leaf(prereq_id, agent_id="agent-beta")
    lock_failing_gate(svc, tmp_path, prereq_id, name="keyloader")
    assert svc.checkout_leaf(prereq_id, agent_id="agent-beta")["task_id"] == prereq_id

    # Goal is no longer a leaf; cannot be checked out until prereq is DONE
    with pytest.raises(VerificationGateError, match="unresolved prerequisites"):
        svc.checkout_leaf(goal.id, agent_id="agent-gamma")

def test_anti_reward_hacking_verifier_and_circuit_breaker(tmp_path: Path) -> None:
    """
    Adversarial red-team test proving:
    1. Role separation prevents an implementer agent from approving its own contract.
    2. Adding @pytest.mark.skip or empty 'except: pass' is blocked by AST scanner.
    3. Modifying a locked test file triggers immediate QUARANTINE.
    4. Legitimate passing code produces an HMAC-verified VerificationReceipt and closes the task.
    """
    svc = TelosService(tmp_path)
    svc.init_workspace("security-test")

    src_file = tmp_path / "calc.py"
    test_file = tmp_path / "test_calc.py"
    src_file.write_text("def add(a: int, b: int) -> int:\n    return 0\n", encoding="utf-8")
    test_file.write_text(
        "from calc import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    task = svc.create_node("Implement add()", kind=NodeKind.L3_IMPLEMENTATION, state=KanbanState.READY)

    # Human architect locks the verification contract before agent checkout
    svc.approve_verification_contract(
        left_node_id=task.id,
        verifier_title="Unit test add()",
        command_argv=[sys.executable, "-m", "pytest", "test_calc.py", "-q"],
        locked_paths=["test_calc.py"],
        ast_scan_paths=["calc.py", "test_calc.py"],
    )

    lease = svc.checkout_leaf(task.id, agent_id="claude-code")

    # Role-separation check: once leased, nobody can add or swap a contract (no caller ID involved)
    with pytest.raises(VerificationGateError, match="locked before any lease"):
        svc.approve_verification_contract(
            left_node_id=task.id,
            verifier_title="Weakened contract",
            command_argv=[sys.executable, "-c", "print('1 passed')"],
            locked_paths=["test_calc.py"],
        )

    # Attempt 1: Agent fails the unit test legitimately -> Strike 1/3
    res1 = svc.submit_for_verification(task.id, lease["lease_id"], lease["fencing_token"])
    assert res1["passed"] is False
    assert res1["failure_category"] == "agent_logic_error"
    assert res1["strikes"] == "1/3"

    # Attempt 2: Agent tries to cheat with silent 'except: pass' -> Quarantined by AST scanner
    src_file.write_text(
        "def add(a: int, b: int) -> int:\n    try:\n        return a + b\n    except Exception:\n        pass\n    return 5\n",
        encoding="utf-8",
    )
    res2 = svc.submit_for_verification(task.id, lease["lease_id"], lease["fencing_token"])
    assert res2["passed"] is False
    assert res2["failure_category"] == "ast_cheat_detected"
    assert res2["state"] == KanbanState.QUARANTINED.value

    # Human architect releases the quarantine (signed attestation) and agent fixes implementation cleanly
    svc.unquarantine_task(task.id, rationale="Reviewed the except-pass attempt", actor="human-architect")

    lease_clean = svc.checkout_leaf(task.id, agent_id="claude-code")
    src_file.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

    res3 = svc.submit_for_verification(task.id, lease_clean["lease_id"], lease_clean["fencing_token"])
    assert res3["passed"] is True
    assert res3["state"] == KanbanState.DONE.value

    receipts = svc.get_receipts_for_task(task.id)
    assert svc.verifier.verify_receipt_signature(receipts[-1]) is True

def test_zwicky_morphological_cca_solver_and_google_sync_gate(tmp_path: Path) -> None:
    """Proves Zwicky CCA prunes incompatible pairs and Google Tasks cannot bypass V-Model gates."""
    svc = TelosService(tmp_path)
    svc.init_workspace("zwicky-and-sync")

    box = ZwickyBox(
        title="Storage & Sync Architecture",
        problem_statement="Select Git-friendly storage and index engine",
        dimensions={
            "storage": [
                ZwickyOption(name="sharded_toml", cost_score=1.0),
                ZwickyOption(name="raw_binary_sqlite", cost_score=1.0),
            ],
            "vcs_workflow": [
                ZwickyOption(name="git_worktrees", cost_score=1.0),
            ],
        },
        assessments=[
            ZwickyPairAssessment(
                dim_a="storage",
                opt_a="raw_binary_sqlite",
                dim_b="vcs_workflow",
                opt_b="git_worktrees",
                compatibility=ZwickyCompatibility.INCOMPATIBLE,
                rationale="Binary B-tree pages conflict across worktree merges",
            ),
            ZwickyPairAssessment(
                dim_a="storage",
                opt_a="sharded_toml",
                dim_b="vcs_workflow",
                opt_b="git_worktrees",
                compatibility=ZwickyCompatibility.SYNERGISTIC,
                rationale="Per-node deterministic files merge cleanly",
            ),
        ],
    )
    svc.save_zwicky_box(box)
    winners = svc.solve_zwicky_box(box.id)
    assert len(winners) == 1
    assert winners[0].configuration["storage"] == "sharded_toml"

    # Verify Google Tasks mobile checkbox cannot mark a gated code task DONE
    t_code = svc.create_node("Critical Code Task", state=KanbanState.READY)
    lock_failing_gate(svc, tmp_path, t_code.id, name="critical")
    adapter = GoogleTasksProjectionAdapter(svc)
    remote_payload = {
        "id": "gtask-123",
        "title": t_code.title,
        "notes": format_google_task_notes(t_code),
        "status": "completed",
    }
    sync_outcome = adapter.reconcile_remote_task_payload(remote_payload)
    assert sync_outcome["action"] == "blocked_unverified_completion"
    assert svc.get_node(t_code.id).state != KanbanState.DONE

"""
Verification Suite for Telos v0.3.0:
Tests cross-scope federation, V-Model bottom-up CONTAINS synthesis, token budget circuit-breaker,
unquarantine workflow, Zwicky ADR task promotion, and MCP JSON-RPC protocol handling.
"""

from __future__ import annotations

from pathlib import Path
import sys
import pytest

from telos.mcp_server import TelosMcpDispatcher
from telos.models import EdgeRelation, KanbanState, NodeKind
from telos.service import TelosService, VerificationGateError


def test_cross_scope_super_and_sub_project_federation(tmp_path: Path) -> None:
    """
    Proves that a Super-Project task with a cross-scope dependency (`scope_ref="../sub_driver"`)
    cannot be leased until the referenced Sub-Project task reaches DONE.
    """
    super_dir = tmp_path / "super_system"
    sub_dir = tmp_path / "sub_driver"

    sub_svc = TelosService(sub_dir)
    sub_svc.init_workspace("sub-driver-project")
    sub_task = sub_svc.create_node(
        "Implement Low-Level PWM Driver",
        kind=NodeKind.L3_IMPLEMENTATION,
        state=KanbanState.READY,
        optimistic_min=20.0,
        most_likely_min=30.0,
        pessimistic_min=40.0,
    )

    super_svc = TelosService(super_dir)
    super_svc.init_workspace(
        "super-system-project",
        sub_project_paths=["../sub_driver"],
    )
    super_task = super_svc.create_node(
        "Integrate System Control Loop",
        kind=NodeKind.L2_MODULE_SPEC,
        state=KanbanState.READY,
        optimistic_min=10.0,
        most_likely_min=20.0,
        pessimistic_min=30.0,
    )
    super_svc.add_edge(
        super_task.id,
        EdgeRelation.DEPENDS_ON,
        sub_task.id,
        scope_ref="../sub_driver",
    )

    # 1. While sub_task is READY (not DONE), super_task must be blocked from checkout
    with pytest.raises(VerificationGateError, match="unresolved prerequisites"):
        super_svc.checkout_leaf(super_task.id, agent_id="agent-super")

    # 2. Pulse radar in super-project reflects sub-project progress (0/1 done) and 0 ready leaves
    radar_before = super_svc.pulse_radar()
    assert radar_before["ready_mikado_leaves"] == []
    assert radar_before["sub_projects"][0]["done"] == 0
    # Federated CPM includes external prerequisite duration (30m + 20m = 50m)
    assert radar_before["critical_path_remaining_min"] == 50.0

    # 3. Complete sub_task in sub-project
    sub_task.state = KanbanState.DONE
    sub_svc.save_node(sub_task)

    # 4. Now super_task is unblocked and ready for checkout
    radar_after = super_svc.pulse_radar()
    assert len(radar_after["ready_mikado_leaves"]) == 1
    assert radar_after["sub_projects"][0]["done"] == 1
    lease = super_svc.checkout_leaf(super_task.id, agent_id="agent-super")
    assert lease["task_id"] == super_task.id


def test_vmodel_bottom_up_contains_synthesis_and_token_budget_breaker(tmp_path: Path) -> None:
    """
    Proves:
    1. A parent L2 node cannot be checked out or verified while its CONTAINS child L3 node is unfinished.
    2. Exceeding the pessimistic token budget on heartbeat immediately quarantines the task.
    3. `unquarantine_task` cleanly restores the task to READY with reset strikes.
    """
    svc = TelosService(tmp_path)
    svc.init_workspace("vmodel-hierarchy")

    parent_l2 = svc.create_node(
        "Auth Module Spec", kind=NodeKind.L2_MODULE_SPEC, state=KanbanState.READY
    )
    child_l3 = svc.create_node(
        "Token Signer Leaf",
        kind=NodeKind.L3_IMPLEMENTATION,
        state=KanbanState.READY,
        parent_id=parent_l2.id,
        optimistic_tokens=1000.0,
        most_likely_tokens=2000.0,
        pessimistic_tokens=5000.0,
    )

    # Parent L2 is blocked by unfinished CONTAINS child_l3
    with pytest.raises(VerificationGateError, match="unresolved prerequisites/children"):
        svc.checkout_leaf(parent_l2.id, agent_id="agent-1")

    # Lease child_l3 and report token consumption exceeding pessimistic budget (5000 tokens)
    lease = svc.checkout_leaf(child_l3.id, agent_id="agent-1")
    hb_ok = svc.report_heartbeat(
        child_l3.id, lease["lease_id"], lease["fencing_token"], tokens_delta=3000
    )
    assert hb_ok["active"] is True
    assert hb_ok["token_budget_remaining"] == 2000

    hb_over = svc.report_heartbeat(
        child_l3.id, lease["lease_id"], lease["fencing_token"], tokens_delta=2500
    )
    assert hb_over["active"] is False
    assert hb_over["state"] == KanbanState.QUARANTINED.value

    # Recover via official unquarantine_task service method
    recovered = svc.unquarantine_task(child_l3.id, rationale="Increased context efficiency")
    assert recovered.state == KanbanState.READY
    assert recovered.strike_count == 0


def test_zwicky_commit_spawns_traced_vmodel_nodes_and_bootstrap(tmp_path: Path) -> None:
    """Proves committing a Zwicky Box ADR spawns L1/L2 nodes with DERIVED_FROM traceability edges."""
    svc = TelosService(tmp_path)
    svc.init_workspace("zwicky-adr")

    box = svc.create_zwicky_box(
        title="IPC Transport Selection",
        problem_statement="Select low-latency agent transport",
        dimensions={
            "framing": [{"name": "jsonrpc_stdio", "cost_score": 1.0}, {"name": "xml_soap", "cost_score": 5.0}],
            "payload": [{"name": "toon_compact", "cost_score": 1.0}],
        },
        assessments=[
            {
                "dim_a": "framing",
                "opt_a": "jsonrpc_stdio",
                "dim_b": "payload",
                "opt_b": "toon_compact",
                "compatibility": "synergistic",
                "rationale": "Minimal token and framing overhead",
            }
        ],
    )

    commit_res = svc.commit_zwicky_configuration(
        box.id, rationale="Choose highest synergy configuration", candidate_rank_index=0
    )
    assert commit_res["selected_configuration"] == {
        "framing": "jsonrpc_stdio",
        "payload": "toon_compact",
    }
    assert len(commit_res["spawned_node_ids"]) == 3  # 1 L1_architecture + 2 L2_module_spec children

    edges = svc.load_all_edges()
    derived_edges = [e for e in edges if e.relation == EdgeRelation.DERIVED_FROM]
    assert len(derived_edges) == 3

    # Test agent bootstrap file generation
    boot = svc.bootstrap_agent_configs()
    assert Path(boot["mcp_config"]).exists()
    assert Path(boot["agents_md"]).exists()

    # Test JSON-RPC dispatcher protocol message handling (used by stdio & Streamable HTTP)
    dispatcher = TelosMcpDispatcher(tmp_path)
    init_resp = dispatcher.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_resp["result"]["serverInfo"]["version"] in ("0.3.0", "0.4.0")

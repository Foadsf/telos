"""
Token-frugal agent protocol.

Every LLM step re-sends the whole context, so an agent's cost is roughly steps x context size. These
tests pin the round trips Telos saves (checkout without an id, verify with next) and the compact toon
views, and prove each shortcut still passes through the same gates as the long way round.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from telos.cli import main as cli_main
from telos.mcp_server import TelosMcpDispatcher
from telos.models import KanbanState
from telos.service import AGENT_PROTOCOL_BLOCK, StaleFencingTokenError, TelosService, VerificationGateError
from telos.toml_codec import read_toml, write_canonical_toml
from tests.conftest import lock_failing_gate


def _svc(tmp_path: Path, **cfg: int) -> TelosService:
    svc = TelosService(tmp_path)
    svc.init_workspace("frugal", **cfg)
    return svc


def _gated(svc: TelosService, root: Path, title: str, name: str) -> tuple[str, Path]:
    task = svc.create_node(title, state=KanbanState.READY)
    _, marker = lock_failing_gate(svc, root, task.id, name=name)
    return task.id, marker


# ----------------------------------------------------------------------------- checkout without an id
def test_checkout_without_id_skips_a_contractless_leaf(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    # Shorter estimate, so it sorts first among ready leaves: picking must skip it for lack of a contract.
    bare = svc.create_node("No exam yet", state=KanbanState.READY,
                           optimistic_min=1.0, most_likely_min=1.0, pessimistic_min=1.0)
    gated, _ = _gated(svc, tmp_path, "Has exam", "has")
    assert svc.build_dag_engine().get_unblocked_mikado_leaves()[0].id == bare.id
    assert svc.checkout_leaf(None, agent_id="impl")["task_id"] == gated


def test_checkout_without_id_refuses_with_the_reason(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.create_node("No exam yet", state=KanbanState.READY)
    with pytest.raises(VerificationGateError, match="awaiting a locked contract"):
        svc.checkout_leaf(None, agent_id="impl")
    board = TelosMcpDispatcher(tmp_path).call_tool("telos_pulse", {"agent_id": "impl"})
    assert "awaiting a locked contract" in board and "ready:" not in board


def test_checkout_without_id_resumes_own_lease_but_never_takes_another_agents(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    task, _ = _gated(svc, tmp_path, "Only task", "only")
    first = svc.checkout_leaf(task, agent_id="impl")

    with pytest.raises(VerificationGateError, match="leased by impl"):
        svc.checkout_leaf(None, agent_id="other")

    again = svc.checkout_leaf(None, agent_id="impl")
    assert again["task_id"] == task and again["fencing_token"] == first["fencing_token"] + 1
    with pytest.raises(StaleFencingTokenError):
        svc.submit_for_verification(task, first["lease_id"], first["fencing_token"])


def test_checkout_without_id_still_refuses_a_tampered_contract(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    task = svc.create_node("Fix it", state=KanbanState.READY)
    r_id, _ = lock_failing_gate(svc, tmp_path, task.id, name="fix")
    r_file = tmp_path / ".telos" / "nodes" / f"{r_id}.toml"
    data = read_toml(r_file)
    data["contract"]["command_argv"] = [sys.executable, "-c", "print('1 passed')"]
    write_canonical_toml(r_file, data)
    co = TelosMcpDispatcher(tmp_path).call_tool("telos_checkout_leaf", {"agent_id": "impl"})
    assert co.startswith("TELOS_ERROR") and "fail their seal" in co


# ----------------------------------------------------------------------------- verify with next
def test_verify_next_leases_the_next_task_only_on_a_pass(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    t1, m1 = _gated(svc, tmp_path, "First", "first")
    t2, m2 = _gated(svc, tmp_path, "Second", "second")
    markers = {t1: m1, t2: m2}

    lease = svc.checkout_leaf(None, agent_id="impl")
    fail = svc.submit_for_verification(lease["task_id"], lease["lease_id"], lease["fencing_token"], lease_next=True)
    assert fail["passed"] is False and "next" not in fail

    markers[lease["task_id"]].touch()
    ok = svc.submit_for_verification(lease["task_id"], lease["lease_id"], lease["fencing_token"], lease_next=True)
    nxt = ok["next"]
    assert ok["passed"] is True and nxt["task_id"] == ({t1, t2} - {lease["task_id"]}).pop()

    markers[nxt["task_id"]].touch()
    last = svc.submit_for_verification(nxt["task_id"], nxt["lease_id"], nxt["fencing_token"], lease_next=True)
    assert last["passed"] is True and "No leasable task" in last["next"]["none"]
    assert all(svc.get_node(t).state == KanbanState.DONE for t in (t1, t2))


def test_verify_next_goes_through_the_wip_gate(tmp_path: Path) -> None:
    svc = _svc(tmp_path, max_wip_total=1)
    t1, m1 = _gated(svc, tmp_path, "First", "first")
    t2, _ = _gated(svc, tmp_path, "Second", "second")
    t3, _ = _gated(svc, tmp_path, "Third", "third")
    lease = svc.checkout_leaf(t1, agent_id="impl")
    svc.checkout_leaf(t2, agent_id="other", ignore_wip_limit=True)
    m1.touch()
    ok = svc.submit_for_verification(t1, lease["lease_id"], lease["fencing_token"], lease_next=True)
    assert ok["passed"] is True and "WIP limit" in ok["next"]["none"]
    assert svc.get_node(t3).state == KanbanState.READY


def test_verify_next_never_leases_a_task_whose_contract_was_tampered(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    t1, m1 = _gated(svc, tmp_path, "First", "first")
    t2 = svc.create_node("Second", state=KanbanState.READY)
    r2, _ = lock_failing_gate(svc, tmp_path, t2.id, name="second")
    r_file = tmp_path / ".telos" / "nodes" / f"{r2}.toml"
    data = read_toml(r_file)
    data["contract"]["locked_paths"] = []
    write_canonical_toml(r_file, data)

    lease = svc.checkout_leaf(t1, agent_id="impl")
    m1.touch()
    ok = svc.submit_for_verification(t1, lease["lease_id"], lease["fencing_token"], lease_next=True)
    assert ok["passed"] is True and "seal" in ok["next"]["none"]
    assert svc.get_node(t2.id).state == KanbanState.READY


# ----------------------------------------------------------------------------- compact views, one per route
def test_mcp_round_trip_and_cli_print_the_same_terse_views(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    svc = _svc(tmp_path)
    t1, m1 = _gated(svc, tmp_path, "First", "first")
    _gated(svc, tmp_path, "Second", "second")
    mcp = TelosMcpDispatcher(tmp_path)

    co = mcp.call_tool("telos_checkout_leaf", {"agent_id": "impl"})
    for field in ("task_id: " + t1, "lease_id:", "fencing_token:", "locked_tests:"):
        assert field in co
    for noise in ("verifier_id", "token_budget", "worktree_path", "strikes", "[]"):
        assert noise not in co

    board_mcp = mcp.call_tool("telos_pulse", {"agent_id": "impl"})
    assert cli_main(["--root", str(tmp_path), "--format", "toon", "pulse", "--agent", "impl"]) == 0
    assert capsys.readouterr().out.strip() == board_mcp
    assert "counts: 0/2 tasks done" in board_mcp and ": []" not in board_mcp and ": {}" not in board_mcp

    lease_id = co.split("lease_id: ", 1)[1].splitlines()[0]
    m1.touch()
    done = mcp.call_tool("telos_submit_verification",
                         {"task_id": t1, "lease_id": lease_id, "fencing_token": 1, "next": "true"})
    assert "passed: True" in done and "next:" in done and "verified_git_ref" not in done
    assert done.split("next:", 1)[1].count("lease_id:") == 1

    # JSON stays the complete record for machines (the bench scorer, the pi footer, the OpenCode toast).
    assert cli_main(["--root", str(tmp_path), "--format", "json", "pulse"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert "critical_path_remaining_min" in full and full["counts"]["total"] == 4


def test_cli_gate_refusal_is_one_line_not_a_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    svc = _svc(tmp_path)
    task = svc.create_node("No exam yet", state=KanbanState.READY)
    assert cli_main(["--root", str(tmp_path), "--format", "toon", "checkout", task.id, "--agent", "impl"]) == 2
    err = capsys.readouterr().err
    assert "no locked verification contract" in err and "Traceback" not in err and err.count("\n") == 1


def test_protocol_block_still_states_every_gate() -> None:
    for phrase in ("cannot mark one done", "sealed", "quarantines", "human-only", "never work around a gate",
                   "telos_checkout_leaf", "telos_submit_verification", "telos_mikado_discover", "fencing_token"):
        assert phrase in AGENT_PROTOCOL_BLOCK
    assert "heartbeat" not in AGENT_PROTOCOL_BLOCK
    assert len(AGENT_PROTOCOL_BLOCK) < 1600

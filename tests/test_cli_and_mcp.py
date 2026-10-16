"""
Comprehensive CLI and MCP Integration Tests for Telos.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from telos.cli import main as cli_main
from telos.mcp_server import TelosMcpDispatcher
from telos.models import ZwickyBox, ZwickyOption
from telos.service import TelosService
from telos.ulid import new_ulid
from tests.conftest import lock_failing_gate

def test_ulid_monotonic_and_distinct() -> None:
    ids = [new_ulid() for _ in range(100)]
    assert len(set(ids)) == 100
    assert ids == sorted(ids)

def test_cli_lifecycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 1. init
    rc = cli_main(["--root", str(tmp_path), "--format", "json", "init", "--name", "cli-test-proj"])
    assert rc == 0
    out = capsys.readouterr().out
    cfg = json.loads(out)
    assert cfg["project_name"] == "cli-test-proj"

    # 2. add parent epic
    rc = cli_main(["--root", str(tmp_path), "--format", "json", "add", "Epic 1", "--kind", "L1_architecture"])
    assert rc == 0
    epic_out = json.loads(capsys.readouterr().out)
    epic_id = epic_out["id"]

    # 3. add child task with parent and dependency
    rc = cli_main([
        "--root", str(tmp_path), "--format", "json", "add", "Task 1",
        "--kind", "L3_implementation", "--parent", epic_id,
        "--opt-min", "10", "--likely-min", "20", "--pess-min", "30"
    ])
    assert rc == 0
    task_out = json.loads(capsys.readouterr().out)
    task_id = task_out["id"]

    # 4. pulse
    rc = cli_main(["--root", str(tmp_path), "--format", "json", "pulse", "--agent", "test-agent"])
    assert rc == 0
    pulse_data = json.loads(capsys.readouterr().out)
    assert pulse_data["counts"]["total"] >= 2

    # 5. architect locks a failing contract (library call stands in for the human CLI), then checkout leaf
    lock_failing_gate(TelosService(tmp_path), tmp_path, task_id, name="task1")
    rc = cli_main(["--root", str(tmp_path), "--format", "json", "checkout", task_id, "--agent", "test-agent"])
    assert rc == 0
    co_data = json.loads(capsys.readouterr().out)
    assert co_data["task_id"] == task_id
    lease_id = co_data["lease_id"]
    token = co_data["fencing_token"]

    # 6. mikado discovery
    rc = cli_main([
        "--root", str(tmp_path), "--format", "json", "mikado", task_id,
        "--lease-id", lease_id, "--token", str(token),
        "--prereq-title", "Prereq Task 0", "--why", "Needs setup"
    ])
    assert rc == 0
    mik_data = json.loads(capsys.readouterr().out)
    assert mik_data["blocked_task_id"] == task_id
    assert mik_data["awaiting_contract"] is True

    # 7. schedule
    rc = cli_main(["--root", str(tmp_path), "--format", "json", "schedule", "--trials", "500"])
    assert rc == 0
    sched_data = json.loads(capsys.readouterr().out)
    assert "cpm" in sched_data
    assert "monte_carlo" in sched_data

def test_mcp_dispatcher_tools(tmp_path: Path) -> None:
    svc = TelosService(tmp_path)
    svc.init_workspace("mcp-proj")
    dispatcher = TelosMcpDispatcher(tmp_path)

    # 1. tool_definitions check
    defs = dispatcher.tool_definitions()
    tool_names = {t["name"] for t in defs}
    assert "telos_pulse" in tool_names
    assert "telos_decompose" in tool_names
    assert "telos_checkout_leaf" in tool_names
    assert "telos_mikado_discover" in tool_names
    assert "telos_submit_verification" in tool_names
    assert "telos_schedule_pert" in tool_names
    assert "telos_zwicky_solve" in tool_names

    # 2. call telos_pulse
    pulse_res = dispatcher.call_tool("telos_pulse", {"agent_id": "agent-1"})
    assert "counts:" in pulse_res

    # 3. call telos_decompose
    decomp_res = dispatcher.call_tool("telos_decompose", {
        "title": "Backend Microservice",
        "kind": "L3_implementation",
        "optimistic_min": 10.0,
        "most_likely_min": 20.0,
        "pessimistic_min": 40.0,
    })
    assert "created_task_id:" in decomp_res
    task_id = None
    for line in decomp_res.splitlines():
        if "created_task_id:" in line:
            task_id = line.split(":", 1)[1].strip()
    assert task_id is not None

    # 4. checkout via MCP (after an architect-side lock)
    lock_failing_gate(svc, tmp_path, task_id, name="backend")
    co_res = dispatcher.call_tool("telos_checkout_leaf", {"task_id": task_id, "agent_id": "mcp-agent"})
    assert "lease_id:" in co_res
    lease_id = None
    for line in co_res.splitlines():
        if "lease_id:" in line:
            lease_id = line.split(":", 1)[1].strip()
    assert lease_id is not None

    # 5. schedule via MCP
    sched_res = dispatcher.call_tool("telos_schedule_pert", {"trials": 100})
    assert "deterministic_cpm_min:" in sched_res

    # 6. zwicky solve via MCP
    box = ZwickyBox(
        title="Protocol Choice",
        problem_statement="Choose IPC",
        dimensions={
            "proto": [ZwickyOption(name="grpc"), ZwickyOption(name="jsonrpc")],
            "transport": [ZwickyOption(name="tcp"), ZwickyOption(name="stdio")],
        },
        assessments=[],
    )
    svc.save_zwicky_box(box)
    zw_res = dispatcher.call_tool("telos_zwicky_solve", {"box_id": box.id})
    assert "configuration" in zw_res

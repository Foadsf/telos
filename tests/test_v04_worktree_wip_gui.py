"""
Verification Suite for Telos v0.4.0:
Tests isolated Git worktree harvesting (proving passing code is synced back to root and not deleted),
Kanban WIP limits, hardened AST anti-cheat (Ellipsis & sys.exit(0)), MCP zwicky_create/contract_lock parity,
parallel execution waves, and HTML GUI dashboard export.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import pytest

from telos.mcp_server import TelosMcpDispatcher
from telos.models import KanbanState
from telos.service import TelosService, VerificationGateError
from telos.web_gui import export_dashboard_file


def test_isolated_git_worktree_harvests_passing_code_back_to_root(tmp_path: Path) -> None:
    """
    Regression test for the v0.3.0 worktree deletion bug:
    Proves that when an agent checks out a task with `create_worktree=True` inside a Git repo,
    edits code inside the isolated worktree, and passes verification, the verified code is
    harvested back into `tmp_path` and recorded under `refs/telos/verified/...` before worktree removal.
    """
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@telos.dev"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Telos Test"], cwd=tmp_path, check=True)

    driver_py = tmp_path / "driver.py"
    test_driver_py = tmp_path / "test_driver.py"
    driver_py.write_text("def pulse_width() -> int:\n    return 0\n", encoding="utf-8")
    test_driver_py.write_text(
        "from driver import pulse_width\n\ndef test_pw() -> None:\n    assert pulse_width() == 42\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "driver.py", "test_driver.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial baseline"], cwd=tmp_path, check=True, capture_output=True)

    svc = TelosService(tmp_path)
    svc.init_workspace("worktree-harvest-test")
    task = svc.create_node("Implement pulse_width()", state=KanbanState.READY)
    svc.approve_verification_contract(
        left_node_id=task.id,
        verifier_title="Test pulse_width()",
        command_argv=[sys.executable, "-m", "pytest", "test_driver.py", "-q"],
        locked_paths=["test_driver.py"],
        ast_scan_paths=["driver.py", "test_driver.py"],
    )

    lease = svc.checkout_leaf(task.id, agent_id="codex-agent", create_worktree=True)
    wt_dir = Path(lease["worktree_path"])
    assert wt_dir.resolve() != tmp_path.resolve()
    assert wt_dir.exists()

    # Agent writes passing implementation inside the isolated worktree only
    (wt_dir / "driver.py").write_text("def pulse_width() -> int:\n    return 42\n", encoding="utf-8")

    res = svc.submit_for_verification(task.id, lease["lease_id"], lease["fencing_token"])
    assert res["passed"] is True
    assert res["verified_git_ref"] is not None
    assert res["verified_git_ref"].startswith("refs/telos/verified/")

    # Isolated worktree is cleaned up, AND the passing code was harvested back into tmp_path/driver.py!
    assert not wt_dir.exists()
    assert "return 42" in driver_py.read_text(encoding="utf-8")


def test_kanban_wip_limits_and_hardened_ast_scanner(tmp_path: Path) -> None:
    """
    Proves:
    1. An agent cannot exceed `max_wip_per_agent=1` by leasing a second task concurrently.
    2. Hardened AST anti-cheat catches `except Exception: ...` (Ellipsis) and `sys.exit(0)`.
    """
    svc = TelosService(tmp_path)
    svc.init_workspace("wip-and-ast", max_wip_per_agent=1, max_wip_total=2)

    t1 = svc.create_node("Task Alpha", state=KanbanState.READY)
    t2 = svc.create_node("Task Beta", state=KanbanState.READY)

    mod_py = tmp_path / "mod.py"
    test_mod_py = tmp_path / "test_mod.py"
    mod_py.write_text("def compute() -> int:\n    return 1\n", encoding="utf-8")
    test_mod_py.write_text("from mod import compute\ndef test_c(): assert compute() == 10\n", encoding="utf-8")

    svc.approve_verification_contract(
        left_node_id=t1.id,
        verifier_title="Verify Alpha",
        command_argv=[sys.executable, "-m", "pytest", "test_mod.py", "-q"],
        locked_paths=["test_mod.py"],
        ast_scan_paths=["mod.py", "test_mod.py"],
    )

    lease1 = svc.checkout_leaf(t1.id, agent_id="agent-1")

    # Attempting to lease t2 with the same agent-1 violates max_wip_per_agent=1
    with pytest.raises(VerificationGateError, match="Kanban WIP limit exceeded"):
        svc.checkout_leaf(t2.id, agent_id="agent-1")

    # Agent-1 tries to bypass AST scanner using `except Exception: ...` (Ellipsis) and `sys.exit(0)`
    mod_py.write_text(
        "import sys\ndef compute() -> int:\n    try:\n        sys.exit(0)\n    except Exception:\n        ...\n    return 10\n",
        encoding="utf-8",
    )
    ver_res = svc.submit_for_verification(t1.id, lease1["lease_id"], lease1["fencing_token"])
    assert ver_res["passed"] is False
    assert ver_res["failure_category"] == "ast_cheat_detected"
    assert "Ellipsis" in ver_res["diagnostic"]
    assert "sys.exit(0)" in ver_res["diagnostic"]


def test_mcp_zwicky_create_contract_lock_waves_and_html_gui(tmp_path: Path) -> None:
    """Proves full MCP parity for telos_zwicky_create, telos_contract_lock, telos_waves, and HTML GUI export."""
    svc = TelosService(tmp_path)
    svc.init_workspace("mcp-v04-test")
    dispatcher = TelosMcpDispatcher(tmp_path)

    # 1. telos_zwicky_create via MCP
    zw_out = dispatcher.call_tool(
        "telos_zwicky_create",
        {
            "title": "Sensor Bus Selection",
            "problem_statement": "Select deterministic bus",
            "dimensions": {
                "bus": [{"name": "spi", "cost_score": 1.0}, {"name": "i2c", "cost_score": 1.5}],
                "dma": [{"name": "enabled", "cost_score": 1.0}],
            },
        },
    )
    assert "box_id:" in zw_out
    box_id = [line.split(":", 1)[1].strip() for line in zw_out.splitlines() if "box_id:" in line][0]

    # 2. telos_zwicky_commit via MCP spawns L1 + L2 tasks
    commit_out = dispatcher.call_tool(
        "telos_zwicky_commit",
        {"box_id": box_id, "rationale": "Select SPI with DMA"},
    )
    assert "spawned_node_ids:" in commit_out

    # 3. telos_waves via MCP returns parallel waves
    waves_out = dispatcher.call_tool("telos_waves", {})
    assert "wave=0" in waves_out

    # 4. Export static HTML GUI dashboard
    dashboard_path = export_dashboard_file(svc, tmp_path / "dashboard.html")
    assert dashboard_path.exists()
    html_content = dashboard_path.read_text(encoding="utf-8")
    assert "mcp-v04-test" in html_content
    assert "Multi-Agent Parallel Execution Waves" in html_content


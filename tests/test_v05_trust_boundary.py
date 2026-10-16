"""
Verification Suite for Telos v0.5.0 trust boundary.

Each test replays a bypass that worked against v0.4.0 through the surface an agent actually has
(MCP dispatcher, workspace files, shell environment) and asserts that it now fails closed.
Positive controls prove the gates still let honest work through.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from telos.cli import main as cli_main
from telos.integrity import IntegrityError
from telos.mcp_server import TelosMcpDispatcher
from telos.models import EdgeRelation, KanbanState, VerificationReceipt
from telos.service import TelosService, VerificationGateError
from telos.toml_codec import read_toml, write_canonical_toml


def _field(toon: str, key: str) -> str:
    for line in toon.splitlines():
        if line.strip().startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{key} not in MCP output:\n{toon}")


def _workspace(tmp_path: Path) -> TelosService:
    """A small project: app.add is WRONG, tests/test_app.py is the honest test."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import add\n\ndef test_add():\n    assert add(2, 2) == 4\n", encoding="utf-8"
    )
    svc = TelosService(tmp_path)
    svc.init_workspace("trust-boundary")
    return svc


def _honest_contract(svc: TelosService, task_id: str) -> str:
    _, r_node = svc.approve_verification_contract(
        left_node_id=task_id,
        verifier_title="Honest add() test",
        command_argv=[sys.executable, "-m", "pytest", "tests/test_app.py", "-q", "-p", "no:cacheprovider"],
        locked_paths=["tests/test_app.py"],
    )
    return r_node.id


def _lease(svc: TelosService, task_id: str, agent: str = "impl") -> tuple[str, int]:
    lease = svc.checkout_leaf(task_id, agent_id=agent)
    return lease["lease_id"], lease["fencing_token"]


# ----------------------------------------------------------------------------- positive controls
def test_control_honest_work_passes_and_wrong_code_fails(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    _honest_contract(svc, task.id)
    lid, tok = _lease(svc, task.id)

    wrong = svc.submit_for_verification(task.id, lid, tok)
    assert wrong["passed"] is False and wrong["failure_category"] == "agent_logic_error"

    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    right = svc.submit_for_verification(task.id, lid, tok)
    assert right["passed"] is True and right["state"] == "done"
    receipt = svc.get_receipts_for_task(task.id)[-1]
    assert receipt.evidence == "junit" and receipt.tests_passed == 1
    assert svc.verifier.verify_receipt_signature(receipt)


def test_control_tamper_after_lock_is_blocked(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    _honest_contract(svc, task.id)
    lid, tok = _lease(svc, task.id)
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import add\n\ndef test_add():\n    assert add(2, 2) == 0\n", encoding="utf-8"
    )
    res = svc.submit_for_verification(task.id, lid, tok)
    assert res["failure_category"] == "test_tamper_attempt" and res["state"] == "quarantined"


# ----------------------------------------------------------------------------- attack 1
def test_attack1_implementer_cannot_write_its_own_exam(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    impl = TelosMcpDispatcher(tmp_path)  # default role: implementer
    names = {t["name"] for t in impl.tool_definitions()}
    assert "telos_contract_lock" not in names and "telos_unquarantine" not in names
    task_id = _field(impl.call_tool("telos_decompose", {"title": "Fix add()"}), "created_task_id")

    out = impl.call_tool(
        "telos_contract_lock",
        {"left_node_id": task_id, "command_argv": [sys.executable, "-c", "print('5 passed')"], "locked_paths": ["app.py"]},
    )
    assert out.startswith("TELOS_ERROR") and "not available to the 'implementer' role" in out

    # No contract, no lease: the implementer cannot start the work without an architect's exam.
    co = impl.call_tool("telos_checkout_leaf", {"task_id": task_id, "agent_id": "impl"})
    assert co.startswith("TELOS_ERROR") and "no locked verification contract" in co

    # Even the architect surface rejects a fake gate: it passes before any work (negative control).
    arch = TelosMcpDispatcher(tmp_path, role="architect")
    fake = arch.call_tool(
        "telos_contract_lock",
        {"left_node_id": task_id, "command_argv": [sys.executable, "-c", "print('5 passed')"], "locked_paths": ["app.py"]},
    )
    assert fake.startswith("TELOS_ERROR") and "Negative control failed" in fake

    # And no contract can be added once a lease exists.
    _honest_contract(svc, task_id)
    _lease(svc, task_id)
    late = arch.call_tool(
        "telos_contract_lock",
        {"left_node_id": task_id, "command_argv": [sys.executable, "-m", "pytest", "tests"], "locked_paths": ["tests"]},
    )
    assert late.startswith("TELOS_ERROR") and "locked before any lease" in late


def test_attack1_human_cli_lock_needs_interactive_terminal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    svc = _workspace(tmp_path)
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    rc = cli_main([
        "--root", str(tmp_path), "contract-lock", task.id, "--path", "tests/test_app.py",
        "--", sys.executable, "-m", "pytest", "tests/test_app.py",
    ])
    assert rc == 3
    assert "human-only" in capsys.readouterr().err


# ----------------------------------------------------------------------------- attack 2
def test_attack2_test_that_cannot_fail_is_rejected_at_lock(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    (tmp_path / "tests" / "test_soft.py").write_text(
        "from app import add\n\ndef test_add():\n    try:\n        assert add(2, 2) == 4\n"
        "    except AssertionError:\n        return None\n",
        encoding="utf-8",
    )
    # The v0.4 blocklist misses this swallow; the negative control does not need to know the trick.
    assert svc.verifier.scan_ast_violations(["tests/test_soft.py"], tmp_path) == []
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    with pytest.raises(VerificationGateError, match="Negative control failed"):
        svc.approve_verification_contract(
            left_node_id=task.id,
            verifier_title="Soft test",
            command_argv=[sys.executable, "-m", "pytest", "tests/test_soft.py", "-q"],
            locked_paths=["tests/test_soft.py"],
        )
    # A human may still accept a green baseline for a regression guard, and it is recorded.
    svc.approve_verification_contract(
        left_node_id=task.id,
        verifier_title="Regression guard",
        command_argv=[sys.executable, "-m", "pytest", "tests/test_soft.py", "-q"],
        locked_paths=["tests/test_soft.py"],
        green_baseline_attested_by="human-architect",
        green_baseline_rationale="guarding existing behaviour",
    )
    assert any(a.action == "green_baseline" for a in svc._read_attestations())


def test_attack2_rigged_harness_is_tamper(tmp_path: Path) -> None:
    """Rigging what runs the tests (conftest hooks, a shadow pytest, PYTEST_ADDOPTS, aborting the run)."""
    svc = _workspace(tmp_path)
    tasks = [svc.create_node(f"Fix add() #{i}", state=KanbanState.READY) for i in range(3)]
    for t in tasks:
        _honest_contract(svc, t.id)

    # (a) conftest.py that rewrites every failure into a pass
    lid, tok = _lease(svc, tasks[0].id, agent="a")
    (tmp_path / "conftest.py").write_text(
        "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n    rep = outcome.get_result()\n    rep.outcome = 'passed'\n",
        encoding="utf-8",
    )
    res = svc.submit_for_verification(tasks[0].id, lid, tok)
    assert res["failure_category"] == "test_tamper_attempt" and "conftest.py" in res["diagnostic"]
    (tmp_path / "conftest.py").unlink()

    # (b) a top-level pytest.py that shadows the real runner for `python -m pytest`
    lid, tok = _lease(svc, tasks[1].id, agent="b")
    (tmp_path / "pytest.py").write_text("print('1 passed')\n", encoding="utf-8")
    res = svc.submit_for_verification(tasks[1].id, lid, tok)
    assert res["failure_category"] == "test_tamper_attempt" and "pytest" in res["diagnostic"]
    (tmp_path / "pytest.py").unlink()

    # (c) code under test kills the run with exit 0 before pytest reports (obfuscated past the AST scan)
    lid, tok = _lease(svc, tasks[2].id, agent="c")
    (tmp_path / "app.py").write_text(
        "import os\ngetattr(os, '_ex' + 'it')(0)\n\ndef add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    res = svc.submit_for_verification(tasks[2].id, lid, tok)
    assert res["passed"] is False and "no structured test report" in res["diagnostic"]


def test_attack2_environment_cannot_steer_the_runner(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _workspace(tmp_path)
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    _honest_contract(svc, task.id)
    lid, tok = _lease(svc, task.id)
    # A plugin outside the workspace, injected through the environment, that turns failures into passes.
    plugin_dir = tmp_path_factory.mktemp("evil-plugin")
    (plugin_dir / "telos_evil_plugin.py").write_text(
        "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n    outcome.get_result().outcome = 'passed'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(plugin_dir))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p telos_evil_plugin")
    # Negative control for the test itself: with this environment a plain run reports a pass.
    rigged = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_app.py", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert rigged.returncode == 0, rigged.stdout + rigged.stderr
    res = svc.submit_for_verification(task.id, lid, tok)
    assert res["passed"] is False and res["failure_category"] == "agent_logic_error"


# ----------------------------------------------------------------------------- attack 3
def test_attack3_direct_state_edits_fail_closed(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    prereq = svc.create_node("Prerequisite", state=KanbanState.READY)
    dependent = svc.create_node("Dependent", state=KanbanState.BLOCKED)
    svc.add_edge(dependent.id, EdgeRelation.DEPENDS_ON, prereq.id)

    node_file = tmp_path / ".telos" / "nodes" / f"{prereq.id}.toml"
    data = read_toml(node_file)
    data["state"] = "done"
    write_canonical_toml(node_file, data)

    loaded = TelosService(tmp_path).get_node(prereq.id)
    assert loaded.state == KanbanState.QUARANTINED and loaded._integrity_error
    radar = svc.pulse_radar()
    assert prereq.id in radar["integrity_violations"]
    assert svc.get_node(dependent.id).state == KanbanState.BLOCKED  # nothing unblocked by the forgery

    # The API refuses DONE without evidence, and creation can't start in a gated state.
    fresh = svc.create_node("Another", state=KanbanState.READY)
    fresh.state = KanbanState.DONE
    with pytest.raises(VerificationGateError, match="cannot be DONE"):
        svc.save_node(fresh)
    with pytest.raises(VerificationGateError, match="cannot be created in state 'done'"):
        svc.create_node("Born done", state=KanbanState.DONE)


def test_attack3_contract_edges_config_and_lease_are_sealed(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    r_id = _honest_contract(svc, task.id)

    # Swap the locked command in the verifier node
    r_file = tmp_path / ".telos" / "nodes" / f"{r_id}.toml"
    original = r_file.read_text(encoding="utf-8")
    data = read_toml(r_file)
    data["contract"]["command_argv"] = [sys.executable, "-c", "print('1 passed')"]
    write_canonical_toml(r_file, data)
    with pytest.raises(IntegrityError, match="fail their seal"):
        svc.checkout_leaf(task.id, agent_id="impl")
    r_file.write_text(original, encoding="utf-8")

    # Re-point the lease at another directory (e.g. a copy with passing code)
    lid, tok = _lease(svc, task.id)
    with sqlite3.connect(tmp_path / ".telos" / ".cache" / "index.sqlite") as conn:
        conn.execute("UPDATE active_leases SET worktree_path = ? WHERE lease_id = ?", (str(tmp_path.parent), lid))
    with pytest.raises(IntegrityError, match="Lease .* fails its seal"):
        svc.submit_for_verification(task.id, lid, tok)

    # Drop a dependency edge or raise the WIP limit by hand
    edges = tmp_path / ".telos" / "edges" / "links.toml"
    raw = read_toml(edges)
    raw["edges"] = []
    write_canonical_toml(edges, raw)
    with pytest.raises(IntegrityError, match="links.toml fails its seal"):
        svc.load_all_edges()
    cfg = tmp_path / ".telos" / "config.toml"
    raw_cfg = read_toml(cfg)
    raw_cfg["max_wip_per_agent"] = 99
    write_canonical_toml(cfg, raw_cfg)
    with pytest.raises(IntegrityError, match="config.toml fails its seal"):
        svc.get_scope_config()


# ----------------------------------------------------------------------------- attack 4
def test_attack4_quarantine_is_released_only_by_a_human(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    svc = _workspace(tmp_path)
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    _honest_contract(svc, task.id)
    lid, tok = _lease(svc, task.id)
    (tmp_path / "tests" / "test_app.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    assert svc.submit_for_verification(task.id, lid, tok)["state"] == "quarantined"

    impl = TelosMcpDispatcher(tmp_path)
    out = impl.call_tool("telos_unquarantine", {"task_id": task.id, "rationale": "I reviewed it myself"})
    assert out.startswith("TELOS_ERROR")

    # Resetting the strike counter in the file is a seal break, not a release.
    node_file = tmp_path / ".telos" / "nodes" / f"{task.id}.toml"
    data = read_toml(node_file)
    data["state"], data["strike_count"] = "ready", 0
    write_canonical_toml(node_file, data)
    co = impl.call_tool("telos_checkout_leaf", {"task_id": task.id, "agent_id": "impl"})
    assert co.startswith("TELOS_ERROR") and "tampering" in co

    # The CLI refuses without an interactive terminal (agents' shells have none).
    assert cli_main(["--root", str(tmp_path), "unquarantine", task.id, "--rationale", "x"]) == 3
    assert "human-only" in capsys.readouterr().err

    # A human release is a signed attestation.
    svc.unquarantine_task(task.id, rationale="Reviewed; test file restored", actor="human-architect")
    assert any(a.action == "unquarantine" and a.task_id == task.id for a in svc._read_attestations())


# ----------------------------------------------------------------------------- attack 5
def test_attack5_key_lives_outside_workspace_and_receipts_are_checked(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    assert tmp_path.resolve() not in svc.keyring.key_path.parents
    assert not list((tmp_path / ".telos").rglob("*.key"))

    # Forge a passing receipt and point a node at it
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    forged = VerificationReceipt(
        task_id=task.id, verifier_node_id=task.id, lease_id="x", fencing_token=1,
        source_tree_digest="0", test_tree_digest="0", command_argv=["pytest"], exit_code=0,
        tests_collected=1, tests_passed=1, tests_failed=0, tests_skipped=0, evidence="junit",
        duration_seconds=0.1, hmac_signature="f" * 64,
    )
    with (tmp_path / ".telos" / "ledger" / "receipts.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(forged.model_dump(mode="json")) + "\n")
    task.last_verified_receipt_id = forged.receipt_id
    task.state = KanbanState.DONE
    with pytest.raises(VerificationGateError, match="cannot be DONE"):
        svc.save_node(task)

    # Deleting the key does not mint a new trust root: everything fails closed until a human reseals.
    svc.keyring.key_path.unlink()
    with pytest.raises(IntegrityError, match="No Telos signing key"):
        TelosService(tmp_path).get_node(task.id)
    with pytest.raises(IntegrityError, match="no signing key"):
        TelosService(tmp_path).init_workspace("again")
    summary = TelosService(tmp_path).reseal_workspace(actor="human-architect", rationale="key restored after review")
    assert summary["resealed_nodes"] >= 1


def test_key_dir_inside_workspace_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELOS_KEY_DIR", str(tmp_path / "keys"))
    with pytest.raises(IntegrityError, match="inside the workspace"):
        TelosService(tmp_path)


# ----------------------------------------------------------------------------- destructive git ops
def test_git_plumbing_never_touches_paths_telos_does_not_own(tmp_path: Path) -> None:
    """A workspace nested in a larger repo, or a lease pointing outside .telos/.cache/worktrees,
    must never reset, clean, snapshot into, or delete the enclosing tree."""
    def git(*args: str, cwd: Path = tmp_path) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "outer.txt").write_text("committed\n", encoding="utf-8")
    git("add", "outer.txt")
    git("commit", "-qm", "outer")
    (tmp_path / "outer.txt").write_text("uncommitted work\n", encoding="utf-8")

    ws = tmp_path / "nested_ws"
    ws.mkdir()
    svc = _workspace(ws)
    assert svc.git_mgr.has_git_commit() is False  # nested: no git plumbing on the enclosing repo

    task = svc.create_node("Fix add()", state=KanbanState.READY)
    _honest_contract(svc, task.id)
    lid, tok = _lease(svc, task.id)
    (ws / "tests" / "test_app.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    assert svc.submit_for_verification(task.id, lid, tok)["state"] == "quarantined"

    # Directly aim the destructive helpers at the enclosing repo: they must refuse.
    svc.git_mgr.snapshot_and_reset_worktree(tmp_path, task.id, "attempt", "HEAD")
    svc.git_mgr.remove_isolated_worktree(tmp_path)
    assert (tmp_path / "outer.txt").read_text(encoding="utf-8") == "uncommitted work\n"
    assert git("for-each-ref", "refs/telos") == ""


# ----------------------------------------------------------------------------- agent bootstrap
def test_bootstrap_merges_and_never_clobbers(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Project rules\n\nKeep this line.\n", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    (tmp_path / "opencode.json").write_text(json.dumps({"model": "chutes/x", "mcp": {"keep": {"type": "local"}}}), encoding="utf-8")

    svc.bootstrap_agent_configs()
    svc.bootstrap_agent_configs()  # idempotent

    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Keep this line." in agents and agents.count("<!-- telos:begin -->") == 1
    mcp = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert set(mcp["mcpServers"]) == {"other", "telos"}
    assert "--role" not in mcp["mcpServers"]["telos"]["args"]
    oc = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
    assert oc["model"] == "chutes/x" and set(oc["mcp"]) == {"keep", "telos"}
    assert "architect" not in json.dumps(oc)
    assert "<!-- telos:begin -->" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- idempotency
def test_zwicky_commit_is_idempotent(tmp_path: Path) -> None:
    svc = _workspace(tmp_path)
    box = svc.create_zwicky_box(
        title="Transport",
        problem_statement="Pick one",
        dimensions={"framing": [{"name": "stdio"}], "payload": [{"name": "toon"}]},
    )
    first = svc.commit_zwicky_configuration(box.id, rationale="only option")
    count = len(svc.load_all_nodes())
    second = svc.commit_zwicky_configuration(box.id, rationale="retry")
    assert second["already_committed"] is True
    assert second["spawned_node_ids"] == first["spawned_node_ids"]
    assert len(svc.load_all_nodes()) == count


def test_mcp_stdio_server_roles_end_to_end(tmp_path: Path) -> None:
    _workspace(tmp_path)
    msgs = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )) + "\n"
    names = {}
    for role in ("implementer", "architect"):
        proc = subprocess.run(
            [sys.executable, "-m", "telos.mcp_server", "--root", str(tmp_path), "--role", role],
            input=msgs, capture_output=True, text=True, timeout=60, check=True,
        )
        lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert lines[0]["result"]["serverInfo"]["version"] == "0.5.0"
        names[role] = {t["name"] for t in lines[1]["result"]["tools"]}
    assert "telos_contract_lock" not in names["implementer"]
    assert "telos_contract_lock" in names["architect"]
    assert not any("unquarantine" in n for n in names["implementer"] | names["architect"])

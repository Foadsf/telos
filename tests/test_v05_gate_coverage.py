"""
Gate checks the v0.5 suite reached only through a redundant path.

A mutation run (each gate disabled in a copy, suite must fail) found four gates whose removal no test
noticed, because another check happened to catch the same input first. Each test below feeds an input
that only the gate under test can stop.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from telos.integrity import IntegrityError
from telos.models import KanbanState
from telos.service import TelosService, VerificationGateError
from telos.toml_codec import read_toml, write_canonical_toml


def _workspace(tmp_path: Path, tests: str) -> TelosService:
    (tmp_path / "tests").mkdir()
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(tests, encoding="utf-8")
    svc = TelosService(tmp_path)
    svc.init_workspace("gate-coverage")
    return svc


ONE_TEST = "from app import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
PYTEST = [sys.executable, "-m", "pytest", "tests/test_app.py", "-q", "-p", "no:cacheprovider"]


def _fix(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


def test_verify_rechecks_the_contract_seal_after_checkout(tmp_path: Path) -> None:
    """The contract is swapped AFTER the lease, so checkout's own seal check cannot see it."""
    svc = _workspace(tmp_path, ONE_TEST)
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    _, r_node = svc.approve_verification_contract(task.id, "add test", PYTEST, ["tests/test_app.py"])
    lease = svc.checkout_leaf(task.id, agent_id="impl")
    r_file = tmp_path / ".telos" / "nodes" / f"{r_node.id}.toml"
    data = read_toml(r_file)
    data["contract"]["command_argv"] = [sys.executable, "-c", "print('1 passed')"]
    write_canonical_toml(r_file, data)
    with pytest.raises(IntegrityError, match="fail their seal"):
        svc.submit_for_verification(task.id, lease["lease_id"], lease["fencing_token"])
    assert svc.get_node(task.id).state != KanbanState.DONE


def test_a_task_quarantined_by_strikes_cannot_be_leased(tmp_path: Path) -> None:
    """Quarantine reached legitimately (seal intact), so only the state check can refuse the lease."""
    svc = _workspace(tmp_path, ONE_TEST)
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    svc.approve_verification_contract(task.id, "add test", PYTEST, ["tests/test_app.py"])
    lease = svc.checkout_leaf(task.id, agent_id="impl")
    for _ in range(task.max_strikes):
        res = svc.submit_for_verification(task.id, lease["lease_id"], lease["fencing_token"])
    assert res["state"] == "quarantined" and not svc.get_node(task.id)._integrity_error
    with pytest.raises(VerificationGateError, match="QUARANTINED after"):
        svc.checkout_leaf(task.id, agent_id="impl")


def test_a_locked_file_outside_the_command_line_is_still_digest_checked(tmp_path: Path) -> None:
    """tests/data.txt is locked but not named in the command, so the harness-surface check skips it."""
    svc = _workspace(tmp_path, "from pathlib import Path\nfrom app import add\n\n"
                               "def test_add():\n"
                               "    assert add(2, 2) == int(Path(__file__).with_name('data.txt').read_text())\n")
    (tmp_path / "tests" / "data.txt").write_text("4", encoding="utf-8")
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    svc.approve_verification_contract(task.id, "add test", PYTEST, ["tests/test_app.py", "tests/data.txt"])
    lease = svc.checkout_leaf(task.id, agent_id="impl")
    (tmp_path / "tests" / "data.txt").write_text("0", encoding="utf-8")  # the wrong code now "passes"
    res = svc.submit_for_verification(task.id, lease["lease_id"], lease["fencing_token"])
    assert res["passed"] is False and res["failure_category"] == "test_tamper_attempt"


def test_min_tests_collected_is_enforced(tmp_path: Path) -> None:
    """Two tests at lock time; deleting one is caught by digests, so instead the command selects one."""
    svc = _workspace(tmp_path, ONE_TEST + "\ndef test_add_zero():\n    assert add(0, 3) == 3\n")
    task = svc.create_node("Fix add()", state=KanbanState.READY)
    svc.approve_verification_contract(task.id, "add test", PYTEST[:3] + ["tests/test_app.py::test_add"] + PYTEST[4:],
                                      ["tests/test_app.py"], min_tests_collected=2)
    lease = svc.checkout_leaf(task.id, agent_id="impl")
    _fix(tmp_path)
    res = svc.submit_for_verification(task.id, lease["lease_id"], lease["fencing_token"])
    assert res["passed"] is False and "fewer than required" in res["diagnostic"]

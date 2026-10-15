"""Shared fixtures: every test gets its own out-of-workspace signing key directory."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from telos.service import TelosService


@pytest.fixture(autouse=True)
def _isolated_key_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELOS_KEY_DIR", str(tmp_path_factory.mktemp("telos-keys")))


def lock_failing_gate(svc: TelosService, root: Path, task_id: str, name: str = "gate") -> tuple[str, Path]:
    """
    Architect-side helper: lock a contract whose test fails until `<name>.ok` exists in the workspace.
    Returns (verifier_node_id, path_that_makes_it_pass).
    """
    marker = root / f"{name}.ok"
    test_file = root / f"test_{name}.py"
    test_file.write_text(
        "from pathlib import Path\n\n"
        f"def test_{name}() -> None:\n"
        f"    assert Path(__file__).with_name('{name}.ok').exists()\n",
        encoding="utf-8",
    )
    _, r_node = svc.approve_verification_contract(
        left_node_id=task_id,
        verifier_title=f"Gate {name}",
        command_argv=[sys.executable, "-m", "pytest", test_file.name, "-q"],
        locked_paths=[test_file.name],
    )
    return r_node.id, marker

"""Trusted Anti-Reward-Hacking Verifier: Whole-Tree SHA Lock, Harness-Surface Lock, Negative Control,
Structured (JUnit) Test Evidence, Hardened AST Anti-Cheat, and HMAC Receipts signed with an out-of-workspace key."""

from __future__ import annotations

import ast
import configparser
from dataclasses import dataclass, field
import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET

from telos.git_worktree import GitPlumbingManager
from telos.integrity import SealKeyring
from telos.models import (
    AttestationRecord,
    FailureCategory,
    VerificationContract,
    VerificationReceipt,
)

# Directories never part of the code/harness surface (VCS, Telos state, caches).
_SKIP_DIRS = {".git", ".telos", "__pycache__", "node_modules", ".pytest_cache", ".pytest_tmp", ".mypy_cache", ".ruff_cache"}
# Files the test harness executes or reads besides the code under test, wherever they appear.
_HOOK_FILENAMES = {"conftest.py", "sitecustomize.py", "usercustomize.py"}
# Environment that changes what the harness runs; never inherited by a verification run.
_ENV_PREFIXES_DROPPED = ("PYTHON", "PYTEST_", "TELOS_INTERNAL_")


class _AstCheatVisitor(ast.NodeVisitor):
    """Detects test skipping decorators, dynamic skip calls, exit(0) spoofing, and exception swallowing."""

    BANNED_SKIP_NAMES = {"skip", "skipIf", "skipUnless", "xfail", "skipTest"}

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[str] = []

    def _check_decorator(self, dec: ast.expr) -> None:
        unwrapped = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(unwrapped, ast.Attribute) and unwrapped.attr in self.BANNED_SKIP_NAMES:
            self.violations.append(
                f"{self.filename}:{dec.lineno}: banned skip/xfail decorator '@...{unwrapped.attr}'"
            )
        elif isinstance(unwrapped, ast.Name) and unwrapped.id in self.BANNED_SKIP_NAMES:
            self.violations.append(
                f"{self.filename}:{dec.lineno}: banned skip decorator '@{unwrapped.id}'"
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for dec in node.decorator_list:
            self._check_decorator(dec)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        for dec in node.decorator_list:
            self._check_decorator(dec)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            self._check_decorator(dec)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in self.BANNED_SKIP_NAMES:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: banned runtime test skip call '{node.func.attr}()'"
                )
            elif (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"sys", "os"}
                and node.func.attr in {"exit", "_exit"}
            ):
                if not node.args or (
                    isinstance(node.args[0], ast.Constant) and node.args[0].value == 0
                ):
                    self.violations.append(
                        f"{self.filename}:{node.lineno}: banned exit-code 0 spoofing '{node.func.value.id}.{node.func.attr}(0)'"
                    )
        elif isinstance(node.func, ast.Name) and node.func.id in self.BANNED_SKIP_NAMES:
            self.violations.append(
                f"{self.filename}:{node.lineno}: banned direct test skip call '{node.func.id}()'"
            )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if len(node.body) == 1:
            stmt = node.body[0]
            if isinstance(stmt, ast.Pass):
                self.violations.append(
                    f"{self.filename}:{node.lineno}: silent 'except: pass' block detected"
                )
            elif (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is Ellipsis
            ):
                self.violations.append(
                    f"{self.filename}:{node.lineno}: silent 'except: ...' (Ellipsis) block detected"
                )
        self.generic_visit(node)


@dataclass
class ExecutionResult:
    exit_code: int
    collected: int
    passed: int
    failed: int
    skipped: int
    evidence: str
    output: str
    timed_out: bool = False
    duration_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def is_green(self, min_tests: int) -> bool:
        return (
            not self.timed_out
            and self.exit_code == 0
            and self.failed == 0
            and self.collected >= max(1, min_tests)
        )


def _is_pytest_command(argv: list[str]) -> bool:
    if "-m" in argv:
        idx = argv.index("-m")
        if idx + 1 < len(argv) and argv[idx + 1] == "pytest":
            return True
    head = Path(argv[0]).name.lower() if argv else ""
    return head.startswith("pytest") or head.startswith("py.test")


def _walk_files(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        d = Path(dirpath)
        dirnames[:] = sorted(
            n for n in dirnames if n not in _SKIP_DIRS and not (d / n / "pyvenv.cfg").exists()
        )
        for name in sorted(filenames):
            yield d / name


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TrustedVerifier:
    """Executes verification contracts and signs cryptographic provenance receipts."""

    def __init__(self, workspace_root: Path, git_mgr: GitPlumbingManager, keyring: SealKeyring) -> None:
        self.root = workspace_root.resolve()
        self.git_mgr = git_mgr
        self.keyring = keyring

    # ---------------------------------------------------------------- signatures
    def sign_receipt(self, receipt: VerificationReceipt) -> str:
        return self.keyring.sign(receipt.canonical_payload())

    def verify_receipt_signature(self, receipt: VerificationReceipt) -> bool:
        return self.keyring.verify(receipt.canonical_payload(), receipt.hmac_signature)

    def sign_attestation(self, record: AttestationRecord) -> str:
        return self.keyring.sign(record.canonical_payload())

    def verify_attestation_signature(self, record: AttestationRecord) -> bool:
        return self.keyring.verify(record.canonical_payload(), record.hmac_signature)

    # ---------------------------------------------------------------- harness surface
    def harness_surface_digests(self, base_dir: Path, command_argv: list[str]) -> dict[str, str]:
        """Digest everything besides the code under test that decides what the harness runs or reports."""
        base = base_dir.resolve()
        digests: dict[str, str] = {}
        for path in _walk_files(base):
            rel = path.relative_to(base).as_posix()
            if path.name in _HOOK_FILENAMES or (path.parent == base and path.suffix == ".pth"):
                digests[f"hook:{rel}"] = _sha(path.read_bytes())

        for ini_name in ("pytest.ini", ".pytest.ini"):
            ini = base / ini_name
            if ini.is_file():
                digests[f"config:{ini_name}"] = _sha(ini.read_bytes())
        pyproject = base / "pyproject.toml"
        if pyproject.is_file():
            try:
                section = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("pytest")
            except tomllib.TOMLDecodeError:
                section = "UNPARSEABLE"
            if section is not None:
                digests["config:pyproject.toml[tool.pytest]"] = _sha(json.dumps(section, sort_keys=True).encode())
        for ini_name, section_name in (("setup.cfg", "tool:pytest"), ("tox.ini", "pytest")):
            ini = base / ini_name
            if ini.is_file():
                parser = configparser.ConfigParser(interpolation=None)
                try:
                    parser.read(ini, encoding="utf-8")
                    if parser.has_section(section_name):
                        items = sorted(parser.items(section_name))
                        digests[f"config:{ini_name}[{section_name}]"] = _sha(json.dumps(items).encode())
                except configparser.Error:
                    digests[f"config:{ini_name}"] = _sha(ini.read_bytes())

        for arg in command_argv:
            candidate = (base / arg)
            try:
                if candidate.is_file() and base in candidate.resolve().parents:
                    rel = candidate.resolve().relative_to(base).as_posix()
                    digests[f"script:{rel}"] = _sha(candidate.read_bytes())
            except (OSError, ValueError):
                continue
        return digests

    @staticmethod
    def top_level_modules(base_dir: Path) -> list[str]:
        base = base_dir.resolve()
        names: set[str] = set()
        if base.is_dir():
            for item in base.iterdir():
                if item.is_file() and item.suffix == ".py":
                    names.add(item.stem)
                elif item.is_dir() and (item / "__init__.py").exists():
                    names.add(item.name)
        return sorted(names)

    def shadowing_modules(self, base_dir: Path, allowed: list[str]) -> list[str]:
        """New top-level modules that would shadow a stdlib or installed module for `python -m ...` runs."""
        base = base_dir.resolve()
        search_path = [
            p for p in sys.path
            if p and Path(p).resolve() not in (base, self.root)
        ]
        hits: list[str] = []
        for name in self.top_level_modules(base):
            if name in allowed:
                continue
            if name in sys.stdlib_module_names or name in sys.builtin_module_names:
                hits.append(name)
                continue
            try:
                spec = importlib.machinery.PathFinder.find_spec(name, search_path)
            except (ImportError, ValueError):
                spec = None
            if spec is not None:
                hits.append(name)
        return hits

    # ---------------------------------------------------------------- AST scan
    def scan_ast_violations(self, relative_paths: list[str], base_dir: Path) -> list[str]:
        violations: list[str] = []
        for rel in sorted(set(relative_paths)):
            target = (base_dir / rel).resolve()
            py_files: list[Path] = []
            if target.is_file() and target.suffix == ".py":
                py_files.append(target)
            elif target.is_dir():
                py_files.extend(sorted(target.rglob("*.py")))

            for py_file in py_files:
                try:
                    source = py_file.read_text(encoding="utf-8")
                    tree = ast.parse(source, filename=str(py_file))
                    visitor = _AstCheatVisitor(str(py_file.relative_to(base_dir)))
                    visitor.visit(tree)
                    violations.extend(visitor.violations)
                except SyntaxError as exc:
                    violations.append(f"{py_file.name}:{exc.lineno}: SyntaxError ({exc.msg})")
        return violations

    # ---------------------------------------------------------------- execution
    @staticmethod
    def _parse_text_counts(output: str) -> tuple[int, int, int]:
        """Fallback for non-pytest commands: counts printed by the runner. No counts means no evidence."""
        passed = failed = skipped = 0
        m_pass = re.search(r"(\d+)\s+passed", output)
        m_fail = re.search(r"(\d+)\s+(?:failed|error)", output)
        m_skip = re.search(r"(\d+)\s+(?:skipped|xfailed)", output)
        if m_pass:
            passed = int(m_pass.group(1))
        if m_fail:
            failed = int(m_fail.group(1))
        if m_skip:
            skipped = int(m_skip.group(1))
        if passed == 0 and failed == 0 and skipped == 0:
            m_unit = re.search(r"Ran\s+(\d+)\s+tests?", output)
            if m_unit:
                passed = int(m_unit.group(1))
        return passed, failed, skipped

    @staticmethod
    def _parse_junit(path: Path) -> tuple[int, int, int, int] | None:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            return None
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if not suites:
            return None
        tests = failures = errors = skipped = 0
        for s in suites:
            tests += int(s.get("tests", 0))
            failures += int(s.get("failures", 0))
            errors += int(s.get("errors", 0))
            skipped += int(s.get("skipped", 0))
        passed = tests - failures - errors - skipped
        return tests, passed, failures + errors, skipped

    @staticmethod
    def _clean_env() -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_ENV_PREFIXES_DROPPED)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def execute(self, contract: VerificationContract, execution_dir: Path) -> ExecutionResult:
        """Run the contract command; pytest commands report through a JUnit file Telos owns."""
        start_t = time.monotonic()
        argv = list(contract.command_argv)
        junit_dir: Path | None = None
        junit_path: Path | None = None
        if _is_pytest_command(argv):
            junit_dir = Path(tempfile.mkdtemp(prefix="telos-junit-"))
            junit_path = junit_dir / "report.xml"
            argv = argv + [f"--junitxml={junit_path}"]
        try:
            try:
                proc = subprocess.run(
                    argv,
                    cwd=str(execution_dir),
                    env=self._clean_env(),
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=contract.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return ExecutionResult(
                    exit_code=124, collected=0, passed=0, failed=0, skipped=0,
                    evidence="timeout", output="", timed_out=True,
                    duration_seconds=round(time.monotonic() - start_t, 4),
                )
            except OSError as exc:
                return ExecutionResult(
                    exit_code=127, collected=0, passed=0, failed=1, skipped=0,
                    evidence="exit_code", output=f"Cannot start verification command: {exc}",
                    duration_seconds=round(time.monotonic() - start_t, 4),
                )
            output = f"{proc.stdout}\n{proc.stderr}".strip()
            if junit_path is not None:
                parsed = self._parse_junit(junit_path)
                if parsed is None:
                    # The runner never wrote its report: an aborted or hijacked run is not evidence of passing.
                    return ExecutionResult(
                        exit_code=proc.returncode, collected=0, passed=0, failed=1, skipped=0,
                        evidence="junit_missing", output=output,
                        duration_seconds=round(time.monotonic() - start_t, 4),
                        notes=["pytest exited without writing its JUnit report"],
                    )
                collected, passed, failed, skipped = parsed
                evidence = "junit"
            else:
                passed, failed, skipped = self._parse_text_counts(output)
                if proc.returncode != 0 and failed == 0:
                    failed = 1
                collected = passed + failed + skipped
                evidence = "exit_code"
            return ExecutionResult(
                exit_code=proc.returncode, collected=collected, passed=passed, failed=failed,
                skipped=skipped, evidence=evidence, output=output,
                duration_seconds=round(time.monotonic() - start_t, 4),
            )
        finally:
            if junit_dir is not None:
                shutil.rmtree(junit_dir, ignore_errors=True)

    # ---------------------------------------------------------------- gate
    def run_verification(
        self,
        task_id: str,
        verifier_node_id: str,
        lease_id: str,
        fencing_token: int,
        contract: VerificationContract,
        execution_dir: Path,
    ) -> tuple[bool, FailureCategory | None, str, VerificationReceipt]:
        start_t = time.monotonic()
        current_digests = self.git_mgr.compute_paths_digests(contract.locked_paths, base_dir=execution_dir)
        source_digest = self.git_mgr.compute_directory_digest([execution_dir], exclude_telos=True)
        test_digest = hashlib.sha256(
            "|".join(f"{k}:{v}" for k, v in sorted(current_digests.items())).encode("utf-8")
        ).hexdigest()

        def _receipt(exit_code: int, collected: int, passed: int, failed: int, skipped: int,
                     violations: list[str], evidence: str) -> VerificationReceipt:
            rec = VerificationReceipt(
                task_id=task_id,
                verifier_node_id=verifier_node_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                source_tree_digest=source_digest,
                test_tree_digest=test_digest,
                command_argv=contract.command_argv,
                exit_code=exit_code,
                tests_collected=collected,
                tests_passed=passed,
                tests_failed=failed,
                tests_skipped=skipped,
                ast_violations=violations,
                evidence=evidence,
                duration_seconds=round(time.monotonic() - start_t, 4),
            )
            rec.hmac_signature = self.sign_receipt(rec)
            return rec

        tampered: list[str] = []
        for path_key, locked_hash in contract.locked_digests.items():
            actual_hash = current_digests.get(path_key, "MISSING")
            if actual_hash != locked_hash:
                tampered.append(f"{path_key} (expected {locked_hash[:10]}..., got {actual_hash[:10]}...)")

        current_harness = self.harness_surface_digests(execution_dir, contract.command_argv)
        for key in sorted(set(contract.harness_digests) | set(current_harness)):
            before = contract.harness_digests.get(key)
            after = current_harness.get(key)
            if before != after:
                change = "added" if before is None else ("removed" if after is None else "modified")
                tampered.append(f"harness surface {change}: {key}")
        for name in self.shadowing_modules(execution_dir, contract.module_names_at_lock):
            tampered.append(f"harness shadowing: new top-level module '{name}' shadows a stdlib/installed module")

        if tampered:
            receipt = _receipt(-1, 0, 0, 1, 0, [], "tamper_check")
            return (
                False,
                FailureCategory.TEST_TAMPER_ATTEMPT,
                f"TAMPERING DETECTED on locked verification surface: {'; '.join(tampered)}",
                receipt,
            )

        scan_targets = list(set(contract.locked_paths + contract.ast_scan_paths))
        ast_violations = self.scan_ast_violations(scan_targets, base_dir=execution_dir)
        if ast_violations and contract.forbid_skipped_tests:
            receipt = _receipt(-2, 0, 0, 1, len(ast_violations), ast_violations, "ast_scan")
            return (
                False,
                FailureCategory.AST_CHEAT_DETECTED,
                f"AST ANTI-CHEAT VIOLATION: {'; '.join(ast_violations)}",
                receipt,
            )

        result = self.execute(contract, execution_dir)
        receipt = _receipt(
            result.exit_code, result.collected, result.passed, result.failed, result.skipped,
            ast_violations, result.evidence,
        )

        if result.timed_out:
            return (
                False,
                FailureCategory.INFRA_OR_FLAKY_ENV,
                f"Verification command timed out after {contract.timeout_seconds}s",
                receipt,
            )
        if result.evidence == "junit_missing":
            return (
                False,
                FailureCategory.AGENT_LOGIC_ERROR,
                "Verification produced no structured test report (run aborted or hijacked before pytest finished).",
                receipt,
            )
        if result.exit_code != 0 or result.failed > 0:
            snippet = result.output[-400:]
            return (
                False,
                FailureCategory.AGENT_LOGIC_ERROR,
                f"Verification exited {result.exit_code} with {result.failed} failing: {snippet}",
                receipt,
            )
        if result.collected < max(1, contract.min_tests_collected):
            return (
                False,
                FailureCategory.AGENT_LOGIC_ERROR,
                f"Collected {result.collected} tests, fewer than required min_tests_collected="
                f"{max(1, contract.min_tests_collected)}",
                receipt,
            )
        if contract.forbid_skipped_tests and result.skipped > 0:
            return (
                False,
                FailureCategory.AST_CHEAT_DETECTED,
                f"Reward hacking blocked: {result.skipped} skipped tests detected in runner report",
                receipt,
            )
        return True, None, "All verification gates and provenance checks passed.", receipt

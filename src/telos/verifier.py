"""Trusted Anti-Reward-Hacking Verifier: Whole-Tree SHA Lock, Hardened AST Anti-Cheat, and HMAC Receipts."""

from __future__ import annotations

import ast
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import subprocess
import time

from telos.git_worktree import GitPlumbingManager
from telos.models import FailureCategory, VerificationContract, VerificationReceipt


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


class TrustedVerifier:
    """Executes verification contracts and signs cryptographic provenance receipts."""

    def __init__(self, workspace_root: Path, git_mgr: GitPlumbingManager) -> None:
        self.root = workspace_root.resolve()
        self.git_mgr = git_mgr
        self.key_path = self.root / ".telos" / ".cache" / "verifier.hmac.key"

    def _get_hmac_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            self.key_path.write_bytes(secrets.token_bytes(32))
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        return self.key_path.read_bytes()

    def sign_receipt(self, receipt: VerificationReceipt) -> str:
        payload = receipt.canonical_payload().encode("utf-8")
        return hmac.new(self._get_hmac_key(), payload, hashlib.sha256).hexdigest()

    def verify_receipt_signature(self, receipt: VerificationReceipt) -> bool:
        expected = self.sign_receipt(receipt)
        return hmac.compare_digest(expected, receipt.hmac_signature)

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

    @staticmethod
    def _parse_test_counts(output: str, exit_code: int) -> tuple[int, int, int, int]:
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
                total = int(m_unit.group(1))
                if exit_code == 0:
                    passed = total
                else:
                    failed = max(1, total)
            elif exit_code == 0:
                passed = 1
            else:
                failed = 1

        collected = passed + failed + skipped
        return collected, passed, failed, skipped

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

        tampered: list[str] = []
        for path_key, locked_hash in contract.locked_digests.items():
            actual_hash = current_digests.get(path_key, "MISSING")
            if actual_hash != locked_hash:
                tampered.append(f"{path_key} (expected {locked_hash[:10]}..., got {actual_hash[:10]}...)")

        scan_targets = list(set(contract.locked_paths + contract.ast_scan_paths))
        ast_violations = self.scan_ast_violations(scan_targets, base_dir=execution_dir)

        source_digest = self.git_mgr.compute_directory_digest([execution_dir], exclude_telos=True)
        test_digest = hashlib.sha256(
            "|".join(f"{k}:{v}" for k, v in sorted(current_digests.items())).encode("utf-8")
        ).hexdigest()

        if tampered:
            receipt = VerificationReceipt(
                task_id=task_id,
                verifier_node_id=verifier_node_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                source_tree_digest=source_digest,
                test_tree_digest=test_digest,
                command_argv=contract.command_argv,
                exit_code=-1,
                tests_collected=0,
                tests_passed=0,
                tests_failed=1,
                tests_skipped=0,
                ast_violations=ast_violations,
                duration_seconds=round(time.monotonic() - start_t, 4),
            )
            receipt.hmac_signature = self.sign_receipt(receipt)
            return (
                False,
                FailureCategory.TEST_TAMPER_ATTEMPT,
                f"TAMPERING DETECTED on locked verification files: {', '.join(tampered)}",
                receipt,
            )

        if ast_violations and contract.forbid_skipped_tests:
            receipt = VerificationReceipt(
                task_id=task_id,
                verifier_node_id=verifier_node_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                source_tree_digest=source_digest,
                test_tree_digest=test_digest,
                command_argv=contract.command_argv,
                exit_code=-2,
                tests_collected=0,
                tests_passed=0,
                tests_failed=1,
                tests_skipped=len(ast_violations),
                ast_violations=ast_violations,
                duration_seconds=round(time.monotonic() - start_t, 4),
            )
            receipt.hmac_signature = self.sign_receipt(receipt)
            return (
                False,
                FailureCategory.AST_CHEAT_DETECTED,
                f"AST ANTI-CHEAT VIOLATION: {'; '.join(ast_violations)}",
                receipt,
            )

        clean_env = {
            k: v for k, v in os.environ.items() if not k.startswith("TELOS_INTERNAL_")
        }
        clean_env["PYTHONDONTWRITEBYTECODE"] = "1"

        try:
            proc = subprocess.run(
                contract.command_argv,
                cwd=str(execution_dir),
                env=clean_env,
                shell=False,
                capture_output=True,
                text=True,
                timeout=contract.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            receipt = VerificationReceipt(
                task_id=task_id,
                verifier_node_id=verifier_node_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                source_tree_digest=source_digest,
                test_tree_digest=test_digest,
                command_argv=contract.command_argv,
                exit_code=124,
                tests_collected=0,
                tests_passed=0,
                tests_failed=0,
                tests_skipped=0,
                ast_violations=[],
                duration_seconds=round(time.monotonic() - start_t, 4),
            )
            receipt.hmac_signature = self.sign_receipt(receipt)
            return (
                False,
                FailureCategory.INFRA_OR_FLAKY_ENV,
                f"Verification command timed out after {contract.timeout_seconds}s",
                receipt,
            )

        combined_out = f"{proc.stdout}\n{proc.stderr}".strip()
        collected, passed, failed, skipped = self._parse_test_counts(combined_out, proc.returncode)

        receipt = VerificationReceipt(
            task_id=task_id,
            verifier_node_id=verifier_node_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            source_tree_digest=source_digest,
            test_tree_digest=test_digest,
            command_argv=contract.command_argv,
            exit_code=proc.returncode,
            tests_collected=collected,
            tests_passed=passed,
            tests_failed=failed,
            tests_skipped=skipped,
            ast_violations=ast_violations,
            duration_seconds=round(time.monotonic() - start_t, 4),
        )
        receipt.hmac_signature = self.sign_receipt(receipt)

        if proc.returncode != 0:
            snippet = combined_out[-400:] if len(combined_out) > 400 else combined_out
            return (
                False,
                FailureCategory.AGENT_LOGIC_ERROR,
                f"Verification exited {proc.returncode}: {snippet}",
                receipt,
            )

        if collected < contract.min_tests_collected:
            return (
                False,
                FailureCategory.AGENT_LOGIC_ERROR,
                f"Collected {collected} tests, fewer than required min_tests_collected={contract.min_tests_collected}",
                receipt,
            )

        if contract.forbid_skipped_tests and skipped > 0:
            return (
                False,
                FailureCategory.AST_CHEAT_DETECTED,
                f"Reward hacking blocked: {skipped} skipped tests detected in runner output",
                receipt,
            )

        return True, None, "All verification gates and provenance checks passed.", receipt


"""Worktree-safe Git plumbing manager for isolated leases and non-destructive Mikado snapshots."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

class GitPlumbingManager:
    """Manages isolated worktrees and private refs/telos/attempts/* snapshots without git stash."""

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.resolve()

    def _run_git(
        self, args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def has_git_commit(self) -> bool:
        res = self._run_git(["rev-parse", "--verify", "HEAD"])
        return res.returncode == 0

    def current_head_sha(self) -> str:
        res = self._run_git(["rev-parse", "HEAD"])
        if res.returncode == 0:
            return res.stdout.strip()
        return self.compute_directory_digest([self.root], exclude_telos=True)

    def create_isolated_worktree(self, lease_id: str) -> tuple[Path, str]:
        """Create an isolated worktree under .telos/.cache/worktrees/<lease_id> if inside a git repo."""
        head_sha = self.current_head_sha()
        wt_path = self.root / ".telos" / ".cache" / "worktrees" / lease_id
        if self.has_git_commit():
            wt_path.parent.mkdir(parents=True, exist_ok=True)
            res = self._run_git(["worktree", "add", "--detach", str(wt_path), head_sha])
            if res.returncode == 0:
                return wt_path, head_sha
        # Fallback if workspace is not a committed git repo yet: return workspace root
        return self.root, head_sha

    def snapshot_and_reset_worktree(
        self, worktree_path: Path, task_id: str, attempt_id: str, baseline_sha: str
    ) -> str:
        """
        Capture dirty state using temporary GIT_INDEX_FILE + git write-tree + git commit-tree
        into refs/telos/attempts/<task_id>/<attempt_id>, then reset only if isolated worktree.
        Never touches shared refs/stash.
        """
        if self.has_git_commit() and worktree_path.exists():
            tmp_index = self.root / ".telos" / ".cache" / f"tmp_idx_{attempt_id}"
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(tmp_index)
            try:
                self._run_git(["read-tree", "HEAD"], cwd=worktree_path, env=env)
                self._run_git(["add", "-A"], cwd=worktree_path, env=env)
                tree_res = self._run_git(["write-tree"], cwd=worktree_path, env=env)
                if tree_res.returncode == 0:
                    tree_sha = tree_res.stdout.strip()
                    commit_res = self._run_git(
                        [
                            "commit-tree",
                            tree_sha,
                            "-p",
                            baseline_sha,
                            "-m",
                            f"Telos Mikado Snapshot task={task_id} attempt={attempt_id}",
                        ],
                        cwd=worktree_path,
                        env=env,
                    )
                    if commit_res.returncode == 0:
                        commit_sha = commit_res.stdout.strip()
                        ref_name = f"refs/telos/attempts/{task_id}/{attempt_id}"
                        self._run_git(["update-ref", ref_name, commit_sha], cwd=self.root)
                        if worktree_path.resolve() != self.root.resolve():
                            self._run_git(["reset", "--hard", baseline_sha], cwd=worktree_path)
                            self._run_git(["clean", "-fd"], cwd=worktree_path)
                        return ref_name
            finally:
                if tmp_index.exists():
                    tmp_index.unlink()

        # Fallback snapshot archive when running outside an initialized git repository
        snap_dir = self.root / ".telos" / ".cache" / "snapshots" / task_id / attempt_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        digest = self.compute_directory_digest([worktree_path], exclude_telos=True)
        (snap_dir / "digest.txt").write_text(digest, encoding="utf-8")
        return f"snapshot://local/{task_id}/{attempt_id}:{digest[:12]}"

    def remove_isolated_worktree(self, worktree_path: Path) -> None:
        if worktree_path.resolve() == self.root.resolve():
            return
        if worktree_path.exists():
            self._run_git(["worktree", "remove", "--force", str(worktree_path)])
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)

    def compute_paths_digests(self, relative_paths: list[str], base_dir: Path | None = None) -> dict[str, str]:
        """Compute deterministic SHA-256 digests for files or directories relative to base_dir."""
        root = (base_dir or self.root).resolve()
        digests: dict[str, str] = {}
        for rel in sorted(set(relative_paths)):
            target = (root / rel).resolve()
            if not target.exists():
                digests[rel] = "MISSING"
            elif target.is_file():
                digests[rel] = hashlib.sha256(target.read_bytes()).hexdigest()
            elif target.is_dir():
                digests[rel] = self.compute_directory_digest([target], exclude_telos=True)
        return digests

    def compute_directory_digest(self, paths: list[Path], exclude_telos: bool = True) -> str:
        hasher = hashlib.sha256()
        for base in sorted(paths):
            if not base.exists():
                continue
            if base.is_file():
                hasher.update(base.name.encode("utf-8"))
                hasher.update(base.read_bytes())
                continue
            for file_path in sorted(base.rglob("*")):
                if not file_path.is_file():
                    continue
                rel_parts = file_path.relative_to(base).parts
                if exclude_telos and (".telos" in rel_parts or ".git" in rel_parts or "__pycache__" in rel_parts):
                    continue
                hasher.update("/".join(rel_parts).encode("utf-8"))
                hasher.update(file_path.read_bytes())
        return hasher.hexdigest()

"""Transactional Application Service mediating Git-tracked TOML/JSONL files and SQLite WAL Index."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any

from telos.dag_pert import CpmNodeSchedule, DagEngine, MonteCarloScheduleResult
from telos.git_worktree import GitPlumbingManager
from telos.models import (
    AttemptRecord,
    EdgeRelation,
    FailureCategory,
    GraphEdge,
    KanbanState,
    LeaseRecord,
    NodeKind,
    PertTriplet,
    ResourceEstimates,
    ScopeConfig,
    TaskNode,
    VerificationContract,
    VerificationReceipt,
    ZwickyBox,
    utc_now,
)
from telos.toml_codec import read_toml, write_canonical_toml
from telos.verifier import TrustedVerifier
from telos.zwicky import MorphologicalCandidate, ZwickySolver

class StaleFencingTokenError(PermissionError):
    """Raised when an agent presents an expired or superseded lease fencing token."""

class VerificationGateError(PermissionError):
    """Raised when attempting an illegal state transition or self-approval."""

class TelosService:
    """Single authoritative application service for CLI, MCP, and TUI consumers."""

    def __init__(self, workspace_root: Path | str = ".") -> None:
        self.root = Path(workspace_root).resolve()
        self.telos_dir = self.root / ".telos"
        self.nodes_dir = self.telos_dir / "nodes"
        self.edges_file = self.telos_dir / "edges" / "links.toml"
        self.zwicky_dir = self.telos_dir / "zwicky"
        self.ledger_dir = self.telos_dir / "ledger"
        self.attempts_file = self.ledger_dir / "attempts.jsonl"
        self.receipts_file = self.ledger_dir / "receipts.jsonl"
        self.cache_dir = self.telos_dir / ".cache"
        self.sqlite_path = self.cache_dir / "index.sqlite"
        self.git_mgr = GitPlumbingManager(self.root)
        self.verifier = TrustedVerifier(self.root, self.git_mgr)

    # -------------------------------------------------------------------------
    # Workspace Initialization & SQLite WAL Operational Cache
    # -------------------------------------------------------------------------
    def init_workspace(
        self,
        project_name: str,
        super_project_path: str | None = None,
        sub_project_paths: list[str] | None = None,
    ) -> ScopeConfig:
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        self.edges_file.parent.mkdir(parents=True, exist_ok=True)
        self.zwicky_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        gitignore = self.telos_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(".cache/\n*.tmp\n", encoding="utf-8")

        cfg_path = self.telos_dir / "config.toml"
        if cfg_path.exists():
            cfg = ScopeConfig.model_validate(read_toml(cfg_path))
        else:
            cfg = ScopeConfig(
                project_name=project_name,
                super_project_path=super_project_path,
                sub_project_paths=sub_project_paths or [],
            )
            write_canonical_toml(cfg_path, cfg.model_dump(mode="json"))

        if not self.edges_file.exists():
            write_canonical_toml(self.edges_file, {"edges": []})
        for ledger in (self.attempts_file, self.receipts_file):
            if not ledger.exists():
                ledger.write_text("", encoding="utf-8")

        self._init_sqlite_wal()
        self.rebuild_sqlite_index()
        return cfg

    def _connect_sqlite(self) -> sqlite3.Connection:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.sqlite_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_sqlite_wal(self) -> None:
        with self._connect_sqlite() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes_index (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    category TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    strike_count INTEGER NOT NULL,
                    expected_minutes REAL NOT NULL,
                    expected_tokens REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edges_index (
                    source_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    scope_ref TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (source_id, relation, target_id, scope_ref)
                );
                CREATE TABLE IF NOT EXISTS active_leases (
                    lease_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    worktree_path TEXT NOT NULL,
                    baseline_commit_sha TEXT NOT NULL,
                    token_budget_limit INTEGER NOT NULL,
                    tokens_consumed INTEGER NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_heartbeat_at TEXT NOT NULL,
                    active INTEGER NOT NULL
                );
                """
            )

    def rebuild_sqlite_index(self) -> None:
        """Synchronize Git-tracked TOML files into the SQLite WAL index and validate DAG acyclicity."""
        self._init_sqlite_wal()
        nodes = self.load_all_nodes()
        edges = self.load_all_edges()
        # Post-merge semantic validation: ensures Git merges didn't introduce a dependency cycle
        dag = DagEngine(nodes, edges)
        dag.validate_acyclic()

        with self._connect_sqlite() as conn:
            conn.execute("DELETE FROM nodes_index;")
            conn.execute("DELETE FROM edges_index;")
            for node in nodes.values():
                conn.execute(
                    """
                    INSERT INTO nodes_index
                    (id, title, kind, state, category, group_name, fencing_token, strike_count, expected_minutes, expected_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node.id,
                        node.title,
                        node.kind.value,
                        node.state.value,
                        node.category,
                        node.group_name,
                        node.fencing_token,
                        node.strike_count,
                        node.estimates.duration_minutes.mean,
                        node.estimates.token_cost.mean,
                    ),
                )
            for edge in edges:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO edges_index (source_id, relation, target_id, scope_ref)
                    VALUES (?, ?, ?, ?)
                    """,
                    (edge.source_id, edge.relation.value, edge.target_id, edge.scope_ref or ""),
                )

    # -------------------------------------------------------------------------
    # Authoritative TOML & JSONL Persistence Operations
    # -------------------------------------------------------------------------
    def _node_path(self, node_id: str) -> Path:
        return self.nodes_dir / f"{node_id}.toml"

    def save_node(self, node: TaskNode) -> TaskNode:
        node.updated_at = utc_now()
        node.labels = sorted(set(node.labels))
        write_canonical_toml(self._node_path(node.id), node.model_dump(mode="json"))
        self.rebuild_sqlite_index()
        return node

    def get_node(self, node_id: str) -> TaskNode:
        path = self._node_path(node_id)
        if not path.exists():
            # Allow prefix matching for CLI convenience
            matches = list(self.nodes_dir.glob(f"{node_id}*.toml"))
            if len(matches) == 1:
                path = matches[0]
            else:
                raise KeyError(f"TaskNode '{node_id}' not found in {self.nodes_dir}")
        return TaskNode.model_validate(read_toml(path))

    def load_all_nodes(self) -> dict[str, TaskNode]:
        if not self.nodes_dir.exists():
            return {}
        result: dict[str, TaskNode] = {}
        for p in sorted(self.nodes_dir.glob("*.toml")):
            node = TaskNode.model_validate(read_toml(p))
            result[node.id] = node
        return result

    def load_all_edges(self) -> list[GraphEdge]:
        if not self.edges_file.exists():
            return []
        raw = read_toml(self.edges_file)
        edges = [GraphEdge.model_validate(item) for item in raw.get("edges", [])]
        edges.sort(key=lambda e: e.sort_key())
        return edges

    def _save_edges(self, edges: list[GraphEdge]) -> None:
        unique = {e.sort_key(): e for e in edges}
        sorted_edges = [unique[k].model_dump(mode="json") for k in sorted(unique.keys())]
        write_canonical_toml(self.edges_file, {"edges": sorted_edges})

    def add_edge(
        self,
        source_id: str,
        relation: EdgeRelation,
        target_id: str,
        scope_ref: str | None = None,
    ) -> GraphEdge:
        nodes = self.load_all_nodes()
        if source_id not in nodes:
            source_id = self.get_node(source_id).id
        if not scope_ref and target_id not in nodes:
            target_id = self.get_node(target_id).id

        new_edge = GraphEdge(
            source_id=source_id, relation=relation, target_id=target_id, scope_ref=scope_ref
        )
        edges = self.load_all_edges()
        candidate_edges = edges + [new_edge]

        if relation == EdgeRelation.DEPENDS_ON and not scope_ref:
            dag = DagEngine(nodes, candidate_edges)
            dag.validate_acyclic()

        self._save_edges(candidate_edges)
        self.rebuild_sqlite_index()
        return new_edge

    def create_node(
        self,
        title: str,
        concise_spec: str = "",
        kind: NodeKind = NodeKind.L3_IMPLEMENTATION,
        state: KanbanState = KanbanState.BACKLOG,
        category: str = "general",
        group_name: str = "default",
        labels: list[str] | None = None,
        optimistic_min: float = 15.0,
        most_likely_min: float = 30.0,
        pessimistic_min: float = 60.0,
        optimistic_tokens: float = 5000.0,
        most_likely_tokens: float = 15000.0,
        pessimistic_tokens: float = 45000.0,
        parent_id: str | None = None,
    ) -> TaskNode:
        estimates = ResourceEstimates(
            duration_minutes=PertTriplet(
                optimistic=optimistic_min,
                most_likely=most_likely_min,
                pessimistic=pessimistic_min,
            ),
            token_cost=PertTriplet(
                optimistic=optimistic_tokens,
                most_likely=most_likely_tokens,
                pessimistic=pessimistic_tokens,
            ),
        )
        node = TaskNode(
            title=title,
            concise_spec=concise_spec,
            kind=kind,
            state=state,
            category=category,
            group_name=group_name,
            labels=sorted(set(labels or [])),
            estimates=estimates,
        )
        self.save_node(node)
        if parent_id:
            parent = self.get_node(parent_id)
            self.add_edge(parent.id, EdgeRelation.CONTAINS, node.id)
        return node

    def _append_jsonl(self, path: Path, record: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n")

    def get_attempts_for_task(self, task_id: str) -> list[AttemptRecord]:
        if not self.attempts_file.exists():
            return []
        records: list[AttemptRecord] = []
        for line in self.attempts_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = AttemptRecord.model_validate(json.loads(line))
            if rec.task_id == task_id:
                records.append(rec)
        return records

    def get_receipts_for_task(self, task_id: str) -> list[VerificationReceipt]:
        if not self.receipts_file.exists():
            return []
        records: list[VerificationReceipt] = []
        for line in self.receipts_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = VerificationReceipt.model_validate(json.loads(line))
            if rec.task_id == task_id:
                records.append(rec)
        return records

    # -------------------------------------------------------------------------
    # V-Model Role-Separated Contract Approval
    # -------------------------------------------------------------------------
    def approve_verification_contract(
        self,
        left_node_id: str,
        verifier_title: str,
        command_argv: list[str],
        locked_paths: list[str],
        ast_scan_paths: list[str] | None = None,
        min_tests_collected: int = 1,
        approved_by: str = "human-architect",
        active_agent_id: str | None = None,
    ) -> tuple[TaskNode, TaskNode]:
        """
        Create/bind a Right-Wing R* verification node to a Left-Wing L* node and lock tree SHA-256 digests.
        Enforces Role Separation: an agent currently leasing the L* task cannot approve its own contract.
        """
        left_node = self.get_node(left_node_id)
        if active_agent_id and left_node.active_lease_id:
            lease = self.get_lease(left_node.active_lease_id)
            if lease and lease.agent_id == active_agent_id:
                raise VerificationGateError(
                    f"Role separation violation: implementer agent '{active_agent_id}' cannot approve "
                    f"verification contracts for its own leased task {left_node.id}."
                )

        right_kind_map = {
            NodeKind.L0_REQUIREMENT: NodeKind.R0_ACCEPTANCE,
            NodeKind.L1_ARCHITECTURE: NodeKind.R1_SYSTEM_VERIFY,
            NodeKind.L2_MODULE_SPEC: NodeKind.R2_INTEGRATION_VERIFY,
            NodeKind.L3_IMPLEMENTATION: NodeKind.R3_UNIT_VERIFY,
        }
        r_kind = right_kind_map.get(left_node.kind, NodeKind.R3_UNIT_VERIFY)
        locked_digests = self.git_mgr.compute_paths_digests(locked_paths, base_dir=self.root)
        missing = [p for p, d in locked_digests.items() if d == "MISSING"]
        if missing:
            raise FileNotFoundError(
                f"Cannot lock verification contract; test/config paths missing: {', '.join(missing)}"
            )

        contract = VerificationContract(
            command_argv=command_argv,
            locked_paths=sorted(set(locked_paths)),
            locked_digests=locked_digests,
            min_tests_collected=min_tests_collected,
            ast_scan_paths=sorted(set(ast_scan_paths or locked_paths)),
            approved_by=approved_by,
        )

        right_node = self.create_node(
            title=verifier_title,
            concise_spec=f"Verification gate for {left_node.id}: {' '.join(command_argv)}",
            kind=r_kind,
            state=KanbanState.READY,
            category=left_node.category,
            group_name=left_node.group_name,
        )
        right_node.contract = contract
        self.save_node(right_node)

        self.add_edge(right_node.id, EdgeRelation.VERIFIES, left_node.id)
        if left_node.state == KanbanState.BACKLOG:
            left_node.state = KanbanState.READY
            self.save_node(left_node)

        return left_node, right_node

    # -------------------------------------------------------------------------
    # Lease Management, Fencing Tokens & Context Capsules
    # -------------------------------------------------------------------------
    def expire_stale_leases(self) -> list[str]:
        now = utc_now()
        expired_tasks: list[str] = []
        with self._connect_sqlite() as conn:
            rows = conn.execute(
                "SELECT lease_id, task_id FROM active_leases WHERE active = 1 AND expires_at < ?",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE active_leases SET active = 0 WHERE lease_id = ?", (row["lease_id"],)
                )
                node = self.get_node(row["task_id"])
                if node.state == KanbanState.LEASED and node.active_lease_id == row["lease_id"]:
                    node.state = KanbanState.READY
                    node.active_lease_id = None
                    self.save_node(node)
                    expired_tasks.append(node.id)
        return expired_tasks

    def get_lease(self, lease_id: str) -> LeaseRecord | None:
        with self._connect_sqlite() as conn:
            row = conn.execute(
                "SELECT * FROM active_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            data["active"] = bool(data["active"])
            return LeaseRecord.model_validate(data)

    def _validate_fencing_token(self, node: TaskNode, lease_id: str, fencing_token: int) -> LeaseRecord:
        self.expire_stale_leases()
        lease = self.get_lease(lease_id)
        if (
            not lease
            or not lease.active
            or node.active_lease_id != lease_id
            or node.fencing_token != fencing_token
            or lease.fencing_token != fencing_token
        ):
            raise StaleFencingTokenError(
                f"Lease fencing check failed for task {node.id}: presented token={fencing_token}, "
                f"current authoritative token={node.fencing_token} (active_lease={node.active_lease_id})."
            )
        return lease

    def checkout_leaf(
        self, task_id: str, agent_id: str, ttl_minutes: int = 30, create_worktree: bool = False
    ) -> dict[str, Any]:
        """
        Acquire an exclusive lease with a monotonically incremented fencing_token.
        Returns a token-frugal L1 Context Capsule including past failed attempts so the agent avoids amnesia.
        """
        self.expire_stale_leases()
        nodes = self.load_all_nodes()
        edges = self.load_all_edges()
        node = self.get_node(task_id)
        nodes[node.id] = node

        if node.state == KanbanState.QUARANTINED:
            raise VerificationGateError(
                f"Task {node.id} is QUARANTINED after {node.strike_count} strikes. "
                f"Decompose via Mikado or reset strikes after human review."
            )

        dag = DagEngine(nodes, edges)
        unfinished_prereqs = [
            pid for pid in dag.prereqs.get(node.id, set()) if nodes[pid].state != KanbanState.DONE
        ]
        if unfinished_prereqs:
            raise VerificationGateError(
                f"Cannot lease non-leaf task {node.id}; unresolved prerequisites: {', '.join(unfinished_prereqs)}"
            )

        node.fencing_token += 1
        lease_record_id = LeaseRecord(
            task_id=node.id,
            agent_id=agent_id,
            fencing_token=node.fencing_token,
            worktree_path=str(self.root),
            baseline_commit_sha=self.git_mgr.current_head_sha(),
            token_budget_limit=int(node.estimates.token_cost.pessimistic),
            expires_at=utc_now() + timedelta(minutes=ttl_minutes),
        )
        if create_worktree:
            wt_path, base_sha = self.git_mgr.create_isolated_worktree(lease_record_id.lease_id)
            lease_record_id.worktree_path = str(wt_path)
            lease_record_id.baseline_commit_sha = base_sha

        node.state = KanbanState.LEASED
        node.active_lease_id = lease_record_id.lease_id
        self.save_node(node)

        with self._connect_sqlite() as conn:
            conn.execute(
                "UPDATE active_leases SET active = 0 WHERE task_id = ? AND lease_id != ?",
                (node.id, lease_record_id.lease_id),
            )
            conn.execute(
                """
                INSERT INTO active_leases
                (lease_id, task_id, agent_id, fencing_token, worktree_path, baseline_commit_sha,
                 token_budget_limit, tokens_consumed, acquired_at, expires_at, last_heartbeat_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    lease_record_id.lease_id,
                    lease_record_id.task_id,
                    lease_record_id.agent_id,
                    lease_record_id.fencing_token,
                    lease_record_id.worktree_path,
                    lease_record_id.baseline_commit_sha,
                    lease_record_id.token_budget_limit,
                    lease_record_id.tokens_consumed,
                    lease_record_id.acquired_at.isoformat(),
                    lease_record_id.expires_at.isoformat(),
                    lease_record_id.last_heartbeat_at.isoformat(),
                ),
            )

        verifiers = [
            nodes[e.source_id]
            for e in edges
            if e.relation == EdgeRelation.VERIFIES and e.target_id == node.id and e.source_id in nodes
        ]
        past_attempts = self.get_attempts_for_task(node.id)[-3:]

        return {
            "task_id": node.id,
            "title": node.title,
            "spec": node.concise_spec,
            "lease_id": lease_record_id.lease_id,
            "fencing_token": lease_record_id.fencing_token,
            "worktree_path": lease_record_id.worktree_path,
            "strikes": f"{node.strike_count}/{node.max_strikes}",
            "token_budget": lease_record_id.token_budget_limit,
            "verification_gates": [
                {
                    "verifier_id": v.id,
                    "cmd": " ".join(v.contract.command_argv) if v.contract else "",
                    "locked_paths": v.contract.locked_paths if v.contract else [],
                }
                for v in verifiers
            ],
            "episodic_memory_do_not_repeat": [
                {
                    "attempt": a.attempt_id[-6:],
                    "category": a.category.value,
                    "rationale": a.rationale,
                    "snapshot_ref": a.git_snapshot_ref,
                }
                for a in past_attempts
            ],
        }

    # -------------------------------------------------------------------------
    # Mikado Method Inverse Discovery & Automated Rollback
    # -------------------------------------------------------------------------
    def mikado_discover_prerequisite(
        self,
        blocked_task_id: str,
        lease_id: str,
        fencing_token: int,
        prerequisite_title: str,
        prerequisite_spec: str,
        why_blocked_rationale: str,
        auto_checkout_prereq: bool = True,
    ) -> dict[str, Any]:
        """
        Record a newly discovered prerequisite via the Mikado Method:
        1. Validate active lease & fencing_token.
        2. Snapshot dirty worktree into refs/telos/attempts/<task>/<attempt> (no git stash) and reset worktree.
        3. Create prerequisite node P1 and link blocked_task DEPENDS_ON P1.
        4. Release blocked_task lease (moves to MIKADO_DISCOVERY without incrementing strike_count).
        5. Optionally auto-checkout the new leaf P1.
        """
        blocked_node = self.get_node(blocked_task_id)
        lease = self._validate_fencing_token(blocked_node, lease_id, fencing_token)

        prereq_node = self.create_node(
            title=prerequisite_title,
            concise_spec=prerequisite_spec,
            kind=blocked_node.kind,
            state=KanbanState.READY,
            category=blocked_node.category,
            group_name=blocked_node.group_name,
            labels=blocked_node.labels,
        )

        attempt = AttemptRecord(
            task_id=blocked_node.id,
            lease_id=lease.lease_id,
            agent_id=lease.agent_id,
            fencing_token=fencing_token,
            category=FailureCategory.MIKADO_PREREQUISITE,
            rationale=why_blocked_rationale[:500],
            spawned_prerequisite_id=prereq_node.id,
        )
        snap_ref = self.git_mgr.snapshot_and_reset_worktree(
            Path(lease.worktree_path),
            blocked_node.id,
            attempt.attempt_id,
            lease.baseline_commit_sha,
        )
        attempt.git_snapshot_ref = snap_ref
        self._append_jsonl(self.attempts_file, attempt)

        self.add_edge(blocked_node.id, EdgeRelation.DEPENDS_ON, prereq_node.id)

        blocked_node.state = KanbanState.MIKADO_DISCOVERY
        blocked_node.active_lease_id = None
        self.save_node(blocked_node)

        with self._connect_sqlite() as conn:
            conn.execute("UPDATE active_leases SET active = 0 WHERE lease_id = ?", (lease.lease_id,))

        result: dict[str, Any] = {
            "blocked_task_id": blocked_node.id,
            "blocked_task_new_state": blocked_node.state.value,
            "snapshot_ref": snap_ref,
            "spawned_prerequisite_id": prereq_node.id,
        }
        if auto_checkout_prereq:
            result["new_leaf_lease"] = self.checkout_leaf(prereq_node.id, lease.agent_id)
        return result

    # -------------------------------------------------------------------------
    # Zero-Trust Verification Gate & 3-Strike Circuit Breaker
    # -------------------------------------------------------------------------
    def submit_for_verification(
        self, task_id: str, lease_id: str, fencing_token: int
    ) -> dict[str, Any]:
        """
        Run all linked V-Model Right-Wing (R*) verification contracts for a leased Left-Wing (L*) task.
        Agents cannot mark tasks DONE directly; only a signed VerificationReceipt transitions state to DONE.
        """
        node = self.get_node(task_id)
        lease = self._validate_fencing_token(node, lease_id, fencing_token)

        nodes = self.load_all_nodes()
        edges = self.load_all_edges()
        verifiers = [
            nodes[e.source_id]
            for e in edges
            if e.relation == EdgeRelation.VERIFIES and e.target_id == node.id and e.source_id in nodes
        ]
        if not verifiers:
            raise VerificationGateError(
                f"Task {node.id} has no linked V-Model verification node (R3/R2/R1/R0). "
                f"Approve a contract before submitting for verification."
            )

        node.state = KanbanState.VERIFYING
        self.save_node(node)

        exec_dir = Path(lease.worktree_path)
        all_receipts: list[str] = []

        for v_node in verifiers:
            if not v_node.contract:
                raise VerificationGateError(f"Verifier node {v_node.id} has no VerificationContract.")

            passed, fail_cat, message, receipt = self.verifier.run_verification(
                task_id=node.id,
                verifier_node_id=v_node.id,
                lease_id=lease.lease_id,
                fencing_token=fencing_token,
                contract=v_node.contract,
                execution_dir=exec_dir,
            )
            self._append_jsonl(self.receipts_file, receipt)
            all_receipts.append(receipt.receipt_id)

            if not passed:
                assert fail_cat is not None
                attempt = AttemptRecord(
                    task_id=node.id,
                    lease_id=lease.lease_id,
                    agent_id=lease.agent_id,
                    fencing_token=fencing_token,
                    category=fail_cat,
                    rationale=message[:500],
                )
                if fail_cat in (FailureCategory.TEST_TAMPER_ATTEMPT, FailureCategory.AST_CHEAT_DETECTED):
                    node.strike_count = node.max_strikes
                elif fail_cat == FailureCategory.AGENT_LOGIC_ERROR:
                    node.strike_count += 1
                # Note: INFRA_OR_FLAKY_ENV does NOT increment strike_count

                if node.strike_count >= node.max_strikes:
                    snap_ref = self.git_mgr.snapshot_and_reset_worktree(
                        exec_dir, node.id, attempt.attempt_id, lease.baseline_commit_sha
                    )
                    attempt.git_snapshot_ref = snap_ref
                    node.state = KanbanState.QUARANTINED
                    node.active_lease_id = None
                    with self._connect_sqlite() as conn:
                        conn.execute(
                            "UPDATE active_leases SET active = 0 WHERE lease_id = ?", (lease.lease_id,)
                        )
                else:
                    node.state = KanbanState.LEASED

                self._append_jsonl(self.attempts_file, attempt)
                self.save_node(node)
                return {
                    "passed": False,
                    "task_id": node.id,
                    "state": node.state.value,
                    "failure_category": fail_cat.value,
                    "strikes": f"{node.strike_count}/{node.max_strikes}",
                    "diagnostic": message,
                    "receipt_id": receipt.receipt_id,
                }

            v_node.state = KanbanState.DONE
            v_node.last_verified_receipt_id = receipt.receipt_id
            self.save_node(v_node)

        node.state = KanbanState.DONE
        node.active_lease_id = None
        node.last_verified_receipt_id = all_receipts[-1]
        self.save_node(node)

        with self._connect_sqlite() as conn:
            conn.execute("UPDATE active_leases SET active = 0 WHERE lease_id = ?", (lease.lease_id,))

        unblocked = self._auto_ready_dependents()
        return {
            "passed": True,
            "task_id": node.id,
            "state": node.state.value,
            "receipt_ids": all_receipts,
            "newly_ready_tasks": unblocked,
        }

    def _auto_ready_dependents(self) -> list[str]:
        """Transition MIKADO_DISCOVERY tasks back to READY once all their prerequisites reach DONE."""
        nodes = self.load_all_nodes()
        edges = self.load_all_edges()
        dag = DagEngine(nodes, edges)
        newly_ready: list[str] = []
        for nid, node in nodes.items():
            if node.state in (KanbanState.MIKADO_DISCOVERY, KanbanState.BLOCKED):
                prereqs = dag.prereqs.get(nid, set())
                if prereqs and all(nodes[p].state == KanbanState.DONE for p in prereqs):
                    node.state = KanbanState.READY
                    self.save_node(node)
                    newly_ready.append(nid)
        return newly_ready

    # -------------------------------------------------------------------------
    # L0 Token-Frugal Radar, Scheduling & Zwicky Operations
    # -------------------------------------------------------------------------
    def pulse_radar(self, agent_id: str | None = None) -> dict[str, Any]:
        """Return compact L0 Radar (~120 tokens) for AI agent orientation."""
        self.expire_stale_leases()
        nodes = self.load_all_nodes()
        edges = self.load_all_edges()
        dag = DagEngine(nodes, edges)
        cpm_map, cpm_total = dag.compute_cpm(include_done=False)
        leaves = dag.get_unblocked_mikado_leaves()

        leased_by_agent = [
            n for n in nodes.values() if n.state == KanbanState.LEASED
        ]
        quarantined = [n.id for n in nodes.values() if n.state == KanbanState.QUARANTINED]

        return {
            "active_leases": [
                {
                    "task_id": n.id,
                    "title": n.title,
                    "lease_id": n.active_lease_id,
                    "fencing_token": n.fencing_token,
                    "strikes": f"{n.strike_count}/{n.max_strikes}",
                }
                for n in leased_by_agent
            ],
            "critical_path_remaining_min": cpm_total,
            "ready_mikado_leaves": [
                {
                    "task_id": l.id,
                    "title": l.title,
                    "est_min": round(l.estimates.duration_minutes.mean, 1),
                    "float_min": cpm_map[l.id].total_float if l.id in cpm_map else 0.0,
                    "critical": cpm_map[l.id].is_critical if l.id in cpm_map else False,
                }
                for l in leaves[:5]
            ],
            "quarantined_tasks": quarantined,
            "counts": {
                "done": sum(1 for n in nodes.values() if n.state == KanbanState.DONE),
                "total": len(nodes),
            },
        }

    def compute_schedule(
        self, trials: int = 2000, seed: int = 42
    ) -> tuple[dict[str, CpmNodeSchedule], MonteCarloScheduleResult]:
        nodes = self.load_all_nodes()
        edges = self.load_all_edges()
        dag = DagEngine(nodes, edges)
        cpm_schedules, _ = dag.compute_cpm(include_done=False)
        mc_result = dag.simulate_monte_carlo(trials=trials, seed=seed, include_done=False)
        return cpm_schedules, mc_result

    def save_zwicky_box(self, box: ZwickyBox) -> ZwickyBox:
        path = self.zwicky_dir / f"{box.id}.toml"
        write_canonical_toml(path, box.model_dump(mode="json"))
        return box

    def load_all_zwicky_boxes(self) -> dict[str, ZwickyBox]:
        if not self.zwicky_dir.exists():
            return {}
        boxes: dict[str, ZwickyBox] = {}
        for p in sorted(self.zwicky_dir.glob("*.toml")):
            b = ZwickyBox.model_validate(read_toml(p))
            boxes[b.id] = b
        return boxes

    def solve_zwicky_box(
        self, box_id: str, top_k: int = 5, forbid_unassessed: bool = False
    ) -> list[MorphologicalCandidate]:
        boxes = self.load_all_zwicky_boxes()
        if box_id not in boxes:
            matches = [b for bid, b in boxes.items() if bid.startswith(box_id)]
            if len(matches) != 1:
                raise KeyError(f"ZwickyBox '{box_id}' not found.")
            box = matches[0]
        else:
            box = boxes[box_id]
        solver = ZwickySolver(box)
        return solver.solve(top_k=top_k, forbid_unassessed=forbid_unassessed)

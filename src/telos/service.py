"""Transactional Application Service mediating Git-tracked TOML/JSONL files and SQLite WAL Index."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any

from pydantic import BaseModel

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
    ZwickyOption,
    ZwickyPairAssessment,
    utc_now,
)
from telos.toml_codec import read_toml, write_canonical_toml
from telos.verifier import TrustedVerifier
from telos.zwicky import MorphologicalCandidate, ZwickySolver


class StaleFencingTokenError(PermissionError):
    """Raised when an agent presents an expired or superseded lease fencing token."""


class VerificationGateError(PermissionError):
    """Raised when attempting an illegal state transition, WIP violation, or self-approval."""


class TelosService:
    """Single authoritative application service for CLI, MCP, TUI, and Web GUI consumers."""

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
        max_wip_per_agent: int = 1,
        max_wip_total: int = 4,
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
            if super_project_path is not None:
                cfg.super_project_path = super_project_path
            if sub_project_paths:
                cfg.sub_project_paths = sorted(set(cfg.sub_project_paths + sub_project_paths))
            cfg.max_wip_per_agent = max_wip_per_agent
            cfg.max_wip_total = max_wip_total
            write_canonical_toml(cfg_path, cfg.model_dump(mode="json"))
        else:
            cfg = ScopeConfig(
                project_name=project_name,
                super_project_path=super_project_path,
                sub_project_paths=sorted(set(sub_project_paths or [])),
                max_wip_per_agent=max_wip_per_agent,
                max_wip_total=max_wip_total,
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

    def get_scope_config(self) -> ScopeConfig | None:
        cfg_path = self.telos_dir / "config.toml"
        if not cfg_path.exists():
            return None
        return ScopeConfig.model_validate(read_toml(cfg_path))

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

    def _resolve_external_nodes(self, edges: list[GraphEdge]) -> dict[tuple[str, str], TaskNode | None]:
        """Resolve cross-scope node references across federated sub/super project directories."""
        external_map: dict[tuple[str, str], TaskNode | None] = {}
        for edge in edges:
            if not edge.scope_ref:
                continue
            ext_root = (self.root / edge.scope_ref).resolve()
            ext_nodes_dir = ext_root / ".telos" / "nodes"
            node_obj: TaskNode | None = None
            if ext_nodes_dir.exists():
                exact = ext_nodes_dir / f"{edge.target_id}.toml"
                if exact.exists():
                    node_obj = TaskNode.model_validate(read_toml(exact))
                else:
                    matches = list(ext_nodes_dir.glob(f"{edge.target_id}*.toml"))
                    if len(matches) == 1:
                        node_obj = TaskNode.model_validate(read_toml(matches[0]))
            external_map[(edge.scope_ref, edge.target_id)] = node_obj
        return external_map

    def build_dag_engine(self) -> DagEngine:
        nodes = self.load_all_nodes()
        edges = self.load_all_edges()
        ext_nodes = self._resolve_external_nodes(edges)
        return DagEngine(nodes, edges, external_nodes=ext_nodes)

    def rebuild_sqlite_index(self) -> None:
        """Synchronize Git-tracked TOML files into the SQLite WAL index and validate DAG acyclicity."""
        self._init_sqlite_wal()
        nodes = self.load_all_nodes()
        edges = self.load_all_edges()
        ext_nodes = self._resolve_external_nodes(edges)
        dag = DagEngine(nodes, edges, external_nodes=ext_nodes)
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
        if relation == EdgeRelation.DERIVED_FROM:
            z_boxes = self.load_all_zwicky_boxes()
            if target_id not in z_boxes:
                target_id = self.get_zwicky_box(target_id).id
        elif not scope_ref and target_id not in nodes:
            target_id = self.get_node(target_id).id

        new_edge = GraphEdge(
            source_id=source_id, relation=relation, target_id=target_id, scope_ref=scope_ref
        )
        edges = self.load_all_edges()
        candidate_edges = edges + [new_edge]

        if relation in (EdgeRelation.DEPENDS_ON, EdgeRelation.CONTAINS) and not scope_ref:
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
        derived_from_zwicky_id: str | None = None,
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
            derived_from_zwicky_id=derived_from_zwicky_id,
        )
        self.save_node(node)
        if parent_id:
            parent = self.get_node(parent_id)
            self.add_edge(parent.id, EdgeRelation.CONTAINS, node.id)
        if derived_from_zwicky_id:
            self.add_edge(node.id, EdgeRelation.DERIVED_FROM, derived_from_zwicky_id)
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
    # Lease Management, WIP Limits, Heartbeats & Quarantine Recovery
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

    def report_heartbeat(
        self,
        task_id: str,
        lease_id: str,
        fencing_token: int,
        tokens_delta: int = 0,
        extend_ttl_minutes: int = 15,
    ) -> dict[str, Any]:
        node = self.get_node(task_id)
        lease = self._validate_fencing_token(node, lease_id, fencing_token)
        new_tokens = lease.tokens_consumed + max(0, tokens_delta)
        now = utc_now()
        new_expires = now + timedelta(minutes=extend_ttl_minutes)

        if new_tokens > lease.token_budget_limit:
            attempt = AttemptRecord(
                task_id=node.id,
                lease_id=lease.lease_id,
                agent_id=lease.agent_id,
                fencing_token=fencing_token,
                category=FailureCategory.TOKEN_BUDGET_EXCEEDED,
                rationale=(
                    f"Token budget exceeded: consumed {new_tokens} > limit {lease.token_budget_limit}."
                ),
            )
            snap_ref = self.git_mgr.snapshot_and_reset_worktree(
                Path(lease.worktree_path), node.id, attempt.attempt_id, lease.baseline_commit_sha
            )
            attempt.git_snapshot_ref = snap_ref
            self._append_jsonl(self.attempts_file, attempt)

            node.strike_count = node.max_strikes
            node.state = KanbanState.QUARANTINED
            node.active_lease_id = None
            self.save_node(node)

            with self._connect_sqlite() as conn:
                conn.execute(
                    "UPDATE active_leases SET tokens_consumed = ?, active = 0 WHERE lease_id = ?",
                    (new_tokens, lease.lease_id),
                )
            return {
                "active": False,
                "task_id": node.id,
                "state": node.state.value,
                "tokens_consumed": new_tokens,
                "token_budget_limit": lease.token_budget_limit,
                "diagnostic": "Circuit breaker tripped: token budget exceeded. Task QUARANTINED.",
            }

        with self._connect_sqlite() as conn:
            conn.execute(
                """
                UPDATE active_leases
                SET tokens_consumed = ?, last_heartbeat_at = ?, expires_at = ?
                WHERE lease_id = ?
                """,
                (new_tokens, now.isoformat(), new_expires.isoformat(), lease.lease_id),
            )

        return {
            "active": True,
            "task_id": node.id,
            "lease_id": lease.lease_id,
            "tokens_consumed": new_tokens,
            "token_budget_remaining": lease.token_budget_limit - new_tokens,
            "expires_at": new_expires.isoformat(),
        }

    def unquarantine_task(
        self, task_id: str, rationale: str, reset_strikes: int = 0
    ) -> TaskNode:
        node = self.get_node(task_id)
        node.state = KanbanState.READY
        node.strike_count = max(0, reset_strikes)
        node.active_lease_id = None
        if rationale:
            node.concise_spec = f"{node.concise_spec}\n[Unquarantined]: {rationale}".strip()
        return self.save_node(node)

    def checkout_leaf(
        self,
        task_id: str,
        agent_id: str,
        ttl_minutes: int = 30,
        create_worktree: bool = False,
        ignore_wip_limit: bool = False,
    ) -> dict[str, Any]:
        """
        Acquire an exclusive lease with a monotonically incremented fencing_token.
        Enforces Agile Kanban WIP limits (`max_wip_per_agent` and `max_wip_total`),
        local prerequisites/children, and federated cross-scope prerequisites.
        """
        self.expire_stale_leases()
        node = self.get_node(task_id)
        if node.state == KanbanState.QUARANTINED:
            raise VerificationGateError(
                f"Task {node.id} is QUARANTINED after {node.strike_count} strikes. "
                f"Decompose via Mikado or run `telos unquarantine` after review."
            )

        cfg = self.get_scope_config()
        if cfg and not ignore_wip_limit:
            with self._connect_sqlite() as conn:
                agent_active = conn.execute(
                    "SELECT task_id FROM active_leases WHERE active = 1 AND agent_id = ? AND task_id != ?",
                    (agent_id, node.id),
                ).fetchall()
                if len(agent_active) >= cfg.max_wip_per_agent:
                    held_ids = ", ".join(r["task_id"] for r in agent_active)
                    raise VerificationGateError(
                        f"Kanban WIP limit exceeded for agent '{agent_id}' "
                        f"(max_wip_per_agent={cfg.max_wip_per_agent}; currently leasing: {held_ids}). "
                        f"Complete, verify, or Mikado-pivot your active task before leasing another."
                    )
                total_active = conn.execute(
                    "SELECT task_id FROM active_leases WHERE active = 1 AND task_id != ?",
                    (node.id,),
                ).fetchall()
                if len(total_active) >= cfg.max_wip_total:
                    raise VerificationGateError(
                        f"Workspace Kanban WIP limit exceeded (max_wip_total={cfg.max_wip_total})."
                    )

        dag = self.build_dag_engine()
        unfinished_local = [
            pid for pid in dag.prereqs.get(node.id, set()) if dag.nodes[pid].state != KanbanState.DONE
        ]
        unfinished_ext = dag.unresolved_external_blockers(node.id)
        all_blockers = unfinished_local + unfinished_ext
        if all_blockers:
            raise VerificationGateError(
                f"Cannot lease non-leaf task {node.id}; unresolved prerequisites/children: {', '.join(all_blockers)}"
            )

        node.fencing_token += 1
        lease_record = LeaseRecord(
            task_id=node.id,
            agent_id=agent_id,
            fencing_token=node.fencing_token,
            worktree_path=str(self.root),
            baseline_commit_sha=self.git_mgr.current_head_sha(),
            token_budget_limit=int(node.estimates.token_cost.pessimistic),
            expires_at=utc_now() + timedelta(minutes=ttl_minutes),
        )
        if create_worktree:
            wt_path, base_sha = self.git_mgr.create_isolated_worktree(lease_record.lease_id)
            lease_record.worktree_path = str(wt_path)
            lease_record.baseline_commit_sha = base_sha

        node.state = KanbanState.LEASED
        node.active_lease_id = lease_record.lease_id
        self.save_node(node)

        with self._connect_sqlite() as conn:
            conn.execute(
                "UPDATE active_leases SET active = 0 WHERE task_id = ? AND lease_id != ?",
                (node.id, lease_record.lease_id),
            )
            conn.execute(
                """
                INSERT INTO active_leases
                (lease_id, task_id, agent_id, fencing_token, worktree_path, baseline_commit_sha,
                 token_budget_limit, tokens_consumed, acquired_at, expires_at, last_heartbeat_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    lease_record.lease_id,
                    lease_record.task_id,
                    lease_record.agent_id,
                    lease_record.fencing_token,
                    lease_record.worktree_path,
                    lease_record.baseline_commit_sha,
                    lease_record.token_budget_limit,
                    lease_record.tokens_consumed,
                    lease_record.acquired_at.isoformat(),
                    lease_record.expires_at.isoformat(),
                    lease_record.last_heartbeat_at.isoformat(),
                ),
            )

        verifiers = [
            dag.nodes[e.source_id]
            for e in dag.edges
            if e.relation == EdgeRelation.VERIFIES and e.target_id == node.id and e.source_id in dag.nodes
        ]
        past_attempts = self.get_attempts_for_task(node.id)[-3:]

        return {
            "task_id": node.id,
            "title": node.title,
            "spec": node.concise_spec,
            "lease_id": lease_record.lease_id,
            "fencing_token": lease_record.fencing_token,
            "worktree_path": lease_record.worktree_path,
            "strikes": f"{node.strike_count}/{node.max_strikes}",
            "token_budget": lease_record.token_budget_limit,
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
    # Zero-Trust Verification Gate, Worktree Harvest & 3-Strike Breaker
    # -------------------------------------------------------------------------
    def submit_for_verification(
        self, task_id: str, lease_id: str, fencing_token: int
    ) -> dict[str, Any]:
        node = self.get_node(task_id)
        lease = self._validate_fencing_token(node, lease_id, fencing_token)

        dag = self.build_dag_engine()
        unfinished_children = [
            cid for cid in dag.children_map.get(node.id, set())
            if dag.nodes[cid].state != KanbanState.DONE
        ]
        if unfinished_children:
            raise VerificationGateError(
                f"V-Model bottom-up synthesis violation: parent node {node.id} cannot be verified "
                f"until all CONTAINS child nodes are DONE (unfinished: {', '.join(unfinished_children)})."
            )

        verifiers = [
            dag.nodes[e.source_id]
            for e in dag.edges
            if e.relation == EdgeRelation.VERIFIES and e.target_id == node.id and e.source_id in dag.nodes
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
        last_receipt: VerificationReceipt | None = None

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

            if not passed:
                self._append_jsonl(self.receipts_file, receipt)
                assert fail_cat is not None
                attempt = AttemptRecord(
                    task_id=node.id,
                    lease_id=lease.lease_id,
                    agent_id=lease.agent_id,
                    fencing_token=fencing_token,
                    category=fail_cat,
                    rationale=message[:500],
                )
                if fail_cat in (
                    FailureCategory.TEST_TAMPER_ATTEMPT,
                    FailureCategory.AST_CHEAT_DETECTED,
                    FailureCategory.TOKEN_BUDGET_EXCEEDED,
                ):
                    node.strike_count = node.max_strikes
                elif fail_cat == FailureCategory.AGENT_LOGIC_ERROR:
                    node.strike_count += 1

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

            last_receipt = receipt
            all_receipts.append(receipt.receipt_id)
            v_node.state = KanbanState.DONE
            v_node.last_verified_receipt_id = receipt.receipt_id
            self.save_node(v_node)

        # Harvest verified changes from isolated worktree back to self.root & record git ref
        assert last_receipt is not None
        verified_ref = self.git_mgr.harvest_and_cleanup_worktree(
            worktree_path=exec_dir,
            task_id=node.id,
            receipt_id=last_receipt.receipt_id,
            baseline_sha=lease.baseline_commit_sha,
        )
        last_receipt.verified_git_ref = verified_ref
        self._append_jsonl(self.receipts_file, last_receipt)

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
            "verified_git_ref": verified_ref,
            "newly_ready_tasks": unblocked,
        }

    def _auto_ready_dependents(self) -> list[str]:
        dag = self.build_dag_engine()
        newly_ready: list[str] = []
        for nid, node in dag.nodes.items():
            if node.state in (KanbanState.MIKADO_DISCOVERY, KanbanState.BLOCKED):
                prereqs = dag.prereqs.get(nid, set())
                ext_unresolved = dag.unresolved_external_blockers(nid)
                if (prereqs or dag.external_prereqs.get(nid)) and not ext_unresolved and all(
                    dag.nodes[p].state == KanbanState.DONE for p in prereqs
                ):
                    node.state = KanbanState.READY
                    self.save_node(node)
                    newly_ready.append(nid)
        return newly_ready

    # -------------------------------------------------------------------------
    # L0 Token-Frugal Radar, Parallel Waves, Scheduling & Zwicky ADR Promotion
    # -------------------------------------------------------------------------
    def pulse_radar(self, agent_id: str | None = None) -> dict[str, Any]:
        self.expire_stale_leases()
        self._auto_ready_dependents()
        dag = self.build_dag_engine()
        cpm_map, cpm_total = dag.compute_cpm(include_done=False)
        leaves = dag.get_unblocked_mikado_leaves()

        leased_by_agent = [
            n for n in dag.nodes.values() if n.state == KanbanState.LEASED
        ]
        quarantined = [n.id for n in dag.nodes.values() if n.state == KanbanState.QUARANTINED]

        cfg = self.get_scope_config()
        sub_summaries: list[dict[str, Any]] = []
        if cfg:
            for sub_rel in cfg.sub_project_paths:
                sub_root = (self.root / sub_rel).resolve()
                if (sub_root / ".telos").exists():
                    sub_svc = TelosService(sub_root)
                    sub_nodes = sub_svc.load_all_nodes()
                    sub_summaries.append(
                        {
                            "scope": sub_rel,
                            "done": sum(1 for n in sub_nodes.values() if n.state == KanbanState.DONE),
                            "total": len(sub_nodes),
                        }
                    )

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
            "sub_projects": sub_summaries,
            "counts": {
                "done": sum(1 for n in dag.nodes.values() if n.state == KanbanState.DONE),
                "total": len(dag.nodes),
            },
        }

    def compute_schedule(
        self, trials: int = 2000, seed: int = 42
    ) -> tuple[dict[str, CpmNodeSchedule], MonteCarloScheduleResult]:
        dag = self.build_dag_engine()
        cpm_schedules, _ = dag.compute_cpm(include_done=False)
        mc_result = dag.simulate_monte_carlo(trials=trials, seed=seed, include_done=False)
        return cpm_schedules, mc_result

    def compute_parallel_waves(self) -> list[list[dict[str, object]]]:
        dag = self.build_dag_engine()
        return dag.compute_parallel_waves()

    def save_zwicky_box(self, box: ZwickyBox) -> ZwickyBox:
        path = self.zwicky_dir / f"{box.id}.toml"
        write_canonical_toml(path, box.model_dump(mode="json"))
        return box

    def get_zwicky_box(self, box_id: str) -> ZwickyBox:
        boxes = self.load_all_zwicky_boxes()
        if box_id in boxes:
            return boxes[box_id]
        matches = [b for bid, b in boxes.items() if bid.startswith(box_id)]
        if len(matches) == 1:
            return matches[0]
        raise KeyError(f"ZwickyBox '{box_id}' not found.")

    def load_all_zwicky_boxes(self) -> dict[str, ZwickyBox]:
        if not self.zwicky_dir.exists():
            return {}
        boxes: dict[str, ZwickyBox] = {}
        for p in sorted(self.zwicky_dir.glob("*.toml")):
            b = ZwickyBox.model_validate(read_toml(p))
            boxes[b.id] = b
        return boxes

    def create_zwicky_box(
        self,
        title: str,
        problem_statement: str,
        dimensions: dict[str, list[dict[str, Any]]],
        assessments: list[dict[str, Any]] | None = None,
        max_cost_budget: float | None = None,
    ) -> ZwickyBox:
        parsed_dims = {
            dim: [
                ZwickyOption.model_validate(opt) if isinstance(opt, dict) else ZwickyOption(name=str(opt))
                for opt in opts
            ]
            for dim, opts in dimensions.items()
        }
        parsed_assessments = [
            ZwickyPairAssessment.model_validate(a) for a in (assessments or [])
        ]
        box = ZwickyBox(
            title=title,
            problem_statement=problem_statement,
            dimensions=parsed_dims,
            assessments=parsed_assessments,
            max_cost_budget=max_cost_budget,
        )
        return self.save_zwicky_box(box)

    def solve_zwicky_box(
        self, box_id: str, top_k: int = 5, forbid_unassessed: bool = False
    ) -> list[MorphologicalCandidate]:
        box = self.get_zwicky_box(box_id)
        solver = ZwickySolver(box)
        return solver.solve(top_k=top_k, forbid_unassessed=forbid_unassessed)

    def commit_zwicky_configuration(
        self,
        box_id: str,
        rationale: str,
        candidate_rank_index: int = 0,
        spawn_tasks: bool = True,
    ) -> dict[str, Any]:
        box = self.get_zwicky_box(box_id)
        candidates = self.solve_zwicky_box(box.id, top_k=max(5, candidate_rank_index + 1))
        if not candidates or candidate_rank_index >= len(candidates):
            raise ValueError(f"No valid Zwicky morphological candidate at index {candidate_rank_index}")

        winner = candidates[candidate_rank_index]
        box.selected_configuration = winner.configuration
        box.decision_rationale = rationale

        spawned_ids: list[str] = []
        if spawn_tasks:
            parent_arch = self.create_node(
                title=f"Architecture: {box.title}",
                concise_spec=(
                    f"Morphological ADR ({box.id}): {rationale} | "
                    f"Config: {json.dumps(winner.configuration, sort_keys=True)}"
                ),
                kind=NodeKind.L1_ARCHITECTURE,
                state=KanbanState.READY,
                derived_from_zwicky_id=box.id,
            )
            spawned_ids.append(parent_arch.id)
            for dim, chosen_opt in sorted(winner.configuration.items()):
                mod_node = self.create_node(
                    title=f"Implement [{dim}]: {chosen_opt}",
                    concise_spec=f"Derived from Zwicky Box '{box.title}' dimension '{dim}' = '{chosen_opt}'.",
                    kind=NodeKind.L2_MODULE_SPEC,
                    state=KanbanState.READY,
                    parent_id=parent_arch.id,
                    derived_from_zwicky_id=box.id,
                )
                spawned_ids.append(mod_node.id)

        box.spawned_node_ids = spawned_ids
        self.save_zwicky_box(box)
        return {
            "box_id": box.id,
            "selected_configuration": winner.configuration,
            "efficiency_rank_score": winner.efficiency_rank_score,
            "spawned_node_ids": spawned_ids,
        }

    def bootstrap_agent_configs(self) -> dict[str, str]:
        """Generate .mcp.json and AGENTS.md / CLAUDE.md instructions for AI assistants."""
        mcp_json_path = self.root / ".mcp.json"
        mcp_config = {
            "mcpServers": {
                "telos": {
                    "command": "telos-mcp",
                    "args": [],
                }
            }
        }
        mcp_json_path.write_text(json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")

        agent_rules = """# Telos Cognitive Exoskeleton Protocol (Mandatory for AI Agents)

You are operating inside a **Telos** deterministic systems-engineering workspace (`.telos/`).
To prevent context drift, local-optima loops, and reward hacking, follow this strict execution loop:

1. **Orient (`telos_pulse` / `telos_waves`):** At the start of every session, call `telos_pulse` to inspect active leases, critical path tasks (`float_min=0`), and unblocked Mikado leaf tasks.
2. **Design Complex Features (`telos_zwicky_create` / `telos_zwicky_solve` / `telos_zwicky_commit`):** Before decomposing ambiguous architectural features, evaluate dimensions and incompatibilities in a Zwicky Morphological Box.
3. **Lock V-Model Contracts (`telos_contract_lock`):** Architect/Verifier agents lock test files and SHA-256 digests before an Implementer agent leases the task.
4. **Lease Unblocked Leaves Only (`telos_checkout_leaf`):** Never edit code without an active `lease_id` and `fencing_token`. Respect Kanban WIP limits. Read `episodic_memory_do_not_repeat` in the checkout capsule so you never repeat a failed approach.
5. **Pivot Immediately on Blockers (`telos_mikado_discover`):** If you hit an unmet prerequisite or failing upstream assumption, DO NOT brute-force edits across unrelated files. Call `telos_mikado_discover` to snapshot/reset the worktree, spawn the prerequisite leaf, and solve bottom-up.
6. **Report Heartbeats (`telos_heartbeat`):** Report token usage during multi-step work.
7. **Zero-Trust Verification (`telos_submit_verification`):** Never modify locked test files or add `@pytest.mark.skip`, `except: pass`, `except: ...`, or `sys.exit(0)`. Close tasks solely via `telos_submit_verification`.
"""
        agents_md_path = self.root / "AGENTS.md"
        claude_md_path = self.root / "CLAUDE.md"
        agents_md_path.write_text(agent_rules, encoding="utf-8")
        if not claude_md_path.exists():
            claude_md_path.write_text(agent_rules, encoding="utf-8")

        return {
            "mcp_config": str(mcp_json_path),
            "agents_md": str(agents_md_path),
            "claude_md": str(claude_md_path),
        }


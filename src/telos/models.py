"""Core Domain Models enforcing PERT invariants, M:N V-Model edges, WIP limits, and Fencing Tokens."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from telos.ulid import new_ulid


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NodeKind(StrEnum):
    L0_REQUIREMENT = "L0_requirement"
    L1_ARCHITECTURE = "L1_architecture"
    L2_MODULE_SPEC = "L2_module_spec"
    L3_IMPLEMENTATION = "L3_implementation"
    R3_UNIT_VERIFY = "R3_unit_verify"
    R2_INTEGRATION_VERIFY = "R2_integration_verify"
    R1_SYSTEM_VERIFY = "R1_system_verify"
    R0_ACCEPTANCE = "R0_acceptance"

    @property
    def is_left_wing(self) -> bool:
        return self.value.startswith("L")

    @property
    def is_right_wing(self) -> bool:
        return self.value.startswith("R")


class KanbanState(StrEnum):
    BACKLOG = "backlog"
    DESIGNING = "designing"
    MIKADO_DISCOVERY = "mikado_discovery"
    READY = "ready"
    LEASED = "leased"
    VERIFYING = "verifying"
    DONE = "done"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"


class EdgeRelation(StrEnum):
    CONTAINS = "contains"          # Parent -> Child structural hierarchy
    DEPENDS_ON = "depends_on"      # Task A depends on Prerequisite Task B (B must finish before A)
    VERIFIES = "verifies"          # Right-Wing R* node verifies Left-Wing L* node (M:N)
    DERIVED_FROM = "derived_from"  # Spec node derived from Zwicky ADR


class FailureCategory(StrEnum):
    AGENT_LOGIC_ERROR = "agent_logic_error"
    TEST_TAMPER_ATTEMPT = "test_tamper_attempt"
    AST_CHEAT_DETECTED = "ast_cheat_detected"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    MIKADO_PREREQUISITE = "mikado_prerequisite"
    INFRA_OR_FLAKY_ENV = "infra_or_flaky_env"


class ZwickyCompatibility(StrEnum):
    UNASSESSED = "unassessed"
    INCOMPATIBLE = "incompatible"
    NEUTRAL = "neutral"
    SYNERGISTIC = "synergistic"


class PertTriplet(BaseModel):
    optimistic: float = Field(gt=0.0)
    most_likely: float = Field(gt=0.0)
    pessimistic: float = Field(gt=0.0)

    @model_validator(mode="after")
    def enforce_monotonic_order(self) -> "PertTriplet":
        if not (self.optimistic <= self.most_likely <= self.pessimistic):
            raise ValueError(
                f"PERT invariant violated: require 0 < O <= M <= P, got "
                f"O={self.optimistic}, M={self.most_likely}, P={self.pessimistic}"
            )
        return self

    @property
    def mean(self) -> float:
        return (self.optimistic + 4.0 * self.most_likely + self.pessimistic) / 6.0

    @property
    def std_dev(self) -> float:
        return (self.pessimistic - self.optimistic) / 6.0

    @property
    def variance(self) -> float:
        return self.std_dev ** 2


class ResourceEstimates(BaseModel):
    duration_minutes: PertTriplet = Field(
        default_factory=lambda: PertTriplet(optimistic=15.0, most_likely=30.0, pessimistic=60.0)
    )
    token_cost: PertTriplet = Field(
        default_factory=lambda: PertTriplet(optimistic=5000.0, most_likely=15000.0, pessimistic=45000.0)
    )


class VerificationContract(BaseModel):
    command_argv: list[str] = Field(min_length=1)
    locked_paths: list[str] = Field(default_factory=list)
    locked_digests: dict[str, str] = Field(default_factory=dict)
    min_tests_collected: int = Field(default=1, ge=0)
    forbid_skipped_tests: bool = True
    ast_scan_paths: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=120, gt=0)
    approved_by: str = "human-architect"
    approved_at: datetime = Field(default_factory=utc_now)
    # Negative control: the command must FAIL on the pre-implementation tree, or a human attests otherwise.
    baseline_policy: str = "must_fail"  # "must_fail" | "human_attested_green"
    baseline_exit_code: int | None = None
    # Harness surface (conftest.py, pytest config sections, site hooks, scripts) and top-level modules at lock.
    harness_digests: dict[str, str] = Field(default_factory=dict)
    module_names_at_lock: list[str] = Field(default_factory=list)


class TaskNode(BaseModel):
    id: str = Field(default_factory=new_ulid)
    title: str = Field(min_length=1)
    concise_spec: str = ""
    kind: NodeKind = NodeKind.L3_IMPLEMENTATION
    state: KanbanState = KanbanState.BACKLOG
    category: str = "general"
    group_name: str = "default"
    labels: list[str] = Field(default_factory=list)
    estimates: ResourceEstimates = Field(default_factory=ResourceEstimates)
    contract: VerificationContract | None = None
    strike_count: int = Field(default=0, ge=0)
    max_strikes: int = Field(default=3, ge=1)
    fencing_token: int = Field(default=0, ge=0)
    active_lease_id: str | None = None
    last_verified_receipt_id: str | None = None
    derived_from_zwicky_id: str | None = None
    google_task_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    seal: str = ""
    _integrity_error: str | None = PrivateAttr(default=None)

    def security_view(self) -> dict[str, Any]:
        """Fields that open or close gates; sealed by Telos, free text (title, spec, labels) stays editable."""
        return {
            "id": self.id,
            "kind": self.kind.value,
            "state": self.state.value,
            "strike_count": self.strike_count,
            "max_strikes": self.max_strikes,
            "fencing_token": self.fencing_token,
            "active_lease_id": self.active_lease_id,
            "last_verified_receipt_id": self.last_verified_receipt_id,
            "contract": self.contract.model_dump(mode="json") if self.contract else None,
            "token_cost": self.estimates.token_cost.model_dump(mode="json"),
        }


class GraphEdge(BaseModel):
    source_id: str
    relation: EdgeRelation
    target_id: str
    scope_ref: str | None = None  # Optional relative path for federated sub/super project

    @model_validator(mode="after")
    def prevent_self_loop(self) -> "GraphEdge":
        if self.source_id == self.target_id and not self.scope_ref:
            raise ValueError(f"Self-loop edge prohibited on node {self.source_id}")
        return self

    def sort_key(self) -> tuple[str, str, str, str]:
        return (self.source_id, self.relation.value, self.target_id, self.scope_ref or "")


class LeaseRecord(BaseModel):
    lease_id: str = Field(default_factory=new_ulid)
    task_id: str
    agent_id: str
    fencing_token: int = Field(ge=1)
    worktree_path: str
    baseline_commit_sha: str
    token_budget_limit: int = Field(gt=0)
    tokens_consumed: int = Field(default=0, ge=0)
    acquired_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    last_heartbeat_at: datetime = Field(default_factory=utc_now)
    active: bool = True
    seal: str = ""

    def security_view(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "fencing_token": self.fencing_token,
            "worktree_path": self.worktree_path,
            "baseline_commit_sha": self.baseline_commit_sha,
            "token_budget_limit": self.token_budget_limit,
        }


class AttemptRecord(BaseModel):
    attempt_id: str = Field(default_factory=new_ulid)
    task_id: str
    lease_id: str
    agent_id: str
    fencing_token: int
    category: FailureCategory
    rationale: str = Field(max_length=500)
    git_snapshot_ref: str | None = None
    spawned_prerequisite_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class VerificationReceipt(BaseModel):
    receipt_id: str = Field(default_factory=new_ulid)
    task_id: str
    verifier_node_id: str
    lease_id: str
    fencing_token: int
    source_tree_digest: str
    test_tree_digest: str
    command_argv: list[str]
    exit_code: int
    tests_collected: int
    tests_passed: int
    tests_failed: int
    tests_skipped: int
    ast_violations: list[str] = Field(default_factory=list)
    verified_git_ref: str | None = None
    evidence: str = "exit_code"  # "junit" (structured runner report) | "exit_code"
    duration_seconds: float
    executed_at: datetime = Field(default_factory=utc_now)
    hmac_signature: str = ""

    def canonical_payload(self) -> str:
        argv_str = ",".join(self.command_argv)
        return (
            f"{self.receipt_id}|{self.task_id}|{self.verifier_node_id}|"
            f"{self.lease_id}|{self.fencing_token}|{self.source_tree_digest}|"
            f"{self.test_tree_digest}|{argv_str}|{self.exit_code}|"
            f"{self.tests_collected}|{self.tests_passed}|{self.tests_failed}|"
            f"{self.tests_skipped}|{','.join(self.ast_violations)}|"
            f"{self.verified_git_ref or ''}|{self.evidence}"
        )

    @property
    def is_pass(self) -> bool:
        return self.exit_code == 0 and self.tests_failed == 0 and self.tests_collected > 0


class AttestationRecord(BaseModel):
    """Signed human decision: unquarantine, DONE without a test gate, green baseline, or reseal migration."""

    attestation_id: str = Field(default_factory=new_ulid)
    task_id: str
    action: str
    actor: str
    rationale: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    hmac_signature: str = ""

    def canonical_payload(self) -> str:
        return (
            f"{self.attestation_id}|{self.task_id}|{self.action}|{self.actor}|"
            f"{self.rationale}|{self.created_at.isoformat()}"
        )


class ZwickyOption(BaseModel):
    name: str
    cost_score: float = Field(default=1.0, ge=0.0)
    description: str = ""


class ZwickyPairAssessment(BaseModel):
    dim_a: str
    opt_a: str
    dim_b: str
    opt_b: str
    compatibility: ZwickyCompatibility = ZwickyCompatibility.UNASSESSED
    rationale: str = ""


class ZwickyBox(BaseModel):
    id: str = Field(default_factory=new_ulid)
    title: str
    problem_statement: str
    dimensions: dict[str, list[ZwickyOption]]
    assessments: list[ZwickyPairAssessment] = Field(default_factory=list)
    max_cost_budget: float | None = None
    selected_configuration: dict[str, str] | None = None
    decision_rationale: str = ""
    spawned_node_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ScopeConfig(BaseModel):
    scope_id: str = Field(default_factory=new_ulid)
    project_name: str
    super_project_path: str | None = None
    sub_project_paths: list[str] = Field(default_factory=list)
    default_lease_ttl_minutes: int = 30
    max_wip_per_agent: int = Field(default=1, ge=1)
    max_wip_total: int = Field(default=4, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    seal: str = ""

    def security_view(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"seal"})


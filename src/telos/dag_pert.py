"""Deterministic DAG Validator, Critical Path Method (CPM), and Monte Carlo Beta-PERT Engine."""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Iterable

from telos.models import EdgeRelation, GraphEdge, KanbanState, PertTriplet, TaskNode


class CycleDetectedError(ValueError):
    """Raised when adding an edge or merging branches creates a dependency cycle."""


@dataclass(frozen=True)
class CpmNodeSchedule:
    node_id: str
    title: str
    expected_duration: float
    std_dev_duration: float
    early_start: float
    early_finish: float
    late_start: float
    late_finish: float
    total_float: float
    is_critical: bool


@dataclass(frozen=True)
class MonteCarloScheduleResult:
    trials: int
    deterministic_cpm_duration: float
    p50_duration: float
    p80_duration: float
    p95_duration: float
    p50_tokens: float
    p80_tokens: float
    p95_tokens: float
    criticality_index: dict[str, float]  # node_id -> probability [0.0..1.0] of being on critical path


class DagEngine:
    """
    Pure-Python DAG engine for cycle detection, Mikado leaf discovery, V-Model hierarchy rollup,
    cross-scope federation, CPM, and Monte Carlo simulation.
    """

    def __init__(
        self,
        nodes: dict[str, TaskNode],
        edges: Iterable[GraphEdge],
        external_nodes: dict[tuple[str, str], TaskNode | None] | None = None,
    ) -> None:
        self.nodes = dict(nodes)
        self.edges = list(edges)
        self.external_nodes = external_nodes or {}

        # Prerequisite map: prereqs[A] = set of local B where A must wait for B to finish
        self.prereqs: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        self.dependents: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        # Structural hierarchy map: children_map[Parent] = set of Child IDs
        self.children_map: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        # Federated external blockers: external_prereqs[A] = list of (scope_ref, target_id)
        self.external_prereqs: dict[str, list[tuple[str, str]]] = {nid: [] for nid in self.nodes}

        for edge in self.edges:
            if edge.relation == EdgeRelation.DEPENDS_ON:
                if edge.scope_ref:
                    if edge.source_id in self.nodes:
                        self.external_prereqs[edge.source_id].append((edge.scope_ref, edge.target_id))
                elif edge.source_id in self.nodes and edge.target_id in self.nodes:
                    self.prereqs[edge.source_id].add(edge.target_id)
                    self.dependents[edge.target_id].add(edge.source_id)
            elif edge.relation == EdgeRelation.CONTAINS and not edge.scope_ref:
                if edge.source_id in self.nodes and edge.target_id in self.nodes:
                    self.children_map[edge.source_id].add(edge.target_id)
                    # Bottom-up V-Model synthesis: a parent node implicitly depends on its children finishing
                    self.prereqs[edge.source_id].add(edge.target_id)
                    self.dependents[edge.target_id].add(edge.source_id)

    def unresolved_external_blockers(self, node_id: str) -> list[str]:
        """Return human-readable identifiers of any cross-scope prerequisites that are not DONE."""
        unresolved: list[str] = []
        for scope_ref, ext_id in self.external_prereqs.get(node_id, []):
            ext_node = self.external_nodes.get((scope_ref, ext_id))
            if ext_node is None:
                unresolved.append(f"{scope_ref}:{ext_id}(UNRESOLVED_EXTERNAL)")
            elif ext_node.state != KanbanState.DONE:
                unresolved.append(f"{scope_ref}:{ext_id}({ext_node.state.value})")
        return unresolved

    def validate_acyclic(self) -> list[str]:
        """Return topological execution order (leaves/prerequisites first) or raise CycleDetectedError."""
        in_degree = {nid: len(self.prereqs[nid]) for nid in self.nodes}
        ready = sorted([nid for nid, deg in in_degree.items() if deg == 0])
        topo_order: list[str] = []

        while ready:
            current = ready.pop(0)
            topo_order.append(current)
            for dep in sorted(self.dependents[current]):
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    ready.append(dep)
                    ready.sort()

        if len(topo_order) != len(self.nodes):
            cycle_nodes = sorted(set(self.nodes.keys()) - set(topo_order))
            raise CycleDetectedError(
                f"Dependency or hierarchy cycle detected involving nodes: {', '.join(cycle_nodes)}"
            )
        return topo_order

    def get_unblocked_mikado_leaves(self, root_id: str | None = None) -> list[TaskNode]:
        """Return READY, MIKADO_DISCOVERY, or BACKLOG leaf tasks whose local and federated prerequisites are DONE."""
        self.validate_acyclic()
        candidate_ids = set(self.nodes.keys())

        if root_id is not None and root_id in self.nodes:
            visited: set[str] = set()
            stack = [root_id]
            while stack:
                curr = stack.pop()
                if curr in visited or curr not in self.nodes:
                    continue
                visited.add(curr)
                stack.extend(self.prereqs.get(curr, set()))
                stack.extend(self.children_map.get(curr, set()))
            candidate_ids = visited

        leaves: list[TaskNode] = []
        for nid in sorted(candidate_ids):
            node = self.nodes[nid]
            if node.state not in (KanbanState.READY, KanbanState.MIKADO_DISCOVERY, KanbanState.BACKLOG):
                continue
            if node.kind.is_right_wing:
                continue
            unresolved_local = [
                pid for pid in self.prereqs[nid]
                if self.nodes[pid].state != KanbanState.DONE
            ]
            if unresolved_local or self.unresolved_external_blockers(nid):
                continue
            leaves.append(node)

        leaves.sort(
            key=lambda n: (
                0 if n.state == KanbanState.READY else 1,
                n.estimates.duration_minutes.mean,
                n.id,
            )
        )
        return leaves

    def compute_cpm(self, include_done: bool = False) -> tuple[dict[str, CpmNodeSchedule], float]:
        """Run deterministic Forward and Backward pass to compute ES, EF, LS, LF, and Total Float."""
        topo_order = self.validate_acyclic()
        active_order = [
            nid for nid in topo_order
            if include_done or self.nodes[nid].state != KanbanState.DONE
        ]
        if not active_order:
            return {}, 0.0

        active_set = set(active_order)
        es: dict[str, float] = {}
        ef: dict[str, float] = {}

        for nid in active_order:
            dur = self.nodes[nid].estimates.duration_minutes.mean
            pred_efs = [ef[p] for p in self.prereqs[nid] if p in active_set]
            for scope_ref, ext_id in self.external_prereqs.get(nid, []):
                ext_node = self.external_nodes.get((scope_ref, ext_id))
                if ext_node is not None and (include_done or ext_node.state != KanbanState.DONE):
                    pred_efs.append(ext_node.estimates.duration_minutes.mean)
            early_s = max(pred_efs) if pred_efs else 0.0
            es[nid] = early_s
            ef[nid] = early_s + dur

        project_duration = max(ef.values()) if ef else 0.0

        ls: dict[str, float] = {}
        lf: dict[str, float] = {}
        for nid in reversed(active_order):
            dur = self.nodes[nid].estimates.duration_minutes.mean
            succ_lss = [ls[s] for s in self.dependents[nid] if s in active_set]
            late_f = min(succ_lss) if succ_lss else project_duration
            lf[nid] = late_f
            ls[nid] = late_f - dur

        schedules: dict[str, CpmNodeSchedule] = {}
        for nid in active_order:
            node = self.nodes[nid]
            tf = round(ls[nid] - es[nid], 4)
            schedules[nid] = CpmNodeSchedule(
                node_id=nid,
                title=node.title,
                expected_duration=round(node.estimates.duration_minutes.mean, 2),
                std_dev_duration=round(node.estimates.duration_minutes.std_dev, 2),
                early_start=round(es[nid], 2),
                early_finish=round(ef[nid], 2),
                late_start=round(ls[nid], 2),
                late_finish=round(lf[nid], 2),
                total_float=tf,
                is_critical=(abs(tf) <= 1e-4),
            )
        return schedules, round(project_duration, 2)

    @staticmethod
    def _sample_beta_pert(triplet: PertTriplet, rng: random.Random) -> float:
        o, m, p = triplet.optimistic, triplet.most_likely, triplet.pessimistic
        if abs(p - o) <= 1e-9:
            return m
        alpha = 1.0 + 4.0 * ((m - o) / (p - o))
        beta = 1.0 + 4.0 * ((p - m) / (p - o))
        return o + rng.betavariate(alpha, beta) * (p - o)

    def simulate_monte_carlo(
        self, trials: int = 2000, seed: int = 42, include_done: bool = False
    ) -> MonteCarloScheduleResult:
        """Run Monte Carlo Beta-PERT schedule risk analysis capturing parallel path merge bias."""
        topo_order = self.validate_acyclic()
        active_order = [
            nid for nid in topo_order
            if include_done or self.nodes[nid].state != KanbanState.DONE
        ]
        _, det_duration = self.compute_cpm(include_done=include_done)
        if not active_order:
            return MonteCarloScheduleResult(
                trials=trials,
                deterministic_cpm_duration=0.0,
                p50_duration=0.0,
                p80_duration=0.0,
                p95_duration=0.0,
                p50_tokens=0.0,
                p80_tokens=0.0,
                p95_tokens=0.0,
                criticality_index={},
            )

        active_set = set(active_order)
        active_prereqs = {
            nid: [p for p in self.prereqs[nid] if p in active_set]
            for nid in active_order
        }
        active_dependents = {
            nid: [s for s in self.dependents[nid] if s in active_set]
            for nid in active_order
        }

        rng = random.Random(seed)
        total_durations: list[float] = []
        total_tokens: list[float] = []
        critical_hits: dict[str, int] = {nid: 0 for nid in active_order}

        for _ in range(trials):
            sampled_dur: dict[str, float] = {}
            es: dict[str, float] = {}
            ef: dict[str, float] = {}
            trial_tokens = 0.0

            for nid in active_order:
                node = self.nodes[nid]
                d = self._sample_beta_pert(node.estimates.duration_minutes, rng)
                t = self._sample_beta_pert(node.estimates.token_cost, rng)
                sampled_dur[nid] = d
                trial_tokens += t
                preds = active_prereqs[nid]
                early_s = max((ef[p] for p in preds), default=0.0)
                for scope_ref, ext_id in self.external_prereqs.get(nid, []):
                    ext_node = self.external_nodes.get((scope_ref, ext_id))
                    if ext_node is not None and (include_done or ext_node.state != KanbanState.DONE):
                        ext_d = self._sample_beta_pert(ext_node.estimates.duration_minutes, rng)
                        if ext_d > early_s:
                            early_s = ext_d
                es[nid] = early_s
                ef[nid] = early_s + d

            proj_end = max(ef.values())
            total_durations.append(proj_end)
            total_tokens.append(trial_tokens)

            ls: dict[str, float] = {}
            for nid in reversed(active_order):
                succs = active_dependents[nid]
                late_f = min((ls[s] for s in succs), default=proj_end)
                ls[nid] = late_f - sampled_dur[nid]
                if abs(ls[nid] - es[nid]) <= 1e-5:
                    critical_hits[nid] += 1

        total_durations.sort()
        total_tokens.sort()

        def _pct(arr: list[float], q: float) -> float:
            idx = min(int(len(arr) * q), len(arr) - 1)
            return round(arr[idx], 2)

        crit_index = {
            nid: round(hits / trials, 3) for nid, hits in critical_hits.items()
        }
        return MonteCarloScheduleResult(
            trials=trials,
            deterministic_cpm_duration=det_duration,
            p50_duration=_pct(total_durations, 0.50),
            p80_duration=_pct(total_durations, 0.80),
            p95_duration=_pct(total_durations, 0.95),
            p50_tokens=_pct(total_tokens, 0.50),
            p80_tokens=_pct(total_tokens, 0.80),
            p95_tokens=_pct(total_tokens, 0.95),
            criticality_index=crit_index,
        )

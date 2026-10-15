"""Zwicky Morphological Analysis & 4-State Cross-Consistency Assessment (CCA) Solver."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from telos.models import ZwickyBox, ZwickyCompatibility, ZwickyOption

@dataclass(frozen=True)
class MorphologicalCandidate:
    configuration: dict[str, str]
    total_cost: float
    synergy_score: int
    unassessed_pairs: int
    efficiency_rank_score: float

class ZwickySolver:
    """Evaluates morphological boxes with hard incompatibilities, budget limits, and unassessed tracking."""

    def __init__(self, box: ZwickyBox) -> None:
        self.box = box
        self.pair_lookup: dict[tuple[str, str, str, str], ZwickyCompatibility] = {}
        for item in box.assessments:
            k1 = (item.dim_a, item.opt_a, item.dim_b, item.opt_b)
            k2 = (item.dim_b, item.opt_b, item.dim_a, item.opt_a)
            self.pair_lookup[k1] = item.compatibility
            self.pair_lookup[k2] = item.compatibility

    def solve(self, top_k: int = 5, forbid_unassessed: bool = False) -> list[MorphologicalCandidate]:
        dim_names = list(self.box.dimensions.keys())
        if not dim_names:
            return []

        option_lists: list[list[tuple[str, ZwickyOption]]] = [
            [(dim, opt) for opt in self.box.dimensions[dim]] for dim in dim_names
        ]

        surviving: list[MorphologicalCandidate] = []
        for combo in product(*option_lists):
            total_cost = sum(opt.cost_score for _, opt in combo)
            if self.box.max_cost_budget is not None and total_cost > self.box.max_cost_budget:
                continue

            incompatible = False
            synergy = 0
            unassessed = 0

            for i in range(len(combo)):
                dim_a, opt_a = combo[i]
                for j in range(i + 1, len(combo)):
                    dim_b, opt_b = combo[j]
                    rel = self.pair_lookup.get(
                        (dim_a, opt_a.name, dim_b, opt_b.name),
                        ZwickyCompatibility.UNASSESSED,
                    )
                    if rel == ZwickyCompatibility.INCOMPATIBLE:
                        incompatible = True
                        break
                    if rel == ZwickyCompatibility.SYNERGISTIC:
                        synergy += 1
                    elif rel == ZwickyCompatibility.UNASSESSED:
                        unassessed += 1
                if incompatible:
                    break

            if incompatible:
                continue
            if forbid_unassessed and unassessed > 0:
                continue

            rank_score = round((synergy * 2.0) - (total_cost * 0.25) - (unassessed * 0.5), 4)
            surviving.append(
                MorphologicalCandidate(
                    configuration={dim: opt.name for dim, opt in combo},
                    total_cost=round(total_cost, 2),
                    synergy_score=synergy,
                    unassessed_pairs=unassessed,
                    efficiency_rank_score=rank_score,
                )
            )

        surviving.sort(
            key=lambda c: (-c.efficiency_rank_score, c.unassessed_pairs, c.total_cost)
        )
        return surviving[:top_k]

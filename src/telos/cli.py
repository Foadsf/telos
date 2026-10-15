"""Unified Command-Line Interface for Human Architects and CLI/Bash AI Agents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

from telos.mcp_server import format_toon, run_stdio_jsonrpc_server
from telos.models import EdgeRelation, KanbanState, NodeKind
from telos.service import TelosService

console = Console()

def _emit(data: Any, fmt: str) -> None:
    if fmt == "json":
        sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")
    elif fmt == "toon":
        sys.stdout.write(format_toon(data) + "\n")
    else:
        console.print_json(data=data, default=str)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telos",
        description="Telos: Deterministic Project, Mikado, PERT & V-Model Harness for AI Agents",
    )
    parser.add_argument("--root", default=".", help="Workspace directory containing .telos/")
    parser.add_argument(
        "--format", choices=["rich", "toon", "json"], default="rich", help="Output format"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. init
    p_init = sub.add_parser("init", help="Initialize a .telos/ workspace in the target directory")
    p_init.add_argument("--name", default="telos-project", help="Project name")
    p_init.add_argument("--super-project", default=None, help="Relative path to parent super-project")

    # 2. add
    p_add = sub.add_parser("add", help="Create a task or specification node")
    p_add.add_argument("title", help="Task title")
    p_add.add_argument("--spec", default="", help="Concise specification")
    p_add.add_argument("--kind", default="L3_implementation", choices=[k.value for k in NodeKind])
    p_add.add_argument("--state", default="ready", choices=[s.value for s in KanbanState])
    p_add.add_argument("--category", default="general")
    p_add.add_argument("--label", action="append", default=[])
    p_add.add_argument("--opt-min", type=float, default=15.0)
    p_add.add_argument("--likely-min", type=float, default=30.0)
    p_add.add_argument("--pess-min", type=float, default=60.0)
    p_add.add_argument("--parent", default=None, help="Parent task ID")
    p_add.add_argument("--depends-on", default=None, help="Prerequisite task ID")

    # 3. contract-lock
    p_lock = sub.add_parser("contract-lock", help="Lock a V-Model verification contract and SHA-256 tree digests")
    p_lock.add_argument("left_node_id", help="Target Left-Wing implementation/spec node ID")
    p_lock.add_argument("--title", default="Verify implementation", help="Right-Wing verifier title")
    p_lock.add_argument("--path", action="append", required=True, help="Test/config path to SHA-256 lock")
    p_lock.add_argument("--scan-path", action="append", default=[], help="Source path for AST anti-cheat scan")
    p_lock.add_argument("--min-tests", type=int, default=1)
    p_lock.add_argument("cmd", nargs=argparse.REMAINDER, help="Verification command argv (e.g. pytest -q)")

    # 4. pulse
    p_pulse = sub.add_parser("pulse", help="Display L0 Agent Radar (leases, critical path, ready Mikado leaves)")
    p_pulse.add_argument("--agent", default="cli-agent")

    # 5. checkout
    p_co = sub.add_parser("checkout", help="Lease an unblocked Mikado leaf node with a monotonic fencing token")
    p_co.add_argument("task_id", help="Task ID")
    p_co.add_argument("--agent", default="claude-code")
    p_co.add_argument("--worktree", action="store_true", help="Create isolated Git worktree")

    # 6. mikado
    p_mik = sub.add_parser("mikado", help="Record a Mikado prerequisite blocker, snapshot/reset worktree, and pivot")
    p_mik.add_argument("blocked_task_id")
    p_mik.add_argument("--lease-id", required=True)
    p_mik.add_argument("--token", type=int, required=True, help="Fencing token")
    p_mik.add_argument("--prereq-title", required=True)
    p_mik.add_argument("--prereq-spec", default="")
    p_mik.add_argument("--why", required=True, help="Why current attempt was blocked")

    # 7. verify
    p_ver = sub.add_parser("verify", help="Submit a leased task to the zero-trust V-Model verification gate")
    p_ver.add_argument("task_id")
    p_ver.add_argument("--lease-id", required=True)
    p_ver.add_argument("--token", type=int, required=True, help="Fencing token")

    # 8. schedule
    p_sched = sub.add_parser("schedule", help="Compute deterministic CPM and Monte Carlo Beta-PERT schedule")
    p_sched.add_argument("--trials", type=int, default=2000)

    # 9. tui & mcp
    sub.add_parser("tui", help="Launch minimalistic interactive Textual TUI")
    sub.add_parser("mcp", help="Run JSON-RPC 2.0 stdio MCP server for AI agents")

    return parser

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = TelosService(args.root)

    if args.command == "init":
        cfg = service.init_workspace(args.name, super_project_path=args.super_project)
        _emit(cfg.model_dump(mode="json"), args.format)
        return 0

    if args.command == "add":
        node = service.create_node(
            title=args.title,
            concise_spec=args.spec,
            kind=NodeKind(args.kind),
            state=KanbanState(args.state),
            category=args.category,
            labels=args.label,
            optimistic_min=args.opt_min,
            most_likely_min=args.likely_min,
            pessimistic_min=args.pess_min,
            parent_id=args.parent,
        )
        if args.depends_on:
            service.add_edge(node.id, EdgeRelation.DEPENDS_ON, args.depends_on)
        _emit(node.model_dump(mode="json"), args.format)
        return 0

    if args.command == "contract-lock":
        cmd_argv = [c for c in args.cmd if c != "--"]
        if not cmd_argv:
            parser.error("Provide a verification command after options (e.g., -- pytest -q)")
        l_node, r_node = service.approve_verification_contract(
            left_node_id=args.left_node_id,
            verifier_title=args.title,
            command_argv=cmd_argv,
            locked_paths=args.path,
            ast_scan_paths=args.scan_path or args.path,
            min_tests_collected=args.min_tests,
        )
        _emit(
            {
                "left_node_id": l_node.id,
                "right_verifier_id": r_node.id,
                "locked_digests": r_node.contract.locked_digests if r_node.contract else {},
            },
            args.format,
        )
        return 0

    if args.command == "pulse":
        _emit(service.pulse_radar(args.agent), args.format)
        return 0

    if args.command == "checkout":
        res = service.checkout_leaf(args.task_id, args.agent, create_worktree=args.worktree)
        _emit(res, args.format)
        return 0

    if args.command == "mikado":
        res = service.mikado_discover_prerequisite(
            blocked_task_id=args.blocked_task_id,
            lease_id=args.lease_id,
            fencing_token=args.token,
            prerequisite_title=args.prereq_title,
            prerequisite_spec=args.prereq_spec,
            why_blocked_rationale=args.why,
        )
        _emit(res, args.format)
        return 0

    if args.command == "verify":
        res = service.submit_for_verification(args.task_id, args.lease_id, args.token)
        _emit(res, args.format)
        return 0 if res["passed"] else 1

    if args.command == "schedule":
        cpm_map, mc = service.compute_schedule(trials=args.trials)
        if args.format in ("json", "toon"):
            _emit(
                {
                    "cpm": {k: v.__dict__ for k, v in cpm_map.items()},
                    "monte_carlo": mc.__dict__,
                },
                args.format,
            )
        else:
            tbl = Table(
                title=(
                    f"Telos Schedule | CPM={mc.deterministic_cpm_duration}m | "
                    f"MC P50={mc.p50_duration}m P80={mc.p80_duration}m P95={mc.p95_duration}m"
                )
            )
            tbl.add_column("ID")
            tbl.add_column("Task")
            tbl.add_column("Mean±SD")
            tbl.add_column("ES -> EF")
            tbl.add_column("LS -> LF")
            tbl.add_column("Float")
            tbl.add_column("Crit Index")
            for nid, s in cpm_map.items():
                tbl.add_row(
                    nid[-6:],
                    s.title,
                    f"{s.expected_duration}±{s.std_dev_duration}m",
                    f"{s.early_start}->{s.early_finish}",
                    f"{s.late_start}->{s.late_finish}",
                    f"{s.total_float}m",
                    f"{mc.criticality_index.get(nid, 0.0) * 100:.1f}%",
                )
            console.print(tbl)
        return 0

    if args.command == "tui":
        from telos.tui import TelosTuiApp

        app = TelosTuiApp(args.root)
        app.run()
        return 0

    if args.command == "mcp":
        run_stdio_jsonrpc_server(args.root)
        return 0

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

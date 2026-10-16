"""Unified Command-Line Interface for Human Architects and CLI/Bash AI Agents."""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

from telos.mcp_server import ROLES, format_toon, run_stdio_jsonrpc_server, run_streamable_http_server
from telos.models import EdgeRelation, KanbanState, NodeKind
from telos.service import CREATABLE_STATES, TelosService
from telos.web_gui import export_dashboard_file, serve_web_gui

console = Console()


class HumanGateError(PermissionError):
    """Raised when a human-only command is run without an interactive terminal or confirmation."""


def _confirm_human(action: str, subject: str) -> str:
    """
    Human gate for decisions an implementer agent must not take: an interactive terminal plus a typed
    confirmation. It stops agents that act through MCP or non-interactive shells; it is not a security
    boundary against an agent that drives a real terminal (see README, Trust model).
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise HumanGateError(
            f"`{action}` is human-only and needs an interactive terminal. "
            f"Ask the human to run it; agents cannot release gates."
        )
    token = subject[-6:] if subject else action
    typed = input(f"[telos] Human confirmation for {action} on '{subject}'. Type '{token}' to confirm: ").strip()
    if typed != token:
        raise HumanGateError(f"Confirmation for `{action}` did not match; nothing changed.")
    return f"{getpass.getuser()}@tty"


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
        description="Telos: Deterministic Project, Mikado, PERT, Zwicky & V-Model Harness for AI Agents",
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
    p_init.add_argument("--sub-project", action="append", default=[], help="Relative path to child sub-project")
    p_init.add_argument("--max-wip-agent", type=int, default=1, help="Max concurrent leases per AI agent")
    p_init.add_argument("--max-wip-total", type=int, default=4, help="Max total concurrent leases in scope")
    p_init.add_argument("--bootstrap-agents", action="store_true", help="Also generate .mcp.json and AGENTS.md")

    # 2. add
    p_add = sub.add_parser("add", help="Create a task or specification node")
    p_add.add_argument("title", help="Task title")
    p_add.add_argument("--spec", default="", help="Concise specification")
    p_add.add_argument("--kind", default="L3_implementation", choices=[k.value for k in NodeKind])
    p_add.add_argument("--state", default="ready", choices=[s.value for s in CREATABLE_STATES])
    p_add.add_argument("--category", default="general")
    p_add.add_argument("--label", action="append", default=[])
    p_add.add_argument("--opt-min", type=float, default=15.0)
    p_add.add_argument("--likely-min", type=float, default=30.0)
    p_add.add_argument("--pess-min", type=float, default=60.0)
    p_add.add_argument("--parent", default=None, help="Parent task ID (CONTAINS)")
    p_add.add_argument("--depends-on", default=None, help="Prerequisite task ID (DEPENDS_ON)")
    p_add.add_argument("--depends-on-scope", default=None, help="Relative path to external scope for --depends-on")

    # 3. contract-lock
    p_lock = sub.add_parser(
        "contract-lock", help="[human] Lock a failing verification contract before any lease (negative control)"
    )
    p_lock.add_argument("left_node_id", help="Target Left-Wing implementation/spec node ID")
    p_lock.add_argument("--title", default="Verify implementation", help="Right-Wing verifier title")
    p_lock.add_argument("--path", action="append", required=True, help="Test/config path to SHA-256 lock")
    p_lock.add_argument("--scan-path", action="append", default=[], help="Source path for AST anti-cheat scan")
    p_lock.add_argument("--min-tests", type=int, default=1)
    p_lock.add_argument(
        "--allow-green-baseline",
        default=None,
        metavar="RATIONALE",
        help="Accept a command that already passes (regression guard); recorded as a signed human attestation",
    )
    p_lock.add_argument("cmd", nargs="*", help="Verification command argv (e.g. pytest -q)")

    # 4. pulse & waves
    p_pulse = sub.add_parser("pulse", help="Display L0 Agent Radar (leases, critical path, ready Mikado leaves)")
    p_pulse.add_argument("--agent", default="cli-agent")
    sub.add_parser("waves", help="Compute parallel multi-agent execution waves")

    # 5. checkout
    p_co = sub.add_parser("checkout", help="Lease an unblocked Mikado leaf node with a monotonic fencing token")
    p_co.add_argument("task_id", help="Task ID")
    p_co.add_argument("--agent", default="claude-code")
    p_co.add_argument("--worktree", action="store_true", help="Create isolated Git worktree")

    # 6. heartbeat
    p_hb = sub.add_parser("heartbeat", help="Extend lease TTL and record consumed tokens")
    p_hb.add_argument("task_id")
    p_hb.add_argument("--lease-id", required=True)
    p_hb.add_argument("--token", type=int, required=True, help="Fencing token")
    p_hb.add_argument("--tokens-used", type=int, default=0, help="Incremental tokens consumed")

    # 7. mikado
    p_mik = sub.add_parser("mikado", help="Record a Mikado prerequisite blocker, snapshot/reset worktree, and pivot")
    p_mik.add_argument("blocked_task_id")
    p_mik.add_argument("--lease-id", required=True)
    p_mik.add_argument("--token", type=int, required=True, help="Fencing token")
    p_mik.add_argument("--prereq-title", required=True)
    p_mik.add_argument("--prereq-spec", default="")
    p_mik.add_argument("--why", required=True, help="Why current attempt was blocked")

    # 8. verify
    p_ver = sub.add_parser("verify", help="Submit a leased task to the zero-trust V-Model verification gate")
    p_ver.add_argument("task_id")
    p_ver.add_argument("--lease-id", required=True)
    p_ver.add_argument("--token", type=int, required=True, help="Fencing token")

    # 9. unquarantine
    p_unq = sub.add_parser("unquarantine", help="[human] Release a QUARANTINED task back to READY after review")
    p_unq.add_argument("task_id")
    p_unq.add_argument("--rationale", required=True, help="Explanation of fix or architectural adjustment")

    p_att = sub.add_parser("attest-done", help="[human] Mark a task without a test gate DONE (signed attestation)")
    p_att.add_argument("task_id")
    p_att.add_argument("--rationale", required=True)

    p_reseal = sub.add_parser(
        "reseal", help="[human] Create this machine's key if missing and re-sign the reviewed workspace state"
    )
    p_reseal.add_argument("--rationale", required=True)

    # 10. schedule
    p_sched = sub.add_parser("schedule", help="Compute deterministic CPM and Monte Carlo Beta-PERT schedule")
    p_sched.add_argument("--trials", type=int, default=2000)

    # 11. Zwicky commands
    p_zw_create = sub.add_parser("zwicky-create", help="Create a Zwicky Morphological Box from JSON dimensions")
    p_zw_create.add_argument("title")
    p_zw_create.add_argument("--problem", required=True)
    p_zw_create.add_argument("--dimensions-json", required=True, help='JSON dict e.g. \'{"db":["sqlite","toml"]}\'')
    p_zw_create.add_argument("--assessments-json", default="[]", help="JSON list of pairwise assessments")

    p_zw_solve = sub.add_parser("zwicky-solve", help="Solve a Zwicky Morphological Box")
    p_zw_solve.add_argument("box_id")
    p_zw_solve.add_argument("--top-k", type=int, default=5)

    p_zw_commit = sub.add_parser("zwicky-commit", help="Commit a winning Zwicky configuration and spawn L1/L2 tasks")
    p_zw_commit.add_argument("box_id")
    p_zw_commit.add_argument("--rationale", required=True)
    p_zw_commit.add_argument("--rank-index", type=int, default=0)

    # 12. bootstrap-agents, tui, gui, export-html & mcp
    sub.add_parser("bootstrap-agents", help="Generate .mcp.json, AGENTS.md, and CLAUDE.md in workspace")
    sub.add_parser("tui", help="Launch minimalistic interactive Textual TUI")

    p_gui = sub.add_parser("gui", help="Serve minimalist Web GUI dashboard on localhost")
    p_gui.add_argument("--port", type=int, default=8080)
    p_gui.add_argument("--open", action="store_true", help="Open in default web browser")

    p_html = sub.add_parser("export-html", help="Export self-contained HTML dashboard file")
    p_html.add_argument("--out", default="telos-dashboard.html", help="Output HTML file path")

    p_mcp = sub.add_parser("mcp", help="Run MCP server (stdio by default, or Streamable HTTP with --http-port)")
    p_mcp.add_argument("--http-port", type=int, default=None, help="Run Streamable HTTP server on this port")
    p_mcp.add_argument("--role", choices=ROLES, default="implementer", help="Tool surface for this server")

    return parser


def parse_cli(argv: list[str] | None = None) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    """
    Parse argv, treating everything after the first `--` as the verification command.
    argparse's own handling of `--` after a `nargs="*"` positional differs between Python 3.12 and
    3.13+, so the split happens here and behaves the same on every supported version.
    """
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    tail: list[str] = []
    if "--" in raw:
        idx = raw.index("--")
        raw, tail = raw[:idx], raw[idx + 1:]
    args = parser.parse_args(raw)
    if tail:
        if not hasattr(args, "cmd"):
            parser.error("`--` is only valid with contract-lock, before the verification command")
        args.cmd = list(args.cmd) + tail
    return parser, args


def main(argv: list[str] | None = None) -> int:
    parser, args = parse_cli(argv)
    service = TelosService(args.root)
    try:
        return _dispatch(parser, args, service)
    except HumanGateError as exc:
        sys.stderr.write(f"telos: {exc}\n")
        return 3


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace, service: TelosService) -> int:

    if args.command == "init":
        cfg = service.init_workspace(
            args.name,
            super_project_path=args.super_project,
            sub_project_paths=args.sub_project,
            max_wip_per_agent=args.max_wip_agent,
            max_wip_total=args.max_wip_total,
        )
        out: dict[str, Any] = cfg.model_dump(mode="json")
        if args.bootstrap_agents:
            out["bootstrapped_files"] = service.bootstrap_agent_configs()
        _emit(out, args.format)
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
            service.add_edge(
                node.id,
                EdgeRelation.DEPENDS_ON,
                args.depends_on,
                scope_ref=args.depends_on_scope,
            )
        _emit(node.model_dump(mode="json"), args.format)
        return 0

    if args.command == "contract-lock":
        cmd_argv = [c for c in args.cmd if c != "--"]
        if not cmd_argv:
            parser.error("Provide a verification command after options (e.g., -- pytest -q)")
        actor = _confirm_human("contract-lock", args.left_node_id)
        l_node, r_node = service.approve_verification_contract(
            left_node_id=args.left_node_id,
            verifier_title=args.title,
            command_argv=cmd_argv,
            locked_paths=args.path,
            ast_scan_paths=args.scan_path or args.path,
            min_tests_collected=args.min_tests,
            approved_by=actor,
            green_baseline_attested_by=actor if args.allow_green_baseline else None,
            green_baseline_rationale=args.allow_green_baseline or "",
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

    if args.command == "waves":
        waves = service.compute_parallel_waves()
        _emit({"waves": [{"wave": i, "tasks": w} for i, w in enumerate(waves)]}, args.format)
        return 0

    if args.command == "checkout":
        res = service.checkout_leaf(args.task_id, args.agent, create_worktree=args.worktree)
        _emit(res, args.format)
        return 0

    if args.command == "heartbeat":
        res = service.report_heartbeat(
            args.task_id, args.lease_id, args.token, tokens_delta=args.tokens_used
        )
        _emit(res, args.format)
        return 0 if res["active"] else 1

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

    if args.command == "unquarantine":
        actor = _confirm_human("unquarantine", args.task_id)
        node = service.unquarantine_task(args.task_id, args.rationale, actor=actor)
        _emit(node.model_dump(mode="json"), args.format)
        return 0

    if args.command == "attest-done":
        actor = _confirm_human("attest-done", args.task_id)
        node = service.attest_done(args.task_id, actor=actor, rationale=args.rationale)
        _emit(node.model_dump(mode="json"), args.format)
        return 0

    if args.command == "reseal":
        actor = _confirm_human("reseal", str(service.root))
        _emit(service.reseal_workspace(actor=actor, rationale=args.rationale), args.format)
        return 0

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

    if args.command == "zwicky-create":
        dims = json.loads(args.dimensions_json)
        assessments = json.loads(args.assessments_json)
        box = service.create_zwicky_box(args.title, args.problem, dims, assessments)
        _emit(box.model_dump(mode="json"), args.format)
        return 0

    if args.command == "zwicky-solve":
        candidates = service.solve_zwicky_box(args.box_id, top_k=args.top_k)
        _emit([c.__dict__ for c in candidates], args.format)
        return 0

    if args.command == "zwicky-commit":
        res = service.commit_zwicky_configuration(
            args.box_id, rationale=args.rationale, candidate_rank_index=args.rank_index
        )
        _emit(res, args.format)
        return 0

    if args.command == "bootstrap-agents":
        _emit(service.bootstrap_agent_configs(), args.format)
        return 0

    if args.command == "export-html":
        out_p = export_dashboard_file(service, Path(args.out))
        _emit({"exported_html": str(out_p)}, args.format)
        return 0

    if args.command == "gui":
        serve_web_gui(args.root, port=args.port, open_browser=args.open)
        return 0

    if args.command == "tui":
        from telos.tui import TelosTuiApp

        app = TelosTuiApp(args.root)
        app.run()
        return 0

    if args.command == "mcp":
        if args.http_port is not None:
            run_streamable_http_server(args.root, port=args.http_port, role=args.role)
        else:
            run_stdio_jsonrpc_server(args.root, role=args.role)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


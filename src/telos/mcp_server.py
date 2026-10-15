"""
Token-Frugal Model Context Protocol (MCP) Server for AI Agents.
Supports both JSON-RPC 2.0 `stdio` transport and MCP Streamable HTTP (`POST /mcp`) out-of-the-box.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from typing import Any

from telos.models import EdgeRelation, KanbanState, NodeKind
from telos.service import TelosService


def format_toon(data: Any, indent: int = 0) -> str:
    """Compact Token-Oriented Object Notation (TOON/YAML-lite) serializer to minimize LLM context usage."""
    pad = "  " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for k, v in data.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(format_toon(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {json.dumps(v) if isinstance(v, list) else v}")
        return "\n".join(lines)
    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, dict):
                compact = " | ".join(f"{k}={v}" for k, v in item.items())
                lines.append(f"{pad}- {compact}")
            else:
                lines.append(f"{pad}- {item}")
        return "\n".join(lines)
    return f"{pad}{data}"


class TelosMcpDispatcher:
    """Executes cognitive MCP tools against a local `.telos/` workspace."""

    def __init__(self, workspace_root: Path | str = ".") -> None:
        self.service = TelosService(workspace_root)

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "telos_pulse",
                "description": "Return L0 Radar (~120 tokens): active leases, critical path duration, federated sub-projects, and top ready Mikado leaves.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"agent_id": {"type": "string"}},
                },
            },
            {
                "name": "telos_waves",
                "description": "Return collision-free parallel execution waves for multi-agent dispatch.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "telos_checkout_leaf",
                "description": "Lease an unblocked Mikado leaf node with a monotonic fencing_token and retrieve L1 Context Capsule + failure history.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                        "create_worktree": {"type": "boolean", "default": False},
                    },
                    "required": ["task_id", "agent_id"],
                },
            },
            {
                "name": "telos_contract_lock",
                "description": "Lock a V-Model Right-Wing verification contract and SHA-256 test digests (role-separated: cannot be called by the active implementer agent).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "left_node_id": {"type": "string"},
                        "verifier_title": {"type": "string", "default": "Unit Verification"},
                        "command_argv": {"type": "array", "items": {"type": "string"}},
                        "locked_paths": {"type": "array", "items": {"type": "string"}},
                        "ast_scan_paths": {"type": "array", "items": {"type": "string"}},
                        "min_tests_collected": {"type": "integer", "default": 1},
                        "active_agent_id": {"type": "string"},
                    },
                    "required": ["left_node_id", "command_argv", "locked_paths"],
                },
            },
            {
                "name": "telos_heartbeat",
                "description": "Extend active lease TTL and record token consumption; trips circuit breaker if pessimistic token budget is exceeded.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "lease_id": {"type": "string"},
                        "fencing_token": {"type": "integer"},
                        "tokens_delta": {"type": "integer", "default": 0},
                    },
                    "required": ["task_id", "lease_id", "fencing_token"],
                },
            },
            {
                "name": "telos_mikado_discover",
                "description": "Record a discovered prerequisite blocker, snapshot/reset dirty state without git stash, link dependency, and checkout the new leaf.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "blocked_task_id": {"type": "string"},
                        "lease_id": {"type": "string"},
                        "fencing_token": {"type": "integer"},
                        "prerequisite_title": {"type": "string"},
                        "prerequisite_spec": {"type": "string"},
                        "why_blocked_rationale": {"type": "string"},
                    },
                    "required": [
                        "blocked_task_id",
                        "lease_id",
                        "fencing_token",
                        "prerequisite_title",
                        "why_blocked_rationale",
                    ],
                },
            },
            {
                "name": "telos_submit_verification",
                "description": "Run the zero-trust V-Model verification gate (child completion check, SHA-256 test digest check, AST anti-skip scan, subprocess test runner) to close a leased task.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "lease_id": {"type": "string"},
                        "fencing_token": {"type": "integer"},
                    },
                    "required": ["task_id", "lease_id", "fencing_token"],
                },
            },
            {
                "name": "telos_unquarantine",
                "description": "Release a QUARANTINED task back to READY after human review or architectural pivot.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["task_id", "rationale"],
                },
            },
            {
                "name": "telos_schedule_pert",
                "description": "Run deterministic CPM and Monte Carlo Beta-PERT simulation (P50/P80/P95 duration & tokens + Criticality Index).",
                "inputSchema": {
                    "type": "object",
                    "properties": {"trials": {"type": "integer", "default": 2000}},
                },
            },
            {
                "name": "telos_zwicky_create",
                "description": "Create a Zwicky Morphological Box with dimensions, options, and pairwise Cross-Consistency Assessments.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "problem_statement": {"type": "string"},
                        "dimensions": {"type": "object"},
                        "assessments": {"type": "array", "items": {"type": "object"}},
                        "max_cost_budget": {"type": "number"},
                    },
                    "required": ["title", "problem_statement", "dimensions"],
                },
            },
            {
                "name": "telos_zwicky_solve",
                "description": "Solve a Zwicky Morphological Box using Cross-Consistency Assessment to rank conflict-free architectures.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "box_id": {"type": "string"},
                        "top_k": {"type": "integer", "default": 5},
                    },
                    "required": ["box_id"],
                },
            },
            {
                "name": "telos_zwicky_commit",
                "description": "Commit a winning Zwicky Morphological configuration as an ADR and automatically spawn L1/L2 tasks linked via DERIVED_FROM.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "box_id": {"type": "string"},
                        "rationale": {"type": "string"},
                        "candidate_rank_index": {"type": "integer", "default": 0},
                        "spawn_tasks": {"type": "boolean", "default": True},
                    },
                    "required": ["box_id", "rationale"],
                },
            },
            {
                "name": "telos_decompose",
                "description": "Create a new task node and optionally attach it under a parent or prerequisite (supports cross-scope `scope_ref`).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "concise_spec": {"type": "string", "default": ""},
                        "kind": {"type": "string", "default": "L3_implementation"},
                        "optimistic_min": {"type": "number", "default": 15.0},
                        "most_likely_min": {"type": "number", "default": 30.0},
                        "pessimistic_min": {"type": "number", "default": 60.0},
                        "parent_id": {"type": "string"},
                        "depends_on_id": {"type": "string"},
                        "depends_on_scope_ref": {"type": "string"},
                    },
                    "required": ["title"],
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            if name == "telos_pulse":
                res = self.service.pulse_radar(arguments.get("agent_id"))
                return format_toon(res)
            if name == "telos_waves":
                waves = self.service.compute_parallel_waves()
                return format_toon({"waves": [{"wave": idx, "tasks": w} for idx, w in enumerate(waves)]})
            if name == "telos_checkout_leaf":
                res = self.service.checkout_leaf(
                    task_id=arguments["task_id"],
                    agent_id=arguments["agent_id"],
                    create_worktree=bool(arguments.get("create_worktree", False)),
                )
                return format_toon(res)
            if name == "telos_contract_lock":
                l_node, r_node = self.service.approve_verification_contract(
                    left_node_id=arguments["left_node_id"],
                    verifier_title=arguments.get("verifier_title", "Unit Verification"),
                    command_argv=list(arguments["command_argv"]),
                    locked_paths=list(arguments["locked_paths"]),
                    ast_scan_paths=list(arguments.get("ast_scan_paths") or arguments["locked_paths"]),
                    min_tests_collected=int(arguments.get("min_tests_collected", 1)),
                    active_agent_id=arguments.get("active_agent_id"),
                )
                return format_toon(
                    {
                        "left_node_id": l_node.id,
                        "right_verifier_id": r_node.id,
                        "locked_digests": r_node.contract.locked_digests if r_node.contract else {},
                    }
                )
            if name == "telos_heartbeat":
                res = self.service.report_heartbeat(
                    task_id=arguments["task_id"],
                    lease_id=arguments["lease_id"],
                    fencing_token=int(arguments["fencing_token"]),
                    tokens_delta=int(arguments.get("tokens_delta", 0)),
                )
                return format_toon(res)
            if name == "telos_mikado_discover":
                res = self.service.mikado_discover_prerequisite(
                    blocked_task_id=arguments["blocked_task_id"],
                    lease_id=arguments["lease_id"],
                    fencing_token=int(arguments["fencing_token"]),
                    prerequisite_title=arguments["prerequisite_title"],
                    prerequisite_spec=arguments.get("prerequisite_spec", ""),
                    why_blocked_rationale=arguments["why_blocked_rationale"],
                )
                return format_toon(res)
            if name == "telos_submit_verification":
                res = self.service.submit_for_verification(
                    task_id=arguments["task_id"],
                    lease_id=arguments["lease_id"],
                    fencing_token=int(arguments["fencing_token"]),
                )
                return format_toon(res)
            if name == "telos_unquarantine":
                node = self.service.unquarantine_task(
                    task_id=arguments["task_id"],
                    rationale=arguments["rationale"],
                )
                return format_toon({"task_id": node.id, "state": node.state.value, "strikes": node.strike_count})
            if name == "telos_schedule_pert":
                cpm, mc = self.service.compute_schedule(trials=int(arguments.get("trials", 2000)))
                return format_toon(
                    {
                        "deterministic_cpm_min": mc.deterministic_cpm_duration,
                        "monte_carlo_p50_min": mc.p50_duration,
                        "monte_carlo_p80_min": mc.p80_duration,
                        "monte_carlo_p95_min": mc.p95_duration,
                        "monte_carlo_p80_tokens": mc.p80_tokens,
                        "critical_tasks": [
                            f"{s.node_id[-6:]}({s.title}): ES={s.early_start} EF={s.early_finish} CI={mc.criticality_index.get(s.node_id, 0.0)}"
                            for s in cpm.values()
                            if s.is_critical
                        ],
                    }
                )
            if name == "telos_zwicky_create":
                box = self.service.create_zwicky_box(
                    title=arguments["title"],
                    problem_statement=arguments["problem_statement"],
                    dimensions=arguments["dimensions"],
                    assessments=arguments.get("assessments"),
                    max_cost_budget=arguments.get("max_cost_budget"),
                )
                return format_toon({"box_id": box.id, "title": box.title, "dimensions": list(box.dimensions.keys())})
            if name == "telos_zwicky_solve":
                candidates = self.service.solve_zwicky_box(
                    box_id=arguments["box_id"], top_k=int(arguments.get("top_k", 5))
                )
                return format_toon([c.__dict__ for c in candidates])
            if name == "telos_zwicky_commit":
                res = self.service.commit_zwicky_configuration(
                    box_id=arguments["box_id"],
                    rationale=arguments["rationale"],
                    candidate_rank_index=int(arguments.get("candidate_rank_index", 0)),
                    spawn_tasks=bool(arguments.get("spawn_tasks", True)),
                )
                return format_toon(res)
            if name == "telos_decompose":
                node = self.service.create_node(
                    title=arguments["title"],
                    concise_spec=arguments.get("concise_spec", ""),
                    kind=NodeKind(arguments.get("kind", "L3_implementation")),
                    state=KanbanState.READY,
                    optimistic_min=float(arguments.get("optimistic_min", 15.0)),
                    most_likely_min=float(arguments.get("most_likely_min", 30.0)),
                    pessimistic_min=float(arguments.get("pessimistic_min", 60.0)),
                    parent_id=arguments.get("parent_id"),
                )
                if arguments.get("depends_on_id"):
                    self.service.add_edge(
                        node.id,
                        EdgeRelation.DEPENDS_ON,
                        arguments["depends_on_id"],
                        scope_ref=arguments.get("depends_on_scope_ref"),
                    )
                return format_toon({"created_task_id": node.id, "title": node.title, "state": node.state.value})
            return f"ERROR: Unknown tool '{name}'"
        except Exception as exc:
            return f"TELOS_ERROR ({type(exc).__name__}): {exc}"

    def handle_jsonrpc_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "telos-harness", "version": "0.4.0"},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self.tool_definitions()},
            }
        if method == "tools/call":
            t_name = params.get("name", "")
            t_args = params.get("arguments", {}) or {}
            text_out = self.call_tool(t_name, t_args)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": text_out}],
                    "isError": text_out.startswith("TELOS_ERROR"),
                },
            }
        if msg_id is not None:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        return None


def run_stdio_jsonrpc_server(workspace_root: Path | str = ".") -> None:
    """Built-in MCP JSON-RPC 2.0 stdio transport server."""
    dispatcher = TelosMcpDispatcher(workspace_root)
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = dispatcher.handle_jsonrpc_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def run_streamable_http_server(
    workspace_root: Path | str = ".", host: str = "127.0.0.1", port: int = 8765
) -> None:
    """Built-in stateless MCP Streamable HTTP (`POST /mcp`) server."""
    dispatcher = TelosMcpDispatcher(workspace_root)

    class _McpHttpHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path not in ("/mcp", "/"):
                self.send_error(404, "Not Found")
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return

            resp = dispatcher.handle_jsonrpc_message(payload)
            if resp is None:
                self.send_response(202)
                self.end_headers()
                return

            body_bytes = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), _McpHttpHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    run_stdio_jsonrpc_server(".")


if __name__ == "__main__":
    main()


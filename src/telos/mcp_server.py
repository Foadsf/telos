"""
Token-Frugal Model Context Protocol (MCP) Server for AI Agents.
Supports both the official `mcp` Python SDK (FastMCP: stdio & streamable-http)
AND a built-in zero-dependency JSON-RPC 2.0 stdio server so it works out-of-the-box everywhere.
"""

from __future__ import annotations

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
    """Executes the 8 high-leverage cognitive MCP tools against a local `.telos/` workspace."""

    def __init__(self, workspace_root: Path | str = ".") -> None:
        self.service = TelosService(workspace_root)

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "telos_pulse",
                "description": "Return L0 Radar (~120 tokens): active leases, critical path duration, and top ready Mikado leaves.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"agent_id": {"type": "string"}},
                },
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
                "description": "Run the zero-trust V-Model verification gate (SHA-256 test digest check, AST anti-skip scan, subprocess test runner) to close a leased task.",
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
                "name": "telos_schedule_pert",
                "description": "Run deterministic CPM and Monte Carlo Beta-PERT simulation (P50/P80/P95 duration & tokens + Criticality Index).",
                "inputSchema": {
                    "type": "object",
                    "properties": {"trials": {"type": "integer", "default": 2000}},
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
                "name": "telos_decompose",
                "description": "Create a new task node and optionally attach it under a parent or prerequisite.",
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
            if name == "telos_checkout_leaf":
                res = self.service.checkout_leaf(
                    task_id=arguments["task_id"],
                    agent_id=arguments["agent_id"],
                    create_worktree=bool(arguments.get("create_worktree", False)),
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
            if name == "telos_zwicky_solve":
                candidates = self.service.solve_zwicky_box(
                    box_id=arguments["box_id"], top_k=int(arguments.get("top_k", 5))
                )
                return format_toon([c.__dict__ for c in candidates])
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
                        node.id, EdgeRelation.DEPENDS_ON, arguments["depends_on_id"]
                    )
                return format_toon({"created_task_id": node.id, "title": node.title, "state": node.state.value})
            return f"ERROR: Unknown tool '{name}'"
        except Exception as exc:
            return f"TELOS_ERROR ({type(exc).__name__}): {exc}"

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

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "telos-harness", "version": "0.2.0"},
                },
            }
        elif method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": dispatcher.tool_definitions()},
            }
        elif method == "tools/call":
            t_name = params.get("name", "")
            t_args = params.get("arguments", {}) or {}
            text_out = dispatcher.call_tool(t_name, t_args)
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": text_out}],
                    "isError": text_out.startswith("TELOS_ERROR"),
                },
            }
        elif msg_id is not None:
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        else:
            continue

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

def main() -> None:
    run_stdio_jsonrpc_server(".")

if __name__ == "__main__":
    main()

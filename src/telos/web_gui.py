"""Minimalist, zero-dependency Browser GUI & Static HTML Dashboard Exporter for Telos."""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
import webbrowser

from telos.models import EdgeRelation, KanbanState
from telos.service import TelosService


def render_dashboard_html(service: TelosService) -> str:
    """Render a self-contained, high-contrast minimalist HTML5 dashboard of the Telos workspace."""
    cfg = service.get_scope_config()
    project_name = cfg.project_name if cfg else "Telos Workspace"
    nodes = service.load_all_nodes()
    edges = service.load_all_edges()
    cpm_map, mc = service.compute_schedule(trials=1500)
    waves = service.compute_parallel_waves()

    # Build Kanban columns
    kanban_cols = [
        ("READY / DISCOVERY", [KanbanState.READY, KanbanState.MIKADO_DISCOVERY, KanbanState.BACKLOG]),
        ("LEASED (IN PROGRESS)", [KanbanState.LEASED, KanbanState.VERIFYING]),
        ("QUARANTINED / BLOCKED", [KanbanState.QUARANTINED, KanbanState.BLOCKED]),
        ("DONE (VERIFIED)", [KanbanState.DONE]),
    ]

    kanban_html_parts: list[str] = []
    for col_title, states in kanban_cols:
        col_nodes = [n for n in nodes.values() if n.state in states]
        cards: list[str] = []
        for n in col_nodes:
            badge_cls = "badge-done" if n.state == KanbanState.DONE else (
                "badge-quarantine" if n.state == KanbanState.QUARANTINED else "badge-ready"
            )
            cards.append(
                f"""
                <div class="card">
                    <div class="card-header">
                        <span class="mono-id">{html.escape(n.id[-8:])}</span>
                        <span class="badge {badge_cls}">{html.escape(n.state.value.upper())}</span>
                    </div>
                    <div class="card-title">{html.escape(n.title)}</div>
                    <div class="card-meta">
                        <span>{html.escape(n.kind.value)}</span>
                        <span>Gen #{n.fencing_token}</span>
                        <span>Strikes {n.strike_count}/{n.max_strikes}</span>
                        <span>{n.estimates.duration_minutes.mean:.1f}m</span>
                    </div>
                </div>
                """
            )
        kanban_html_parts.append(
            f"""
            <div class="kanban-col">
                <div class="col-header">{html.escape(col_title)} <span>({len(col_nodes)})</span></div>
                {''.join(cards) or '<div class="empty-hint">No tasks</div>'}
            </div>
            """
        )

    # Build PERT / Gantt rows
    max_finish = max((s.late_finish for s in cpm_map.values()), default=1.0) or 1.0
    gantt_rows: list[str] = []
    for nid, s in cpm_map.items():
        left_pct = (s.early_start / max_finish) * 100.0
        width_pct = max(2.0, ((s.early_finish - s.early_start) / max_finish) * 100.0)
        bar_cls = "gantt-bar-crit" if s.is_critical else "gantt-bar-norm"
        ci_pct = mc.criticality_index.get(nid, 0.0) * 100.0
        gantt_rows.append(
            f"""
            <tr>
                <td class="mono-id">{html.escape(nid[-6:])}</td>
                <td>{html.escape(s.title)}</td>
                <td>{s.early_start:.1f}m &rarr; {s.early_finish:.1f}m</td>
                <td>{s.total_float:.1f}m</td>
                <td>{ci_pct:.0f}%</td>
                <td style="width: 38%;">
                    <div class="gantt-track">
                        <div class="{bar_cls}" style="left:{left_pct:.1f}%; width:{width_pct:.1f}%;"></div>
                    </div>
                </td>
            </tr>
            """
        )

    # Build V-Model rows
    vmodel_rows: list[str] = []
    for e in edges:
        if e.relation == EdgeRelation.VERIFIES and e.source_id in nodes and e.target_id in nodes:
            r_node = nodes[e.source_id]
            l_node = nodes[e.target_id]
            locked = ", ".join(r_node.contract.locked_paths) if r_node.contract else "None"
            vmodel_rows.append(
                f"""
                <tr>
                    <td>{html.escape(l_node.title)} <span class="mono-id">({html.escape(l_node.id[-6:])})</span></td>
                    <td><span class="badge badge-ready">{html.escape(l_node.state.value.upper())}</span></td>
                    <td>{html.escape(r_node.title)} <span class="mono-id">({html.escape(r_node.id[-6:])})</span></td>
                    <td class="mono-id">{html.escape(locked)}</td>
                    <td class="mono-id">{html.escape((l_node.last_verified_receipt_id or 'Pending')[-10:])}</td>
                </tr>
                """
            )

    # Build Parallel Waves summary
    wave_blocks: list[str] = []
    for idx, w in enumerate(waves):
        items = ", ".join(f"{html.escape(str(t['title']))} ({html.escape(str(t['task_id'])[-6:])})" for t in w)
        wave_blocks.append(f"<div class='wave-row'><strong>Wave {idx}:</strong> {items}</div>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(project_name)} // Telos Cognitive Exoskeleton</title>
<style>
:root {{
    --bg: #0a0d12;
    --panel: #121721;
    --border: #232b3a;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --crit: #f85149;
    --ok: #3fb950;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px;
    line-height: 1.5;
}}
header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
}}
h1 {{ font-size: 1.25rem; font-weight: 600; letter-spacing: 0.02em; }}
.metrics-strip {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
}}
.metric-card {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px;
}}
.metric-label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; }}
.metric-val {{ font-size: 1.35rem; font-weight: 600; margin-top: 4px; color: var(--accent); }}
.kanban-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 28px;
}}
.kanban-col {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    min-height: 180px;
}}
.col-header {{
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--muted);
    margin-bottom: 12px;
    display: flex;
    justify-content: space-between;
}}
.card {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px;
    margin-bottom: 8px;
}}
.card-header {{ display: flex; justify-content: space-between; margin-bottom: 6px; }}
.card-title {{ font-weight: 500; font-size: 0.9rem; margin-bottom: 6px; }}
.card-meta {{ font-size: 0.75rem; color: var(--muted); display: flex; gap: 8px; flex-wrap: wrap; }}
.mono-id {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.75rem; color: var(--muted); }}
.badge {{ font-size: 0.68rem; padding: 2px 6px; border-radius: 4px; font-weight: 600; }}
.badge-ready {{ background: rgba(88,166,255,0.15); color: var(--accent); }}
.badge-done {{ background: rgba(63,185,80,0.15); color: var(--ok); }}
.badge-quarantine {{ background: rgba(248,81,73,0.2); color: var(--crit); }}
.section-panel {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 24px;
}}
.section-title {{ font-size: 0.95rem; font-weight: 600; margin-bottom: 12px; color: var(--text); }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
th {{ color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }}
.gantt-track {{ position: relative; height: 12px; background: var(--bg); border-radius: 4px; overflow: hidden; }}
.gantt-bar-crit {{ position: absolute; top: 0; bottom: 0; background: var(--crit); border-radius: 4px; }}
.gantt-bar-norm {{ position: absolute; top: 0; bottom: 0; background: var(--accent); border-radius: 4px; }}
.wave-row {{ font-size: 0.85rem; padding: 6px 0; border-bottom: 1px solid var(--border); }}
.empty-hint {{ font-size: 0.8rem; color: var(--muted); font-style: italic; }}
</style>
</head>
<body>
<header>
    <h1>TELOS // {html.escape(project_name)}</h1>
    <span class="mono-id">Scope ID: {html.escape(cfg.scope_id if cfg else 'N/A')}</span>
</header>
<div class="metrics-strip">
    <div class="metric-card">
        <div class="metric-label">Deterministic CPM</div>
        <div class="metric-val">{mc.deterministic_cpm_duration:.1f}m</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">Monte Carlo P50 / P80 / P95</div>
        <div class="metric-val">{mc.p50_duration:.0f}m / {mc.p80_duration:.0f}m / {mc.p95_duration:.0f}m</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">Token Budget (P80)</div>
        <div class="metric-val">{mc.p80_tokens:,.0f} tok</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">Verified / Total Nodes</div>
        <div class="metric-val">{sum(1 for n in nodes.values() if n.state == KanbanState.DONE)} / {len(nodes)}</div>
    </div>
</div>

<div class="kanban-grid">
    {''.join(kanban_html_parts)}
</div>

<div class="section-panel">
    <div class="section-title">PERT / CPM &amp; Monte Carlo Criticality Gantt</div>
    <table>
        <thead>
            <tr><th>ID</th><th>Task</th><th>Early Window</th><th>Float</th><th>Crit Index</th><th>Timeline</th></tr>
        </thead>
        <tbody>
            {''.join(gantt_rows) or '<tr><td colspan="6" class="empty-hint">No active tasks</td></tr>'}
        </tbody>
    </table>
</div>

<div class="section-panel">
    <div class="section-title">Multi-Agent Parallel Execution Waves</div>
    {''.join(wave_blocks) or '<div class="empty-hint">No active execution waves</div>'}
</div>

<div class="section-panel">
    <div class="section-title">V-Model Traceability &amp; Cryptographic Receipts</div>
    <table>
        <thead>
            <tr><th>Left-Wing Spec</th><th>State</th><th>Right-Wing Verifier</th><th>Locked SHA-256 Paths</th><th>Receipt</th></tr>
        </thead>
        <tbody>
            {''.join(vmodel_rows) or '<tr><td colspan="5" class="empty-hint">No V-Model contracts registered</td></tr>'}
        </tbody>
    </table>
</div>
</body>
</html>
"""


def export_dashboard_file(service: TelosService, out_path: Path) -> Path:
    html_doc = render_dashboard_html(service)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path.resolve()


def serve_web_gui(
    workspace_root: Path | str = ".", host: str = "127.0.0.1", port: int = 8080, open_browser: bool = False
) -> None:
    service = TelosService(workspace_root)

    class _GuiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/state":
                payload = json.dumps(service.pulse_radar(), default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            body = render_dashboard_html(service).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), _GuiHandler)
    if open_browser:
        webbrowser.open(f"http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


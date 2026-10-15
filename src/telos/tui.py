"""Minimalist, keyboard-driven Textual TUI for Human Inspection & Architectural Control."""

from __future__ import annotations

from pathlib import Path
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static, TabbedContent, TabPane, Tree

from telos.models import EdgeRelation, KanbanState
from telos.service import TelosService


class TelosTuiApp(App[None]):
    """High-contrast reactive TUI displaying Kanban, Mikado DAG, PERT Gantt, V-Model, and Zwicky views."""

    TITLE = "TELOS // AI-Agent Systems Engineering & Project Harness"
    CSS = """
    Screen {
        background: #0d1117;
        color: #c9d1d9;
    }
    #main-split {
        height: 1fr;
    }
    #left-tree {
        width: 34;
        border-right: solid #30363d;
        padding: 1;
        background: #161b22;
    }
    #center-tabs {
        width: 1fr;
        padding: 1;
    }
    #telemetry-drawer {
        height: 12;
        border-top: solid #30363d;
        background: #161b22;
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
        background: #0d1117;
    }
    """

    BINDINGS = [
        Binding("r", "refresh_state", "Refresh State"),
        Binding("u", "unquarantine_selected", "Unquarantine Task"),
        Binding("1", "switch_tab('tab-kanban')", "Kanban"),
        Binding("2", "switch_tab('tab-pert')", "PERT / Gantt"),
        Binding("3", "switch_tab('tab-vmodel')", "V-Model"),
        Binding("4", "switch_tab('tab-zwicky')", "Zwicky CCA"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, workspace_root: Path | str = ".") -> None:
        super().__init__()
        self.service = TelosService(workspace_root)
        self._selected_task_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-split"):
            yield Tree("Scope & Mikado Hierarchy", id="left-tree")
            with Vertical(id="center-tabs"):
                with TabbedContent(id="tabs"):
                    with TabPane("1: Agile Kanban", id="tab-kanban"):
                        yield DataTable(id="tbl-kanban", cursor_type="row")
                    with TabPane("2: PERT / Monte Carlo Gantt", id="tab-pert"):
                        yield Static(id="pert-summary")
                        yield DataTable(id="tbl-pert", cursor_type="row")
                    with TabPane("3: V-Model Verification Matrix", id="tab-vmodel"):
                        yield DataTable(id="tbl-vmodel", cursor_type="row")
                    with TabPane("4: Zwicky Morphological Box", id="tab-zwicky"):
                        yield DataTable(id="tbl-zwicky", cursor_type="row")
        yield RichLog(id="telemetry-drawer", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh_state()

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "tbl-kanban" and event.row_key.value:
            task_id = str(event.row_key.value)
            self._selected_task_id = task_id
            node = self.service.get_node(task_id)
            attempts = self.service.get_attempts_for_task(task_id)
            receipts = self.service.get_receipts_for_task(task_id)
            log = self.query_one("#telemetry-drawer", RichLog)
            log.clear()
            log.write(
                f"[bold cyan]Task {node.id}[/bold cyan] ({node.state.value.upper()}) | "
                f"Title: {node.title} | Spec: {node.concise_spec or 'N/A'}"
            )
            for a in attempts[-3:]:
                log.write(f"  [yellow]Attempt {a.attempt_id[-6:]}[/yellow] [{a.category.value}]: {a.rationale}")
            for r in receipts[-2:]:
                log.write(
                    f"  [green]Receipt {r.receipt_id[-6:]}[/green] exit={r.exit_code} "
                    f"passed={r.tests_passed} ref={r.verified_git_ref or 'local'}"
                )

    def action_unquarantine_selected(self) -> None:
        if not self._selected_task_id:
            return
        node = self.service.get_node(self._selected_task_id)
        if node.state == KanbanState.QUARANTINED:
            self.service.unquarantine_task(
                node.id, rationale="Unquarantined via TUI operator action", actor="tui-operator"
            )
            self.action_refresh_state()

    def action_refresh_state(self) -> None:
        nodes = self.service.load_all_nodes()
        edges = self.service.load_all_edges()

        tree = self.query_one("#left-tree", Tree)
        tree.clear()
        tree.root.expand()
        for n in nodes.values():
            badge = f"[{n.state.value.upper()}]"
            node_branch = tree.root.add(f"{badge} {n.title} ({n.id[-6:]})")
            prereqs = [
                nodes[e.target_id]
                for e in edges
                if e.relation == EdgeRelation.DEPENDS_ON and e.source_id == n.id and e.target_id in nodes
            ]
            for p in prereqs:
                node_branch.add_leaf(f"requires -> {p.title} [{p.state.value}]")

        tbl_k = self.query_one("#tbl-kanban", DataTable)
        tbl_k.clear(columns=True)
        tbl_k.add_columns("ID", "State", "Kind", "Title", "Fencing Gen", "Strikes", "Est (Min)")
        for n in nodes.values():
            tbl_k.add_row(
                n.id[-8:],
                n.state.value.upper(),
                n.kind.value,
                n.title,
                str(n.fencing_token),
                f"{n.strike_count}/{n.max_strikes}",
                f"{n.estimates.duration_minutes.mean:.1f}m",
                key=n.id,
            )

        cpm_map, mc = self.service.compute_schedule(trials=1000)
        summary = self.query_one("#pert-summary", Static)
        summary.update(
            f"[bold cyan]Deterministic CPM:[/bold cyan] {mc.deterministic_cpm_duration}m   |   "
            f"[bold yellow]Monte Carlo (Merge-Bias Adjusted):[/bold yellow] "
            f"P50={mc.p50_duration}m  P80={mc.p80_duration}m  P95={mc.p95_duration}m   |   "
            f"[bold green]Token P80:[/bold green] {mc.p80_tokens:,.0f} tok"
        )
        tbl_p = self.query_one("#tbl-pert", DataTable)
        tbl_p.clear(columns=True)
        tbl_p.add_columns("ID", "Task", "ES -> EF", "LS -> LF", "Float", "Criticality", "Unicode Gantt Whisker")
        max_ef = max((s.late_finish for s in cpm_map.values()), default=1.0) or 1.0
        for nid, s in cpm_map.items():
            width = 24
            start_pos = int((s.early_start / max_ef) * width)
            end_pos = max(start_pos + 1, int((s.early_finish / max_ef) * width))
            bar_char = "█" if s.is_critical else "▒"
            gantt_bar = ("·" * start_pos) + (bar_char * (end_pos - start_pos)) + ("·" * max(0, width - end_pos))
            ci = mc.criticality_index.get(nid, 0.0)
            tbl_p.add_row(
                nid[-6:],
                s.title,
                f"{s.early_start:.0f} -> {s.early_finish:.0f}m",
                f"{s.late_start:.0f} -> {s.late_finish:.0f}m",
                f"{s.total_float:.1f}m",
                f"{ci * 100:.0f}%",
                f"|{gantt_bar}|",
            )

        tbl_v = self.query_one("#tbl-vmodel", DataTable)
        tbl_v.clear(columns=True)
        tbl_v.add_columns("Left-Wing Spec", "Left State", "Right-Wing Verifier", "Locked Paths", "Receipt")
        for e in edges:
            if e.relation == EdgeRelation.VERIFIES and e.source_id in nodes and e.target_id in nodes:
                r_node = nodes[e.source_id]
                l_node = nodes[e.target_id]
                paths = ", ".join(r_node.contract.locked_paths) if r_node.contract else "None"
                tbl_v.add_row(
                    f"{l_node.title} ({l_node.id[-6:]})",
                    l_node.state.value,
                    f"{r_node.title} ({r_node.id[-6:]})",
                    paths,
                    (l_node.last_verified_receipt_id or "Pending")[-8:],
                )

        tbl_z = self.query_one("#tbl-zwicky", DataTable)
        tbl_z.clear(columns=True)
        tbl_z.add_columns("Box Title", "RankScore", "Synergy", "Unassessed", "Cost", "Configuration")
        for box in self.service.load_all_zwicky_boxes().values():
            candidates = self.service.solve_zwicky_box(box.id, top_k=3)
            for c in candidates:
                cfg_str = ", ".join(f"{k}={v}" for k, v in c.configuration.items())
                tbl_z.add_row(
                    box.title,
                    f"{c.efficiency_rank_score:.2f}",
                    str(c.synergy_score),
                    str(c.unassessed_pairs),
                    f"{c.total_cost:.1f}",
                    cfg_str,
                )

        log = self.query_one("#telemetry-drawer", RichLog)
        log.clear()
        log.write(
            f"[bold green]Telos WAL Sync OK[/bold green] | Nodes: {len(nodes)} | Edges: {len(edges)} | "
            f"Quarantined: {sum(1 for n in nodes.values() if n.state == KanbanState.QUARANTINED)} "
            f"(Select a row in Kanban to inspect attempts/receipts; press 'u' to unquarantine)"
        )


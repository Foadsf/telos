"""Scoped Google Tasks Projection & Human Inbox Adapter (Verification-Gate Safe)."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
import urllib.request

from telos.models import KanbanState, TaskNode
from telos.service import TelosService

_FOOTER_RE = re.compile(
    r"\[telos:id=(?P<id>[0-9A-Z]+)\s*\|\s*state=(?P<state>[a-z_]+)\s*\|\s*pert=(?P<pert>[^\]]+)\]"
)

def format_google_task_notes(node: TaskNode, critical: bool = False) -> str:
    """Serialize human description plus compact machine-parseable Telos metadata envelope."""
    cp_tag = "CP" if critical else "non-CP"
    mean_m = round(node.estimates.duration_minutes.mean, 1)
    std_m = round(node.estimates.duration_minutes.std_dev, 1)
    footer = (
        f"\n---\n"
        f"[telos:id={node.id} | state={node.state.value} | pert={mean_m}m±{std_m}m({cp_tag}) | "
        f"strikes={node.strike_count}/{node.max_strikes}]"
    )
    max_spec_len = 8192 - len(footer) - 10
    body = node.concise_spec[:max_spec_len]
    return f"{body}{footer}"

def parse_google_task_footer(notes: str) -> dict[str, str] | None:
    match = _FOOTER_RE.search(notes or "")
    if not match:
        return None
    return match.groupdict()

class GoogleTasksProjectionAdapter:
    """
    Synchronizes a scoped subset of Telos tasks with Google Tasks REST API v1.
    Guarantees a mobile checkbox on a code task with a verification gate cannot bypass R3 verification:
    external completion requests move gated tasks to READY/VERIFYING rather than DONE.
    """

    BASE_URL = "https://tasks.googleapis.com/tasks/v1"

    def __init__(self, service: TelosService, access_token: str | None = None) -> None:
        self.service = service
        self.access_token = access_token

    def reconcile_remote_task_payload(self, remote_task: dict[str, Any]) -> dict[str, Any]:
        """
        Reconcile a Google Task item dictionary against authoritative local Telos state.
        Works both offline (for unit tests and dry runs) and online with live Google Tasks JSON payloads.
        """
        notes = remote_task.get("notes", "")
        meta = parse_google_task_footer(notes)
        remote_completed = remote_task.get("status") == "completed"
        remote_deleted = bool(remote_task.get("deleted", False))
        remote_title = remote_task.get("title", "Untitled Google Task")

        if meta is None:
            # New human inbox task created in Google Tasks mobile/web app
            created = self.service.create_node(
                title=remote_title,
                concise_spec=notes,
                state=KanbanState.READY,
                labels=["google-inbox"],
            )
            created.google_task_id = remote_task.get("id")
            self.service.save_node(created)
            return {"action": "imported_from_inbox", "task_id": created.id}

        node = self.service.get_node(meta["id"])
        edges = self.service.load_all_edges()
        has_verification_gate = any(
            e.relation.value == "verifies" and e.target_id == node.id for e in edges
        )

        if remote_deleted:
            return {"action": "ignored_remote_delete_protected", "task_id": node.id}

        if remote_completed and node.state != KanbanState.DONE:
            if has_verification_gate:
                # Mobile checkbox CANNOT bypass V-Model verification gate
                return {
                    "action": "blocked_unverified_completion",
                    "task_id": node.id,
                    "state": node.state.value,
                    "reason": "Task requires signed V-Model VerificationReceipt before DONE.",
                }
            self.service.attest_done(
                node.id,
                actor=f"google-tasks:{remote_task.get('id', 'unknown')}",
                rationale="Completed by its human owner in Google Tasks",
            )
            return {"action": "marked_human_task_done", "task_id": node.id}

        return {"action": "in_sync", "task_id": node.id, "state": node.state.value}

    def push_task_to_google(self, tasklist_id: str, node: TaskNode) -> dict[str, Any]:
        if not self.access_token:
            raise RuntimeError("Google OAuth2 access_token is required for live HTTP push.")
        payload = {
            "title": node.title,
            "notes": format_google_task_notes(node),
            "status": "completed" if node.state == KanbanState.DONE else "needsAction",
        }
        url = f"{self.BASE_URL}/lists/{tasklist_id}/tasks"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        node.google_task_id = result.get("id")
        self.service.save_node(node)
        return result

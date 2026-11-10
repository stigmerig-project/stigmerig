"""StigmeRig telemetry exporter for Mermaid sequence diagrams and metrics."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from stigmerig.db import StigmeRigDB


def parse_iso(ts_str: str) -> datetime.datetime:
    """Parse ISO UTC timestamp."""
    try:
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc)


class TelemetryExporter:
    """Exports event sequences and duration metrics from SQLite event logs."""

    def __init__(self, db: StigmeRigDB):
        self.db = db

    def get_events(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch chronological task events from queue.db."""
        query = "SELECT * FROM task_events"
        params: List[Any] = []
        if task_id:
            query += " WHERE task_id = ?"
            params.append(task_id)
        query += " ORDER BY id ASC"

        with self.db.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def generate_mermaid_sequence(self, task_id: Optional[str] = None) -> str:
        """Render a Mermaid sequence diagram visualizing agent handoffs."""
        events = self.get_events(task_id)
        if not events:
            return "sequenceDiagram\n    Note over System: No events recorded\n"

        lines = ["sequenceDiagram", "    autonumber"]

        # Discover participants
        participants: List[str] = ["System"]
        for ev in events:
            agent = ev.get("agent") or "System"
            if agent not in participants:
                participants.append(agent)
            payload_str = ev.get("payload") or "{}"
            try:
                payload = json.loads(payload_str)
                verifier = payload.get("verifier")
                if verifier and verifier not in participants:
                    participants.append(verifier)
            except Exception:
                pass

        actor_map: Dict[str, str] = {}
        used_ids: set[str] = set()

        for p in participants:
            if re.fullmatch(r"[A-Za-z0-9_]+", p):
                safe_id = p
                if safe_id in used_ids:
                    counter = 1
                    while f"{safe_id}_{counter}" in used_ids:
                        counter += 1
                    safe_id = f"{safe_id}_{counter}"
                actor_map[p] = safe_id
                used_ids.add(safe_id)
                lines.append(f"    actor {safe_id}")
            else:
                base = re.sub(r"[^A-Za-z0-9_]", "_", p).strip("_") or "actor"
                safe_id = base
                counter = 1
                while safe_id in used_ids:
                    safe_id = f"{base}_{counter}"
                    counter += 1
                used_ids.add(safe_id)
                actor_map[p] = safe_id
                lines.append(f'    actor {safe_id} as "{p}"')

        for ev in events:
            t_id = ev["task_id"]
            e_type = ev["event_type"]
            agent = ev.get("agent") or "System"
            agent_id = actor_map.get(agent, agent)
            sys_id = actor_map.get("System", "System")
            payload_str = ev.get("payload") or "{}"
            try:
                payload = json.loads(payload_str)
            except Exception:
                payload = {}

            if e_type == "created":
                lines.append(f"    {sys_id}->>{agent_id}: Create {t_id}")
            elif e_type == "claimed":
                lines.append(f"    {agent_id}->>{sys_id}: Claim lease for {t_id}")
            elif e_type == "completed":
                verifier = payload.get("verifier")
                if verifier and verifier != agent:
                    verifier_id = actor_map.get(verifier, verifier)
                    lines.append(f"    {agent_id}->>{verifier_id}: Submit {t_id} for review")
                    lines.append(f"    {verifier_id}->>{sys_id}: Verified {t_id}")
                else:
                    lines.append(f"    {agent_id}->>{sys_id}: Completed {t_id}")
            elif e_type == "blocked":
                reason = payload.get("reason", "unknown")
                lines.append(f"    {agent_id}->>{sys_id}: Report blocked {t_id} ({reason})")
            else:
                lines.append(f"    {agent_id}->>{sys_id}: {e_type} {t_id}")

        return "\n".join(lines) + "\n"

    def compute_duration_metrics(self) -> List[Dict[str, Any]]:
        """Compute execution durations per task."""
        events = self.get_events()
        tasks_map: Dict[str, Dict[str, Optional[datetime.datetime]]] = {}

        for ev in events:
            t_id = ev["task_id"]
            e_type = ev["event_type"]
            ts = parse_iso(ev["timestamp"])

            if t_id not in tasks_map:
                tasks_map[t_id] = {"created": None, "claimed": None, "completed": None}

            if e_type == "created" and not tasks_map[t_id]["created"]:
                tasks_map[t_id]["created"] = ts
            elif e_type == "claimed" and not tasks_map[t_id]["claimed"]:
                tasks_map[t_id]["claimed"] = ts
            elif e_type == "completed":
                tasks_map[t_id]["completed"] = ts

        metrics: List[Dict[str, Any]] = []
        for t_id, data in tasks_map.items():
            t_created = data["created"]
            t_claimed = data["claimed"]
            t_completed = data["completed"]

            wait_sec = (t_claimed - t_created).total_seconds() if t_claimed and t_created else None
            exec_sec = (t_completed - t_claimed).total_seconds() if t_completed and t_claimed else None
            total_sec = (t_completed - t_created).total_seconds() if t_completed and t_created else None

            metrics.append({
                "task_id": t_id,
                "wait_seconds": wait_sec,
                "exec_seconds": exec_sec,
                "total_seconds": total_sec,
            })

        return metrics

    def generate_metrics_markdown(self) -> str:
        """Render task durations in a clean markdown table."""
        metrics = self.compute_duration_metrics()
        lines = [
            "# Task Concurrency & Duration Metrics",
            "",
            "| Task ID | Wait Time (s) | Execution Time (s) | Total Duration (s) |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for m in metrics:
            w = f"{m['wait_seconds']:.1f}" if m['wait_seconds'] is not None else "-"
            e = f"{m['exec_seconds']:.1f}" if m['exec_seconds'] is not None else "-"
            t = f"{m['total_seconds']:.1f}" if m['total_seconds'] is not None else "-"
            lines.append(f"| `{m['task_id']}` | {w} | {e} | {t} |")

        return "\n".join(lines) + "\n"

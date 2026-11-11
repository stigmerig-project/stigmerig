"""StigmeRig telemetry exporter for Mermaid sequence diagrams and metrics."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from stigmerig.db import StigmeRigDB


VERIFY_ONLY_ARCHETYPES: frozenset[str] = frozenset({
    "unit_tester",
    "prose_auditor",
    "policy_gatekeeper",
    "topology_reader",
})

_EVENT_TYPE_MAP: Dict[str, str] = {
    "AGENT CREATED": "created",
    "TASK_CREATED": "created",
    "CREATED": "created",
    "AGENT CLAIMED": "claimed",
    "AGENT WORKING": "claimed",
    "CLAIMED": "claimed",
    "AGENT DONE": "completed",
    "AGENT_DONE": "completed",
    "TASK_COMPLETE": "completed",
    "TASK_COMPLETED": "completed",
    "TASK_COMPLETION": "completed",
    "TASK_DONE": "completed",
    "COMPLETED": "completed",
    "AGENT BLOCKED": "blocked",
    "TASK_BLOCKED": "blocked",
    "BLOCKED": "blocked",
    "AGENT CANCELLED": "cancelled",
    "TASK_CANCELLED": "cancelled",
    "CANCELLED": "cancelled",
    "SELF_EXECUTION_OVERRIDE": "self_execution_override",
}


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
        """Fetch chronological task events from queue.db supporting both Tier A and Tier B schemas."""
        params: List[Any] = []
        where_clause = ""
        if task_id:
            where_clause = " WHERE task_id = ?"
            params.append(task_id)

        with self.db.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(task_events)")
            cols = {r["name"] for r in cur.fetchall()}
            order_col = "id" if "id" in cols else ("event_id" if "event_id" in cols else "rowid")
            query = f"SELECT * FROM task_events{where_clause} ORDER BY {order_col} ASC"
            cur.execute(query, params)
            raw_rows = [dict(r) for r in cur.fetchall()]

        norm_rows: List[Dict[str, Any]] = []
        for r in raw_rows:
            norm = dict(r)
            if "id" not in norm and "event_id" in norm:
                norm["id"] = norm["event_id"]
            if "agent" not in norm and "actor" in norm:
                norm["agent"] = norm["actor"]
            if "payload" not in norm and "message" in norm:
                norm["payload"] = norm["message"]
            if "timestamp" not in norm and "created_at" in norm:
                norm["timestamp"] = norm["created_at"]
            raw_event_type = str(norm.get("event_type") or "").strip()
            norm["raw_event_type"] = raw_event_type
            upper_type = raw_event_type.upper()
            if upper_type in _EVENT_TYPE_MAP:
                norm["event_type"] = _EVENT_TYPE_MAP[upper_type]
            else:
                norm["event_type"] = raw_event_type
            norm_rows.append(norm)
        return norm_rows

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

    def compute_topology_and_dmv_metrics(self) -> Dict[str, Any]:
        """Compute wave topology shapes, verifier overhead ratios, and blocker trap summaries."""
        with self.db.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM tasks")
            all_tasks = [dict(r) for r in cur.fetchall()]

        events = self.get_events()
        durations = self.compute_duration_metrics()
        completed_duration_ids = {
            m["task_id"] for m in durations if m.get("total_seconds") is not None or m.get("exec_seconds") is not None
        }
        completed_event_ids = {
            ev["task_id"] for ev in events if ev.get("event_type") == "completed"
        }

        completed_ts_by_task: Dict[str, datetime.datetime] = {}
        created_ts_by_task: Dict[str, datetime.datetime] = {}
        for ev in events:
            t_id = ev.get("task_id")
            if not t_id:
                continue
            ts = parse_iso(str(ev.get("timestamp") or ""))
            if ev.get("event_type") == "created" and t_id not in created_ts_by_task:
                created_ts_by_task[t_id] = ts
            elif ev.get("event_type") == "completed":
                completed_ts_by_task[t_id] = ts

        referenced_parents = {
            str(t["parent_id"]).strip()
            for t in all_tasks
            if t.get("parent_id") and str(t["parent_id"]).strip()
        }

        def is_epic_record(row: Dict[str, Any]) -> bool:
            rec_type = str(row.get("type") or "").strip().lower()
            if rec_type == "epic":
                return True
            return str(row.get("id") or "") in referenced_parents

        leaf_tasks = [t for t in all_tasks if not is_epic_record(t)]
        total_records = len(all_tasks)
        total_tasks = len(leaf_tasks)

        completed_tasks = sum(
            1
            for t in leaf_tasks
            if str(t.get("status") or "").strip().lower() == "done"
            or t["id"] in completed_duration_ids
            or t["id"] in completed_event_ids
        )

        verify_only_count = sum(
            1
            for t in leaf_tasks
            if str(t.get("archetype") or "").strip() in VERIFY_ONLY_ARCHETYPES
        )
        verify_only_ratio = round(verify_only_count / total_tasks, 4) if total_tasks > 0 else 0.0
        dmv_alert = verify_only_ratio > 0.30

        self_execution_overrides = sum(
            1 for ev in events if ev.get("event_type") == "self_execution_override"
        )
        ceremony_ratio = (
            round((verify_only_count + self_execution_overrides) / total_tasks, 4)
            if total_tasks > 0
            else 0.0
        )

        grouped_by_epic: Dict[str, List[Dict[str, Any]]] = {}
        for t in leaf_tasks:
            parent_id = str(t.get("parent_id") or "").strip()
            if not parent_id:
                continue
            grouped_by_epic.setdefault(parent_id, []).append(t)

        epics_summary: Dict[str, Dict[str, Any]] = {}
        for parent_id, children in sorted(grouped_by_epic.items()):
            intervals: List[Tuple[datetime.datetime, datetime.datetime, str]] = []
            for child in children:
                cid = str(child["id"])
                start_dt = (
                    parse_iso(str(child["created_at"]))
                    if child.get("created_at")
                    else created_ts_by_task.get(cid, datetime.datetime.now(datetime.timezone.utc))
                )
                comp_dt = completed_ts_by_task.get(cid)
                upd_dt = parse_iso(str(child["updated_at"])) if child.get("updated_at") else start_dt
                end_dt = comp_dt if (comp_dt is not None and comp_dt >= start_dt) else upd_dt
                if end_dt < start_dt:
                    end_dt = start_dt
                intervals.append((start_dt, end_dt, cid))

            intervals.sort(key=lambda item: (item[0], item[1], item[2]))

            wave_sizes: List[int] = []
            current_wave_min_start: Optional[datetime.datetime] = None
            current_wave_max_end: Optional[datetime.datetime] = None
            current_wave_count = 0

            for start_dt, end_dt, _ in intervals:
                if current_wave_max_end is None or current_wave_min_start is None:
                    current_wave_min_start = start_dt
                    current_wave_max_end = end_dt
                    current_wave_count = 1
                elif start_dt >= current_wave_max_end and start_dt > current_wave_min_start:
                    wave_sizes.append(current_wave_count)
                    current_wave_min_start = start_dt
                    current_wave_max_end = end_dt
                    current_wave_count = 1
                else:
                    current_wave_count += 1
                    if end_dt > current_wave_max_end:
                        current_wave_max_end = end_dt

            if current_wave_count > 0:
                wave_sizes.append(current_wave_count)

            n_children = len(children)
            phase_depth = len(wave_sizes)
            max_wave_width = max(wave_sizes) if wave_sizes else 0

            if n_children <= 1:
                topology_class = "single"
            elif phase_depth == 1:
                topology_class = "concurrent"
            elif max_wave_width == 1:
                topology_class = "linear"
            else:
                topology_class = "hybrid_dag"

            epics_summary[parent_id] = {
                "total_tasks": n_children,
                "phase_depth": phase_depth,
                "max_wave_width": max_wave_width,
                "topology_class": topology_class,
            }

        reason_counts: Dict[str, int] = {}
        for ev in events:
            if ev.get("event_type") != "blocked":
                continue
            payload_raw = ev.get("payload") or ""
            reason = "unspecified"
            if payload_raw:
                try:
                    parsed = json.loads(payload_raw)
                    if isinstance(parsed, dict):
                        reason = str(parsed.get("reason") or "unspecified").strip() or "unspecified"
                    else:
                        reason = str(parsed).strip() or "unspecified"
                except Exception:
                    reason = str(payload_raw).strip() or "unspecified"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

        traps_list: List[Dict[str, Any]] = []
        traps_path = self.db.rig_dir / "traps.json"
        if traps_path.exists():
            try:
                traps_doc = json.loads(traps_path.read_text(encoding="utf-8"))
                if isinstance(traps_doc, dict) and isinstance(traps_doc.get("traps"), list):
                    traps_list = [x for x in traps_doc["traps"] if isinstance(x, dict)]
            except Exception:
                traps_list = []

        recurring_candidates = [
            t
            for t in traps_list
            if int(t.get("occurrences", 0)) >= 3
            and str(t.get("status") or "").strip().lower() != "dismissed"
        ]

        return {
            "total_tasks": total_tasks,
            "total_records": total_records,
            "completed_tasks": completed_tasks,
            "verify_only_tasks": verify_only_count,
            "verify_only_ratio": verify_only_ratio,
            "dmv_alert": dmv_alert,
            "self_execution_overrides": self_execution_overrides,
            "ceremony_ratio": ceremony_ratio,
            "epics": epics_summary,
            "blocked_reasons": {
                "by_reason": reason_counts,
                "total_blocked_events": sum(reason_counts.values()),
                "total_traps": len(traps_list),
                "recurring_candidates": recurring_candidates,
            },
        }

    def export_metrics_json(self) -> Dict[str, Any]:
        """Export structured duration, governance, topology, and blocker metrics."""
        return {
            "durations": self.compute_duration_metrics(),
            "governance_and_topology": self.compute_topology_and_dmv_metrics(),
        }

    def generate_metrics_markdown(self) -> str:
        """Render task durations and governance summary in markdown."""
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

        gov = self.compute_topology_and_dmv_metrics()
        lines.extend([
            "",
            "## Governance, Topology & Blocker Summary",
            "",
            f"- **Leaf Tasks**: {gov['completed_tasks']} / {gov['total_tasks']} completed ({gov['total_records']} total records)",
            f"- **Verify-Only Ratio (`verify_only_ratio`)**: {gov['verify_only_ratio']:.4f} (`dmv_alert`: `{gov['dmv_alert']}`)",
            f"- **Ceremony Ratio (`ceremony_ratio`)**: {gov['ceremony_ratio']:.4f} (overrides: {gov['self_execution_overrides']})",
            f"- **Recorded Traps**: {gov['blocked_reasons']['total_traps']} total, {len(gov['blocked_reasons']['recurring_candidates'])} recurring candidates",
        ])

        if gov["epics"]:
            lines.extend([
                "",
                "### Epic Wave Shapes",
                "",
                "| Epic ID | Leaf Tasks | Phase Depth | Max Wave Width | Topology Class |",
                "| :--- | :--- | :--- | :--- | :--- |",
            ])
            for epic_id, info in gov["epics"].items():
                lines.append(
                    f"| `{epic_id}` | {info['total_tasks']} | {info['phase_depth']} | "
                    f"{info['max_wave_width']} | `{info['topology_class']}` |"
                )

        recurring = gov["blocked_reasons"]["recurring_candidates"]
        if recurring:
            lines.extend([
                "",
                "### Recurring Blocker Traps",
                "",
                "| Trap ID | Occurrences | Trigger Pattern | Reason |",
                "| :--- | :--- | :--- | :--- |",
            ])
            for trap in recurring:
                lines.append(
                    f"| `{trap.get('id', '-')}` | {trap.get('occurrences', 0)} | "
                    f"`{trap.get('trigger_pattern', '')}` | {trap.get('reason', '')} |"
                )

        return "\n".join(lines) + "\n"

"""StigmeRig telemetry exporter for Mermaid sequence diagrams and metrics."""

from __future__ import annotations

import datetime
import inspect
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from stigmerig.db import StigmeRigDB, _escape_md_cell


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

_COMPLETED_TASK_STATUSES: frozenset[str] = frozenset({
    "done",
    "completed",
    "task_done",
    "task_complete",
    "task_completed",
    "task_completion",
    "agent done",
    "agent_done",
})


def parse_iso(ts_str: str) -> datetime.datetime:
    """Parse ISO UTC timestamp."""
    try:
        s = str(ts_str).strip()
        dt = datetime.datetime.fromisoformat(f"{s[:-1]}+00:00" if s.endswith(("Z", "z")) else s)
    except (ValueError, TypeError):
        return datetime.datetime.now(datetime.timezone.utc)
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None else dt


def _try_parse_iso(ts_str: Any) -> Optional[datetime.datetime]:
    try:
        s = ts_str.strip() if isinstance(ts_str, str) else ""
        dt = datetime.datetime.fromisoformat(f"{s[:-1]}+00:00" if s.endswith(("Z", "z")) else s) if s else None
    except (ValueError, TypeError):
        return None
    return None if dt is None else (dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None else dt)


def _parse_payload_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sanitize_mermaid_text(value: Any) -> str:
    return str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace('"', "'").strip()


def _escape_md_code(value: Any) -> str:
    return _escape_md_cell(value).replace("`", "'")


def _has_force_claimed_marker(raw: Any) -> bool:
    if isinstance(raw, dict):
        vals: Tuple[Any, ...] = (raw.get("reason"), raw.get("message"))
    elif isinstance(raw, str) and raw.strip():
        try:
            p = json.loads(raw)
            vals = (p.get("reason"), p.get("message")) if isinstance(p, dict) else ((p,) if isinstance(p, str) else ())
        except json.JSONDecodeError:
            vals = (raw,)
    else:
        return False
    return any(isinstance(v, str) and "Force-claimed" in v for v in vals)


class TelemetryExporter:
    """Exports event sequences and duration metrics from SQLite event logs."""

    def __init__(self, db: StigmeRigDB):
        self.db = db

    def get_events(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch chronological task events from queue.db supporting both Tier A and Tier B schemas."""
        if task_id is not None:
            task_id = str(task_id).strip()
            if not task_id:
                return []
        if not self.db.queue_db_path.is_file():
            return []
        params: List[Any] = []
        where_clause = ""
        if task_id is not None:
            where_clause = " WHERE TRIM(task_id, ' \t\n\r\f\v') = ?"
            params.append(task_id)

        with self.db.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(task_events)")
            cols = {r["name"] for r in cur.fetchall()}
            if not cols or "task_id" not in cols:
                return []
            order_col = "COALESCE(id, event_id)" if {"id", "event_id"} <= cols else ("id" if "id" in cols else ("event_id" if "event_id" in cols else "rowid"))
            query = f"SELECT * FROM task_events{where_clause} ORDER BY {order_col} ASC"
            cur.execute(query, params)
            raw_rows = [dict(r) for r in cur.fetchall()]

        norm_rows: List[Dict[str, Any]] = []
        for r in raw_rows:
            clean_tid = str(r["task_id"]).strip() if r.get("task_id") is not None else ""
            if not clean_tid:
                continue
            norm = {**r, "task_id": clean_tid}
            if norm.get("id") is None and "event_id" in norm:
                norm["id"] = norm["event_id"]
            for b_col, a_col in (("agent", "actor"), ("payload", "message"), ("timestamp", "created_at")):
                b_val, a_val = norm.get(b_col), norm.get(a_col)
                if (b_val is None or (isinstance(b_val, str) and not b_val.strip())) and ((a_val is not None and str(a_val).strip()) or (b_col not in norm and a_col in norm)):
                    norm[b_col] = a_val
            norm.setdefault("timestamp", "")
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
            agent = str(ev.get("agent") or "").strip() or "System"
            if agent not in participants:
                participants.append(agent)
            raw_verifier = _parse_payload_dict(ev.get("payload")).get("verifier")
            verifier = raw_verifier.strip() if isinstance(raw_verifier, str) else ""
            if verifier and verifier not in participants:
                participants.append(verifier)

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
                lines.append(f'    actor {safe_id} as "{_sanitize_mermaid_text(p)}"')

        for ev in events:
            t_id = ev["task_id"]
            e_type = ev["event_type"]
            agent = str(ev.get("agent") or "").strip() or "System"
            agent_id = actor_map.get(agent, agent)
            sys_id = actor_map.get("System", "System")
            payload = _parse_payload_dict(ev.get("payload"))

            if e_type == "created":
                lines.append(f"    {sys_id}->>{agent_id}: Create {t_id}")
            elif e_type == "claimed":
                lines.append(f"    {agent_id}->>{sys_id}: Claim lease for {t_id}")
            elif e_type == "completed":
                raw_verifier = payload.get("verifier")
                verifier = raw_verifier.strip() if isinstance(raw_verifier, str) else ""
                if verifier and verifier != agent:
                    verifier_id = actor_map.get(verifier, verifier)
                    lines.append(f"    {agent_id}->>{verifier_id}: Submit {t_id} for review")
                    lines.append(f"    {verifier_id}->>{sys_id}: Verified {t_id}")
                else:
                    lines.append(f"    {agent_id}->>{sys_id}: Completed {t_id}")
            elif e_type == "blocked":
                raw_reason = payload.get("reason")
                reason = (_sanitize_mermaid_text(raw_reason) if raw_reason is not None else "") or "unknown"
                lines.append(f"    {agent_id}->>{sys_id}: Report blocked {t_id} ({reason})")
            else:
                lines.append(f"    {agent_id}->>{sys_id}: {e_type} {t_id}")

        return "\n".join(lines) + "\n"

    def compute_duration_metrics(self, events: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Compute execution durations per task."""
        events = self.get_events() if events is None else events
        tasks_map: Dict[str, Dict[str, Optional[datetime.datetime]]] = {}

        for ev in events:
            t_id = str(ev.get("task_id") or "").strip()
            if not t_id:
                continue
            e_type = ev["event_type"]
            ts = _try_parse_iso(ev.get("timestamp"))

            if t_id not in tasks_map:
                tasks_map[t_id] = {"created": None, "claimed": None, "completed": None}
            if ts is None:
                continue

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

    def compute_topology_and_dmv_metrics(self, events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Compute wave topology shapes, verifier overhead ratios, and blocker trap summaries."""
        tasks_by_id: Dict[str, Dict[str, Any]] = {}
        if self.db.queue_db_path.is_file():
            with self.db.get_queue_conn() as conn:
                raw_tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks")] if conn.execute("PRAGMA table_info(tasks)").fetchall() else []
            for r in raw_tasks:
                tid = r.get("id") if r.get("id") is not None and str(r.get("id")).strip() else r.get("task_id")
                clean_tid = str(tid).strip() if tid is not None else ""
                if not clean_tid:
                    continue
                if clean_tid in tasks_by_id:
                    merged = tasks_by_id[clean_tid]
                    for k, v in r.items():
                        if v is not None and (not isinstance(v, str) or v.strip()):
                            merged[k] = v
                    merged["id"] = clean_tid
                else:
                    tasks_by_id[clean_tid] = {**r, "id": clean_tid}
        all_tasks: List[Dict[str, Any]] = list(tasks_by_id.values())

        events = self.get_events() if events is None else events
        completed_event_ids: set[str] = set()
        completed_ts_by_task: Dict[str, datetime.datetime] = {}
        created_ts_by_task: Dict[str, datetime.datetime] = {}
        claimed_ts_by_task: Dict[str, List[datetime.datetime]] = {}
        for ev in events:
            t_id = str(ev.get("task_id") or "").strip()
            if t_id and ev.get("event_type") == "completed":
                completed_event_ids.add(t_id)
            ts = _try_parse_iso(ev.get("timestamp"))
            if not t_id or ts is None:
                continue
            if ev.get("event_type") == "created" and t_id not in created_ts_by_task:
                created_ts_by_task[t_id] = ts
            elif ev.get("event_type") == "claimed":
                claimed_ts_by_task.setdefault(t_id, []).append(ts)
            elif ev.get("event_type") == "completed":
                completed_ts_by_task[t_id] = ts
        completed_duration_ids = {tid for tid in completed_ts_by_task if tid in created_ts_by_task or tid in claimed_ts_by_task}

        referenced_parents = {
            str(t["parent_id"]).strip()
            for t in all_tasks
            if t.get("parent_id") and str(t["parent_id"]).strip() and str(t["parent_id"]).strip() != str(t.get("id") or "").strip()
        }

        def is_epic_record(row: Dict[str, Any]) -> bool:
            rec_type = str(row.get("type") or "").strip().lower()
            if rec_type in ("epic", "stigmerig_coordinator") or str(row.get("archetype") or "").strip().lower() in ("epic", "stigmerig_coordinator"):
                return True
            return str(row.get("id") or "").strip() in referenced_parents

        leaf_tasks = [t for t in all_tasks if not is_epic_record(t)]
        total_records = len(all_tasks)
        total_tasks = len(leaf_tasks)

        completed_tasks = sum(
            1
            for t in leaf_tasks
            if " ".join(str(t.get("status") or "").split()).lower() in _COMPLETED_TASK_STATUSES
            or t["id"] in completed_duration_ids
            or t["id"] in completed_event_ids
        )

        verify_only_arch = {a.strip().lower() for a in VERIFY_ONLY_ARCHETYPES}
        verify_only_count = sum(
            1
            for t in leaf_tasks
            if str(t.get("archetype") or "").strip().lower() in verify_only_arch
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

        leaf_ids = {t["id"] for t in leaf_tasks}
        claimants_by_task: Dict[str, set[str]] = {}
        blocked_leaf_ids: set[str] = set()
        force_claimed_leaf_ids: set[str] = set()
        for ev in events:
            tid = str(ev.get("task_id") or "").strip()
            if not tid or tid not in leaf_ids:
                continue
            ev_type = ev.get("event_type")
            if ev_type == "claimed":
                actor = str(ev.get("agent") or "").strip().lower()
                if actor:
                    claimants_by_task.setdefault(tid, set()).add(actor)
                if _has_force_claimed_marker(ev.get("payload")) or _has_force_claimed_marker(ev.get("message")):
                    force_claimed_leaf_ids.add(tid)
            elif ev_type == "blocked":
                blocked_leaf_ids.add(tid)

        rework_task_ids = (
            {tid for tid, actors in claimants_by_task.items() if len(actors) >= 2}
            | blocked_leaf_ids
            | force_claimed_leaf_ids
        )
        rework_tasks = len(rework_task_ids)
        rework_rate = round(rework_tasks / total_tasks, 4) if total_tasks > 0 else 0.0

        grouped_by_epic: Dict[str, List[Dict[str, Any]]] = {}
        for t in leaf_tasks:
            parent_id = str(t.get("parent_id") or "").strip()
            if not parent_id or parent_id == str(t.get("id") or "").strip():
                continue
            grouped_by_epic.setdefault(parent_id, []).append(t)

        epics_summary: Dict[str, Dict[str, Any]] = {}
        for parent_id, children in sorted(grouped_by_epic.items()):
            intervals: List[Tuple[datetime.datetime, datetime.datetime, str]] = []
            for child in children:
                cid = str(child["id"])
                created_dt = _try_parse_iso(child.get("created_at")) or created_ts_by_task.get(cid)
                claims = [ts for ts in claimed_ts_by_task.get(cid, ()) if created_dt is None or ts >= created_dt]
                start_dt = min(claims) if claims else (created_dt or datetime.datetime.now(datetime.timezone.utc))
                comp_dt = completed_ts_by_task.get(cid)
                upd_dt = _try_parse_iso(child.get("updated_at")) or start_dt
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
            p_raw = ev.get("payload")
            payload_raw = (ev.get("message") if p_raw is None or p_raw == {} or (isinstance(p_raw, str) and "".join(p_raw.split()) in ("", "{}")) else p_raw) or ""
            reason = "unspecified"
            if payload_raw:
                try:
                    parsed = payload_raw if isinstance(payload_raw, dict) else json.loads(payload_raw)
                    raw_r = parsed.get("reason") if isinstance(parsed, dict) else parsed
                    reason = raw_r.strip() if isinstance(raw_r, str) and raw_r.strip() else "unspecified"
                except (json.JSONDecodeError, TypeError):
                    reason = payload_raw.strip() if isinstance(payload_raw, str) and payload_raw.strip() else "unspecified"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

        traps_list: List[Dict[str, Any]] = []
        traps_path = self.db.rig_dir / "traps.json"
        if traps_path.exists():
            try:
                traps_doc = json.loads(traps_path.read_text(encoding="utf-8"))
                if isinstance(traps_doc, dict) and isinstance(traps_doc.get("traps"), list):
                    traps_list = [x for x in traps_doc["traps"] if isinstance(x, dict)]
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                traps_list = []

        recurring_candidates = [
            t
            for t in traps_list
            if int(t.get("occurrences") or 0) >= 3
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
            "rework_tasks": rework_tasks,
            "rework_rate": rework_rate,
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
        events = self.get_events()
        return {
            "durations": self.compute_duration_metrics(events),
            "governance_and_topology": self.compute_topology_and_dmv_metrics(events),
        }

    def generate_metrics_markdown(self, events: Optional[List[Dict[str, Any]]] = None) -> str:
        """Render task durations and governance summary in markdown."""
        shared_events = self.get_events() if events is None else events
        metrics = (
            self.compute_duration_metrics(shared_events)
            if inspect.signature(self.compute_duration_metrics).parameters
            else self.compute_duration_metrics()
        )
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
            lines.append(f"| `{_escape_md_code(m['task_id'])}` | {w} | {e} | {t} |")

        gov = (
            self.compute_topology_and_dmv_metrics(shared_events)
            if inspect.signature(self.compute_topology_and_dmv_metrics).parameters
            else self.compute_topology_and_dmv_metrics()
        )
        lines.extend([
            "",
            "## Governance, Topology & Blocker Summary",
            "",
            f"- **Leaf Tasks**: {gov['completed_tasks']} / {gov['total_tasks']} completed ({gov['total_records']} total records)",
            f"- **Verify-Only Ratio (`verify_only_ratio`)**: {gov['verify_only_ratio']:.4f} (`dmv_alert`: `{gov['dmv_alert']}`)",
            f"- **Ceremony Ratio (`ceremony_ratio`)**: {gov['ceremony_ratio']:.4f} (overrides: {gov['self_execution_overrides']})",
            f"- **Rework Rate (`rework_rate`)**: {gov['rework_rate']:.4f} ({gov['rework_tasks']} / {gov['total_tasks']} leaf tasks)",
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
                    f"| `{_escape_md_code(epic_id)}` | {info['total_tasks']} | {info['phase_depth']} | "
                    f"{info['max_wave_width']} | `{_escape_md_code(info['topology_class'])}` |"
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
                    f"| `{_escape_md_cell(trap.get('id') or '-')}` | {int(trap.get('occurrences') or 0)} | "
                    f"`{_escape_md_cell(trap.get('trigger_pattern') or '')}` | {_escape_md_cell(trap.get('reason') or '')} |"
                )

        return "\n".join(lines) + "\n"

"""StigmeRig local-first database and state engine."""

from __future__ import annotations

from contextlib import contextmanager
import datetime
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


GENESIS_HASH: str = "0" * 64
WORKFLOW_RAILS: Tuple[str, ...] = ("diff_synthesizer", "unit_tester", "policy_gatekeeper")
_TRAP_STOP_WORDS: frozenset[str] = frozenset({
    "with", "from", "that", "this", "have", "been", "were", "when", "what",
    "where", "which", "while", "into", "onto", "upon", "over", "under",
    "after", "before", "because", "during", "through", "about", "against",
    "between", "without", "within", "cannot", "could", "should", "would",
    "task", "failed", "blocked", "error", "issue", "problem", "unable",
})


def _normalize_blocker_signature(reason: str) -> str:
    """Extract deterministic keyword signature (>= 4 chars excluding stop words) from blocker reason."""
    clean = (reason or "").strip().lower()
    raw_tokens = re.findall(r"[a-z0-9_]+", clean)
    keywords = list(
        dict.fromkeys(t for t in raw_tokens if len(t) >= 4 and t not in _TRAP_STOP_WORDS)
    )
    if not keywords:
        keywords = list(dict.fromkeys(t for t in raw_tokens if len(t) >= 2))
    return " ".join(keywords) if keywords else clean

DEFAULT_AGENTS_TEMPLATE = """# AGENTS

This document defines autonomous agent coordination standards and stigmergic invariants.

## Autonomous Operation Invariants

1. **Local-First Stigmergy**: Agents coordinate indirectly through `.stigmerig` task state and filesystem artifacts.
2. **Worktree Isolation**: Implementers isolate execution branches in dedicated git worktrees.
3. **Anti-Self-Approval**: Verification must be performed by an independent verifier before completing tasks.
4. **Append-Only WORM Audit**: All state transitions and completion records are hash-chained in the audit ledger.

## Workflow

1. Query available tasks via `stigmerig task list --status todo`.
2. Claim a task with lease TTL via `stigmerig task claim --id <id> --agent <agent>`.
3. Complete implementation and submit for verification via `stigmerig task done --id <id> --agent <agent> --verifier <verifier>`.
"""

DEFAULT_CONTEXT_TEMPLATE = """# Project Context

## Overview
Active multi-agent coordination environment managed by StigmeRig.

## Current State
- Coordination Engine: Active
- Task State: `.stigmerig/`
"""

DEFAULT_RIG_JSON_TEMPLATE = """{
  "version": "0.1.0",
  "icm_enabled": true
}
"""


def derive_seat(agent: Optional[str] = None) -> str:
    """Derive seat attribution from environment or agent identifier."""
    env_seat = os.environ.get("STIGMERIG_SEAT") or os.environ.get("AGENT_SEAT") or os.environ.get("SEAT")
    if env_seat:
        return env_seat.strip()
    if agent:
        for sep in ("/", "@", ":"):
            if sep in agent:
                part = agent.split(sep)[0].strip()
                if part:
                    return part
        return agent.strip()
    return "default"


def _is_self_approval(actor: Optional[str], verifier: Optional[str]) -> bool:
    """Check if verifier matches actor identity or base seat/agent components."""
    if not actor or not verifier:
        return False
    actor_str = str(actor).strip()
    verifier_str = str(verifier).strip()
    if not actor_str or not verifier_str:
        return False

    if actor_str.lower() == verifier_str.lower():
        return True

    actor_parts = set()
    verifier_parts = set()
    for sep in ("/", "@", ":"):
        if sep in actor_str:
            actor_parts.update(p.strip().lower() for p in actor_str.split(sep) if p.strip())
        if sep in verifier_str:
            verifier_parts.update(p.strip().lower() for p in verifier_str.split(sep) if p.strip())

    if actor_parts and verifier_parts:
        if actor_parts & verifier_parts:
            return True
    elif actor_parts and verifier_str.lower() in actor_parts:
        return True
    elif verifier_parts and actor_str.lower() in verifier_parts:
        return True

    return False


def utc_now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_rig_dir(override: Optional[Path | str] = None) -> Path:
    """Resolve the StigmeRig state directory.

    Order of precedence:
    1. Explicit override argument.
    2. STIGMERIG_ROOT environment variable.
    3. .stigmerig in the current working directory.
    """
    if override:
        return Path(override).resolve()
    env_root = os.environ.get("STIGMERIG_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return (Path.cwd() / ".stigmerig").resolve()


class StigmeRigDB:
    """Manages SQLite WAL queue, WORM audit chain, and markdown task files."""

    def __init__(self, rig_dir: Optional[Path | str] = None):
        self.rig_dir = resolve_rig_dir(rig_dir)
        self.queue_db_path = self.rig_dir / "queue.db"
        self.audit_db_path = self.rig_dir / "audit.db"
        self.tasks_dir = self.rig_dir / "tasks"
        self.board_path = self.rig_dir / "Task_Board.md"

    def init_rig(self, scaffold_icm: bool = True) -> Optional[str]:
        """Initialize directory layout, SQLite databases with WAL pragmas, and ICM scaffolding."""
        self.rig_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        with self.get_queue_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'todo',
                    priority INTEGER DEFAULT 100,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_by TEXT,
                    lease_until TEXT,
                    version INTEGER DEFAULT 0,
                    context_path TEXT,
                    parent_id TEXT,
                    tags TEXT,
                    author TEXT,
                    archetype TEXT DEFAULT 'diff_synthesizer'
                );
            """)
            # Migration check for existing databases
            cur = conn.cursor()
            cols = [c[1] for c in cur.execute("PRAGMA table_info(tasks)").fetchall()]
            if "archetype" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN archetype TEXT DEFAULT 'diff_synthesizer';")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    payload TEXT,
                    timestamp TEXT NOT NULL
                );
            """)
            conn.commit()

        with self.get_audit_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS prevent_audit_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit_log entries are append-only and cannot be deleted');
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS prevent_audit_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit_log entries are append-only and cannot be updated');
                END;
            """)
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS verify_hash_chain
                BEFORE INSERT ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'Hash chain broken: previous_hash does not match the latest entry_hash')
                    WHERE NEW.previous_hash IS NOT COALESCE(
                        (SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1),
                        '{GENESIS_HASH}'
                    );
                END;
            """)
            conn.commit()

        if not self.board_path.exists():
            self.sync_board()

        hint: Optional[str] = None
        if scaffold_icm:
            hint = self.scaffold_icm()

        return hint

    def scaffold_icm(self) -> Optional[str]:
        """Scaffold ICM files (AGENTS.md, CONTEXT.md, rig.json) without overwriting existing files."""
        parent_dir = self.rig_dir.parent
        rig_json = self.rig_dir / "rig.json"
        if not rig_json.exists():
            rig_json.write_text(DEFAULT_RIG_JSON_TEMPLATE, encoding="utf-8")

        agents_file = parent_dir / "AGENTS.md"
        sidecar_file = self.rig_dir / "AGENTS.stigmerig.md"
        hint: Optional[str] = None

        if not agents_file.exists():
            agents_file.write_text(DEFAULT_AGENTS_TEMPLATE, encoding="utf-8")
        else:
            if not sidecar_file.exists():
                sidecar_file.write_text(DEFAULT_AGENTS_TEMPLATE, encoding="utf-8")
            hint = f"AGENTS.md already exists; wrote sidecar instructions to .stigmerig/{sidecar_file.name} (include it in your AGENTS.md)"

        context_file = parent_dir / "CONTEXT.md"
        if not context_file.exists():
            context_file.write_text(DEFAULT_CONTEXT_TEMPLATE, encoding="utf-8")

        return hint

    @contextmanager
    def get_queue_conn(self):
        """Create a connection to queue.db with WAL pragmas, closing on context exit."""
        conn = sqlite3.connect(str(self.queue_db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def get_audit_conn(self):
        """Create a connection to audit.db with WAL pragmas, closing on context exit."""
        conn = sqlite3.connect(str(self.audit_db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_task_file(self, task_id: str) -> Path:
        """Resolve task markdown file supporting both flat tasks/<ID>.md and nested tasks/<ID>/context.md."""
        nested = self.tasks_dir / task_id / "context.md"
        if nested.exists():
            return nested
        flat = self.tasks_dir / f"{task_id}.md"
        if flat.exists():
            return flat
        return nested if (self.tasks_dir / task_id).is_dir() else flat

    def create_task(
        self,
        task_id: str,
        title: str,
        parent_id: Optional[str] = None,
        tags: Optional[str] = None,
        author: Optional[str] = None,
        archetype: str = "diff_synthesizer",
        priority: int = 100,
    ) -> Dict[str, Any]:
        """Create a new task record in SQLite and write its markdown scaffold."""
        now = utc_now_iso()
        task_file = self.get_task_file(task_id)
        context_path = str(task_file.relative_to(self.rig_dir.parent))

        with self.get_queue_conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, status, priority, created_at, updated_at,
                    claimed_by, lease_until, version, context_path,
                    parent_id, tags, author, archetype
                ) VALUES (?, ?, 'todo', ?, ?, ?, NULL, NULL, 0, ?, ?, ?, ?, ?)
                """,
                (task_id, title, priority, now, now, context_path, parent_id, tags, author, archetype),
            )
            conn.execute(
                """
                INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
                VALUES (?, 'created', ?, ?, ?)
                """,
                (task_id, author or "system", json.dumps({"title": title, "archetype": archetype}), now),
            )
            conn.commit()

        if not task_file.exists():
            scaffold = f"""# {task_id}: {title}

- **Parent**: {parent_id or "None"}
- **Archetype**: `{archetype}`
- **Tags**: {tags or "None"}
- **Author**: {author or "unknown"}
- **Created**: {now}

## Description

Add task details and acceptance criteria here.
"""
            task_file.write_text(scaffold, encoding="utf-8")

        self.log_audit(
            task_id=task_id,
            action="task_created",
            agent=author or "system",
            verification_status="unverified",
            payload=json.dumps({"title": title, "parent_id": parent_id}),
        )
        self.sync_board()
        return self.get_task(task_id) or {}

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single task by ID."""
        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)

    def list_tasks(
        self,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List tasks matching optional filters."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if parent_id:
            query += " AND parent_id = ?"
            params.append(parent_id)
        query += " ORDER BY priority ASC, created_at ASC"

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def claim_task(
        self,
        task_id: str,
        agent: str,
        ttl_seconds: int = 1800,
    ) -> bool:
        """Atomic Compare-And-Swap task lease claiming.

        A claim succeeds if:
        1. Task status is 'todo' or 'working'.
        2. Task is either unclaimed, expired, or already claimed by the same agent.
        3. The version in the DB matches the in-memory read version.
        """
        task = self.get_task(task_id)
        if not task:
            return False

        status = task.get("status")
        if status not in ("todo", "working"):
            return False

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_str = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        lease_until_dt = now_dt + datetime.timedelta(seconds=ttl_seconds)
        lease_until_str = lease_until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        current_claimant = task.get("claimed_by")
        current_lease = task.get("lease_until")
        version = task.get("version", 0)

        if status == "working":
            can_claim = (current_claimant == agent) or (current_lease is None or current_lease < now_str)
        else:  # status == "todo"
            can_claim = (
                current_claimant is None
                or current_claimant == agent
                or (current_lease is None or current_lease < now_str)
            )

        if not can_claim:
            return False

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE tasks
                SET claimed_by = ?,
                    lease_until = ?,
                    status = 'working',
                    version = version + 1,
                    updated_at = ?
                WHERE id = ?
                  AND version = ?
                  AND status IN ('todo', 'working')
                  AND (
                      claimed_by = ?
                      OR lease_until IS NULL
                      OR lease_until < ?
                      OR (status = 'todo' AND claimed_by IS NULL)
                  )
                """,
                (agent, lease_until_str, now_str, task_id, version, agent, now_str),
            )
            success = cur.rowcount == 1
            if success:
                conn.execute(
                    """
                    INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
                    VALUES (?, 'claimed', ?, ?, ?)
                    """,
                    (task_id, agent, json.dumps({"lease_until": lease_until_str}), now_str),
                )
                conn.commit()

        if success:
            self.log_audit(
                task_id=task_id,
                action="task_claimed",
                agent=agent,
                verification_status="in_progress",
                payload=json.dumps({"lease_until": lease_until_str}),
            )
            self.sync_board()
        return success

    def complete_task(
        self,
        task_id: str,
        agent: str,
        verifier: Optional[str] = None,
        allow_self_approval: bool = False,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Mark task as done enforcing anti-self-approval when required."""
        task = self.get_task(task_id)
        if not task:
            return False, f"Task {task_id} not found"

        if task["status"] == "done":
            return True, f"Task {task_id} already completed"

        implementer = task.get("claimed_by") or agent

        # Anti-self-approval rule
        if not allow_self_approval:
            if verifier is None or not str(verifier).strip():
                raise PermissionError("Independent --verifier is required unless --allow-self-approval is set.")
            clean_verifier = str(verifier).strip()
            if _is_self_approval(agent, clean_verifier) or _is_self_approval(task.get("claimed_by"), clean_verifier):
                raise PermissionError(
                    f"Self-approval forbidden: verifier '{verifier}' matches agent '{agent}' or claimant '{task.get('claimed_by')}'."
                )

        now = utc_now_iso()
        seat = derive_seat(agent)
        event_payload = {
            "verifier": verifier,
            "implementer": implementer,
            "seat": seat,
            "session_id": session_id,
        }
        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE tasks
                SET status = 'done',
                    lease_until = NULL,
                    version = version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
                VALUES (?, 'completed', ?, ?, ?)
                """,
                (
                    task_id,
                    agent,
                    json.dumps(event_payload),
                    now,
                ),
            )
            conn.commit()

        self.log_audit(
            task_id=task_id,
            action="task_completed",
            agent=agent,
            verification_status="verified" if verifier else "unverified",
            payload=json.dumps(event_payload),
        )
        self.sync_board()
        return True, "Task completed successfully"

    def get_next_workflow_step(self, task_id: str) -> Dict[str, Any]:
        """Compute next workflow step under parent task or epic enforcing workflow rails."""
        task = self.get_task(task_id)
        if not task:
            return {}

        parent_id = task.get("parent_id")
        if not parent_id:
            return {}

        current_arch = (task.get("archetype") or "").strip()

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM tasks WHERE parent_id = ? AND id != ?",
                (parent_id, task_id),
            )
            rows = [dict(r) for r in cur.fetchall()]

        todo_siblings = [r for r in rows if r.get("status") == "todo"]
        if not todo_siblings:
            return {}

        if current_arch in WORKFLOW_RAILS:
            idx = WORKFLOW_RAILS.index(current_arch)
            preferred = list(WORKFLOW_RAILS[idx + 1:])
        else:
            preferred = list(WORKFLOW_RAILS)

        selected: Optional[Dict[str, Any]] = None
        for target_arch in preferred:
            matching = [s for s in todo_siblings if s.get("archetype") == target_arch]
            if matching:
                matching.sort(key=lambda s: (int(s.get("priority") if s.get("priority") is not None else 100), s.get("created_at", "")))
                selected = matching[0]
                break

        if not selected:
            todo_siblings.sort(key=lambda s: (int(s.get("priority") if s.get("priority") is not None else 100), s.get("created_at", "")))
            selected = todo_siblings[0]

        return {
            "task_id": selected["id"],
            "title": selected["title"],
            "archetype": selected.get("archetype"),
            "priority": selected.get("priority", 100),
            "parent_id": parent_id,
        }

    def fail_task(self, task_id: str, agent: str, reason: str) -> bool:
        """Mark task as blocked or failed."""
        now = utc_now_iso()
        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE tasks
                SET status = 'blocked',
                    lease_until = NULL,
                    version = version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, task_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Task '{task_id}' not found.")
            conn.execute(
                """
                INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
                VALUES (?, 'blocked', ?, ?, ?)
                """,
                (task_id, agent, json.dumps({"reason": reason}), now),
            )
            conn.commit()

        self.log_audit(
            task_id=task_id,
            action="task_blocked",
            agent=agent,
            verification_status="failed",
            payload=json.dumps({"reason": reason}),
        )
        self.sync_board()
        try:
            self._record_blocker_trap(task_id=task_id, agent=agent, reason=reason)
        except Exception:
            pass
        return True

    def _record_blocker_trap(
        self,
        task_id: str,
        agent: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        """Persist deduplicated blocker trap candidates into traps.json (failure-soft)."""
        clean_reason = (reason or "").strip() or "unspecified blocker"
        trigger_pattern = _normalize_blocker_signature(clean_reason)
        traps_path = self.rig_dir / "traps.json"
        now = utc_now_iso()
        trace_entry = f"Task {task_id} ({agent})"

        doc: Dict[str, Any] = {"version": "1.0.0", "traps": []}
        if traps_path.exists():
            try:
                loaded = json.loads(traps_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("traps"), list):
                    doc = loaded
                    doc.setdefault("version", "1.0.0")
            except Exception:
                doc = {"version": "1.0.0", "traps": []}

        traps: List[Dict[str, Any]] = doc["traps"]
        matched: Optional[Dict[str, Any]] = None
        norm_title = clean_reason.lower()

        for entry in traps:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "").strip().lower() == "dismissed":
                continue
            existing_pattern = str(entry.get("trigger_pattern") or "").strip().lower()
            existing_title = str(entry.get("title") or entry.get("reason") or "").strip().lower()
            if (
                (trigger_pattern and existing_pattern == trigger_pattern)
                or existing_title == norm_title
                or (trigger_pattern and _normalize_blocker_signature(existing_title) == trigger_pattern)
            ):
                matched = entry
                break

        if matched is not None:
            occurrences = int(matched.get("occurrences", 0)) + 1
            matched["occurrences"] = occurrences
            matched["last_seen_at"] = now
            traces = [str(x) for x in (matched.get("source_traces") or [])]
            traces.append(trace_entry)
            matched["source_traces"] = traces[-10:]
            matched["promotion_candidate"] = occurrences >= 3
            result = matched
        else:
            max_num = 0
            for entry in traps:
                if isinstance(entry, dict):
                    m = re.match(r"^TRAP-(\d+)$", str(entry.get("id") or ""))
                    if m:
                        max_num = max(max_num, int(m.group(1)))
            trap_id = f"TRAP-{max(max_num, len(traps)) + 1:03d}"
            result = {
                "id": trap_id,
                "title": clean_reason[:120],
                "reason": clean_reason,
                "trigger_pattern": trigger_pattern,
                "status": "candidate",
                "occurrences": 1,
                "promotion_candidate": False,
                "first_seen_at": now,
                "last_seen_at": now,
                "source_traces": [trace_entry],
            }
            traps.append(result)

        traps_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return result

    def log_audit(
        self,
        task_id: str,
        action: str,
        agent: str,
        verification_status: str,
        payload: str = "",
    ) -> str:
        """Append an entry to the SHA-256 hash-chained WORM audit ledger."""
        now = utc_now_iso()
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        conn = sqlite3.connect(str(self.audit_db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            previous_hash = row["entry_hash"] if row else GENESIS_HASH

            raw_header = f"{now}:{task_id}:{action}:{agent}:{verification_status}:{payload_hash}:{previous_hash}"
            entry_hash = hashlib.sha256(raw_header.encode("utf-8")).hexdigest()

            cur.execute(
                """
                INSERT INTO audit_log (
                    timestamp, task_id, action, agent,
                    verification_status, payload_hash, previous_hash, entry_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    task_id,
                    action,
                    agent,
                    verification_status,
                    payload_hash,
                    previous_hash,
                    entry_hash,
                ),
            )
            conn.execute("COMMIT;")
            return entry_hash
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def verify_audit_chain(self) -> Tuple[bool, str]:
        """Validate hash chain link integrity across all entries in audit_log supporting Tier A and Tier B."""
        with self.get_audit_conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(audit_log)")
            cols = {r["name"] for r in cur.fetchall()}
            if not cols:
                return True, "audit_log table does not exist"
            hash_col = "hash" if "hash" in cols else "entry_hash"

            cur.execute("SELECT * FROM audit_log ORDER BY id ASC")
            rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            return True, "Audit log is empty"

        prev_hash: Optional[str] = None
        for i, row in enumerate(rows):
            current_hash = row.get(hash_col)
            previous_hash = row.get("previous_hash")

            if i == 0:
                # Genesis row: previous_hash can be "0"*64, None, or empty
                if previous_hash not in ("0" * 64, None, ""):
                    return (
                        False,
                        f"Hash break at genesis ID {row['id']}: expected genesis, got {previous_hash}",
                    )
            else:
                if previous_hash != prev_hash:
                    return (
                        False,
                        f"Hash break at ID {row['id']}: expected previous {prev_hash}, got {previous_hash}",
                    )

            if hash_col == "entry_hash" and "payload_hash" in cols:
                raw_header = (
                    f"{row['timestamp']}:{row['task_id']}:{row['action']}:"
                    f"{row['agent']}:{row['verification_status']}:{row['payload_hash']}:{row['previous_hash']}"
                )
                calculated_hash = hashlib.sha256(raw_header.encode("utf-8")).hexdigest()
                if calculated_hash != current_hash:
                    return (
                        False,
                        f"Hash mismatch at ID {row['id']}: calculated {calculated_hash}, stored {current_hash}",
                    )

            prev_hash = current_hash

        return True, f"Verified {len(rows)} audit entries with intact hash chain"

    def sync_board(self) -> Path:
        """Render markdown board partitioned by status into Task_Board.md."""
        tasks = self.list_tasks()
        sections: Dict[str, List[Dict[str, Any]]] = {
            "todo": [],
            "working": [],
            "done": [],
            "blocked": [],
        }

        for t in tasks:
            st = t["status"]
            if st not in sections:
                sections[st] = []
            sections[st].append(t)

        now = utc_now_iso()
        content = [
            f"# StigmeRig Task Board",
            f"",
            f"*Last synchronized: {now}*",
            f"",
        ]

        status_display = [
            ("working", "Active / In Progress"),
            ("todo", "To Do / Backlog"),
            ("blocked", "Blocked / Human Hold"),
            ("done", "Completed"),
        ]

        for st_key, display_name in status_display:
            items = sections.get(st_key, [])
            content.append(f"## {display_name} ({len(items)})")
            content.append("")
            if not items:
                content.append("_No tasks._")
            else:
                content.append("| ID | Title | Archetype | Claimant | Updated |")
                content.append("| :--- | :--- | :--- | :--- | :--- |")
                for item in items:
                    claimant = item.get("claimed_by") or "-"
                    arch = item.get("archetype") or "diff_synthesizer"
                    content.append(f"| `{item['id']}` | {item['title']} | `{arch}` | {claimant} | {item['updated_at']} |")
            content.append("")

        self.board_path.write_text("\n".join(content) + "\n", encoding="utf-8")
        return self.board_path

"""StigmeRig local-first database and state engine."""

from __future__ import annotations

from contextlib import contextmanager
import datetime
import fnmatch
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from stigmerig.traps import trap_file_lock


GENESIS_HASH: str = "0" * 64
SHA256_HEX_PATTERN: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{64}$")
TIER_B_AUDIT_MARKERS: frozenset[str] = frozenset({"entry_hash", "payload_hash", "verification_status"})
_LINK_REPARSE_TAGS: frozenset[int] = frozenset({
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
})
_RIG_LOCKS_GUARD = threading.Lock()
_RIG_LOCKS: Dict[str, threading.RLock] = {}


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or (getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
WORKFLOW_RAILS: Tuple[str, ...] = ("diff_synthesizer", "unit_tester", "policy_gatekeeper")
VERIFY_ONLY_ARCHETYPES: frozenset[str] = frozenset({
    "unit_tester",
    "prose_auditor",
    "policy_gatekeeper",
    "topology_reader",
})
TASK_ID_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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

_AGENTS_MANAGED_BODY = """## Autonomous Operation Invariants

1. **Local-First Stigmergy**: Agents coordinate indirectly through `.stigmerig` task state and filesystem artifacts.
2. **Worktree Isolation**: Implementers isolate execution branches in dedicated git worktrees.
3. **Anti-Self-Approval**: Verification must be performed by an independent verifier before completing tasks.
4. **Append-Only WORM Audit**: All state transitions and completion records are hash-chained in the audit ledger.
5. **Deterministic Trap Ratchet**: Run `stigmerig traps scan` across polyglot source files and bind verification outputs via `--evidence`.

## Workflow

1. Query available tasks via `stigmerig task list --status todo`.
2. Claim a task with lease TTL via `stigmerig task claim --id <id> --agent <agent>`.
3. Scan traps via `stigmerig traps scan` and complete verification via `stigmerig task done --id <id> --agent <agent> --verifier <verifier> --evidence <path>`."""


def _compute_icm_sha256(body: str) -> str:
    """Compute canonical 16-hex-char SHA-256 digest of LF-normalized ICM directive text."""
    normalized = body.replace("\r\n", "\n").strip("\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


CANONICAL_AGENTS_SHA256: str = _compute_icm_sha256(_AGENTS_MANAGED_BODY)
_ICM_BEGIN_MARKER_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*<!--\s*stigmerig:begin\s+sha256=([0-9a-fA-F]{16})\s*-->[ \t]*$"
)
_ICM_END_MARKER_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*<!--\s*stigmerig:end\s*-->[ \t]*$"
)
_ICM_MARKER_PREFIX_RE: re.Pattern[str] = re.compile(r"^[ \t]*<!--\s*stigmerig:")
_LINE_SPLIT_RE: re.Pattern[str] = re.compile(r"([^\r\n]*)(\r\n|\r|\n|$)")


def _render_icm_block(newline: str = "\n") -> str:
    """Render bounded managed AGENTS block using the given line ending."""
    body = _AGENTS_MANAGED_BODY.replace("\n", newline)
    return (
        f"<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->{newline}"
        f"{body}{newline}"
        f"<!-- stigmerig:end -->"
    )


def _split_lines_with_endings(text: str) -> List[Tuple[str, str]]:
    """Split text into (line, eol) pairs without normalizing CRLF or LF."""
    lines: List[Tuple[str, str]] = []
    for match in _LINE_SPLIT_RE.finditer(text):
        if match.start() == len(text):
            break
        lines.append((match.group(1), match.group(2)))
    return lines


def _parse_icm_markers(
    text: str,
) -> Optional[Tuple[List[Tuple[str, str]], int, int, str, str]]:
    """Parse bounded stigmerig markers from raw text.

    Returns None when zero line-anchored markers exist, otherwise returns
    (lines, begin_idx, end_idx, header_sha256, actual_sha256).
    Raises ValueError on malformed, duplicate, nested, reversed, or unclosed markers.
    """
    lines = _split_lines_with_endings(text)
    begin_idx: Optional[int] = None
    header_sha256: Optional[str] = None
    end_idx: Optional[int] = None
    closed = False

    for idx, (line_body, _) in enumerate(lines):
        begin_match = _ICM_BEGIN_MARKER_RE.match(line_body)
        if begin_match:
            if begin_idx is not None:
                kind = "Duplicate" if closed else "Nested"
                raise ValueError(f"{kind} stigmerig:begin marker at line {idx + 1}")
            begin_idx = idx
            header_sha256 = begin_match.group(1).lower()
            continue

        if _ICM_END_MARKER_RE.match(line_body):
            if begin_idx is None:
                raise ValueError(f"Reversed or orphan stigmerig:end marker at line {idx + 1}")
            if closed:
                raise ValueError(f"Duplicate stigmerig:end marker at line {idx + 1}")
            end_idx = idx
            closed = True
            continue

        if _ICM_MARKER_PREFIX_RE.match(line_body):
            raise ValueError(f"Malformed stigmerig marker at line {idx + 1}")

    if begin_idx is not None and not closed:
        raise ValueError(f"Unclosed stigmerig:begin marker at line {begin_idx + 1}")

    if begin_idx is None:
        return None

    assert end_idx is not None and header_sha256 is not None
    inner = "".join(line + eol for line, eol in lines[begin_idx + 1 : end_idx])
    actual_sha256 = _compute_icm_sha256(inner)
    return lines, begin_idx, end_idx, header_sha256, actual_sha256


def _detect_line_ending(
    lines: Sequence[Tuple[str, str]], begin_idx: int, end_idx: int
) -> str:
    """Detect CRLF vs LF preference from outer prose lines, falling back to full file."""
    outer_eols = [
        eol for _, eol in (*lines[:begin_idx], *lines[end_idx:]) if eol in ("\r\n", "\n")
    ]
    sample = outer_eols or [eol for _, eol in lines if eol in ("\r\n", "\n")]
    crlf_count = sample.count("\r\n")
    lf_count = sample.count("\n")
    return "\r\n" if crlf_count > 0 and crlf_count >= lf_count else "\n"


def _replace_icm_block(
    lines: Sequence[Tuple[str, str]], begin_idx: int, end_idx: int
) -> str:
    """Replace lines[begin_idx..end_idx] while preserving surrounding prose and EOLs."""
    line_sep = _detect_line_ending(lines, begin_idx, end_idx)
    prefix = "".join(line + eol for line, eol in lines[:begin_idx])
    suffix = "".join(line + eol for line, eol in lines[end_idx + 1 :])
    end_eol = lines[end_idx][1]
    return f"{prefix}{_render_icm_block(line_sep)}{end_eol}{suffix}"


DEFAULT_AGENTS_TEMPLATE = f"# AGENTS\n\n{_render_icm_block()}\n"

DEFAULT_CONTEXT_TEMPLATE = """# Project Context

## Overview
Active multi-agent coordination environment managed by StigmeRig.

## Current State
- Coordination Engine: Active
- Task State: `.stigmerig/`
"""

DEFAULT_RIG_JSON_TEMPLATE = """{
  "version": "0.1.0",
  "icm_enabled": true,
  "max_pending_review": 3,
  "high_friction_tags": [
    "security",
    "migration",
    "infra",
    "release",
    "destructive"
  ]
}
"""


def _match_segment_glob(path_str: str, pattern_str: str) -> bool:
    """Match a POSIX path against a glob pattern with segment-aware * and **."""
    norm_path = path_str.strip().replace("\\", "/").lstrip("./").strip("/")
    norm_pat = pattern_str.strip().replace("\\", "/").lstrip("./").strip("/")
    if not norm_path or not norm_pat:
        return False

    path_parts = [p for p in norm_path.split("/") if p]
    if "/" not in norm_pat and norm_pat != "**":
        return fnmatch.fnmatchcase(path_parts[-1], norm_pat)

    pat_parts = [p for p in norm_pat.split("/") if p]
    memo: Dict[Tuple[int, int], bool] = {}

    def _match(pi: int, si: int) -> bool:
        key = (pi, si)
        if key in memo:
            return memo[key]
        if pi == len(pat_parts) and si == len(path_parts):
            res = True
        elif pi < len(pat_parts) and pat_parts[pi] == "**":
            res = _match(pi + 1, si) or (si < len(path_parts) and _match(pi, si + 1))
        elif pi == len(pat_parts) or si == len(path_parts):
            res = False
        else:
            res = fnmatch.fnmatchcase(path_parts[si], pat_parts[pi]) and _match(pi + 1, si + 1)
        memo[key] = res
        return res

    return _match(0, 0)


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


def _validate_model_string(value: str, label: str) -> str:
    """Validate and strip a non-empty printable model identifier."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(c in value for c in ("\r", "\n", "\t", "\x00"))
        or not value.isprintable()
    ):
        raise ValueError(f"{label} must be a non-empty printable string")
    return value.strip()


def resolve_model_id(model_id: Optional[str] = None) -> Optional[str]:
    """Validate explicit model_id or fall back to STIGMERIG_MODEL environment variable."""
    if model_id is not None:
        return _validate_model_string(model_id, "model_id")
    env_model = os.environ.get("STIGMERIG_MODEL")
    if env_model is None or env_model == "":
        return None
    return _validate_model_string(env_model, "STIGMERIG_MODEL")


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


UTC_ISO_TIMESTAMP_PATTERN: re.Pattern[str] = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def _is_canonical_utc_iso(value: str) -> bool:
    if not isinstance(value, str) or not UTC_ISO_TIMESTAMP_PATTERN.fullmatch(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _validate_audit_header_fields(task_id: str, action: str, agent: str, verification_status: str) -> Optional[str]:
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        return f"invalid task_id {task_id!r}"
    if not isinstance(agent, str) or not agent.strip() or not agent.isprintable():
        return "agent must be non-empty"
    for name, val in (("action", action), ("verification_status", verification_status)):
        if not isinstance(val, str) or not val.strip() or ":" in val or not val.isprintable():
            return f"invalid {name}"
    return None


def _escape_md_cell(value: Any) -> str:
    """Escape markdown table cell values to prevent breaking column layout."""
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return text.replace("|", r"\|")


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

    def __init__(
        self,
        rig_dir: Optional[Path | str] = None,
        workspace_root: Optional[Path | str] = None,
    ):
        raw_input = (
            Path(rig_dir)
            if rig_dir
            else (
                Path(env_root)
                if (env_root := os.environ.get("STIGMERIG_ROOT"))
                else Path.cwd() / ".stigmerig"
            )
        )
        self._input_rig_dir: Path = raw_input
        self._raw_rig_dir: Path = Path(os.path.abspath(str(raw_input)))
        self.rig_dir = resolve_rig_dir(rig_dir)
        self.workspace_root: Optional[Path] = (
            Path(workspace_root).resolve() if workspace_root else None
        )
        self.queue_db_path = self.rig_dir / "queue.db"
        self.audit_db_path = self.rig_dir / "audit.db"
        self.tasks_dir = self.rig_dir / "tasks"
        self.board_path = self.rig_dir / "Task_Board.md"
        with _RIG_LOCKS_GUARD:
            self._lock = _RIG_LOCKS.setdefault(str(self.rig_dir), threading.RLock())

    def _load_rig_config(self) -> Dict[str, Any]:
        """Parse optional review backpressure and intentional friction settings from rig.json."""
        defaults: Dict[str, Any] = {
            "max_pending_review": None,
            "high_friction_tags": frozenset(),
            "high_friction_globs": (),
        }
        rig_json = self.rig_dir / "rig.json"
        if not rig_json.is_file():
            return defaults

        try:
            raw = json.loads(rig_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid rig.json at {rig_json}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError(f"Invalid rig.json at {rig_json}: root must be a JSON object")

        max_pending = raw.get("max_pending_review")
        if max_pending is not None:
            if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 0:
                raise ValueError(f"Invalid max_pending_review in rig.json: {max_pending!r}")
            defaults["max_pending_review"] = max_pending

        raw_tags = raw.get("high_friction_tags")
        if raw_tags is not None:
            if not isinstance(raw_tags, list) or any(not isinstance(t, str) or not t.strip() for t in raw_tags):
                raise ValueError("Invalid high_friction_tags in rig.json: must be a list of non-empty strings")
            defaults["high_friction_tags"] = frozenset(t.strip().lower() for t in raw_tags)

        raw_globs = raw.get("high_friction_globs")
        if raw_globs is not None:
            if not isinstance(raw_globs, list) or any(not isinstance(g, str) or not g.strip() for g in raw_globs):
                raise ValueError("Invalid high_friction_globs in rig.json: must be a list of non-empty strings")
            defaults["high_friction_globs"] = tuple(g.strip().replace("\\", "/") for g in raw_globs)

        return defaults

    @staticmethod
    def _validate_touched_paths(touched_paths: Optional[Sequence[str | Path]]) -> Tuple[str, ...]:
        """Validate and normalize touched_paths into POSIX strings."""
        if touched_paths is None:
            return ()
        if isinstance(touched_paths, (str, bytes, bytearray, Path)) or not isinstance(touched_paths, Sequence):
            raise ValueError("touched_paths must be a sequence of paths, not a bare string/bytes")
        normalized: List[str] = []
        for item in touched_paths:
            if not isinstance(item, (str, Path)):
                raise ValueError(f"Invalid touched path: {item!r}")
            s = str(item).strip()
            if not s or not s.isprintable():
                raise ValueError(f"Invalid touched path: {item!r}")
            normalized.append(s.replace("\\", "/"))
        return tuple(normalized)

    def _matches_intentional_friction(self, task: Dict[str, Any], touched_paths: Sequence[str]) -> bool:
        """Return True if task tags or touched_paths match configured intentional friction rules."""
        cfg = self._load_rig_config()
        hf_tags: frozenset[str] = cfg["high_friction_tags"]
        if hf_tags:
            task_tags = {t.strip().lower() for t in (task.get("tags") or "").split(",") if t.strip()}
            if task_tags & hf_tags:
                return True

        hf_globs: Tuple[str, ...] = cfg["high_friction_globs"]
        if hf_globs and touched_paths:
            for tp in touched_paths:
                for pat in hf_globs:
                    if _match_segment_glob(tp, pat):
                        return True
        return False

    def _validate_icm_paths(self, *, include_icm_files: bool = True) -> None:
        """Reject symlinks and NTFS junctions on parent_dir, rig_dir, AGENTS.md, and sidecar."""
        rig_candidates: List[Path] = [self.rig_dir]
        for cand in (getattr(self, "_raw_rig_dir", None), getattr(self, "_input_rig_dir", None)):
            if isinstance(cand, Path) and cand not in rig_candidates:
                rig_candidates.append(cand)

        parent_candidates: List[Path] = []
        for rig_cand in rig_candidates:
            parent_cand = rig_cand.parent
            if parent_cand not in parent_candidates:
                parent_candidates.append(parent_cand)

        checks: List[Tuple[str, Path]] = [
            *(("parent_dir", p) for p in parent_candidates),
            *(("rig_dir", p) for p in rig_candidates),
        ]
        if include_icm_files:
            checks.extend(("AGENTS.md", p / "AGENTS.md") for p in parent_candidates)
            checks.extend(("AGENTS.stigmerig.md", p / "AGENTS.stigmerig.md") for p in rig_candidates)

        for label, target in checks:
            if _is_link_or_junction(target):
                raise ValueError(f"Refusing symlink or junction for {label}: {target}")

    @staticmethod
    def _read_icm_text(path: Path) -> str:
        """Read UTF-8 bytes from a regular ICM file without newline translation."""
        if not path.is_file():
            raise ValueError(f"Expected regular file for ICM target: {path}")
        try:
            return path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Invalid UTF-8 in {path}: {exc}") from exc

    def init_rig(self, scaffold_icm: bool = True, sync_icm: bool = False) -> Optional[str]:
        """Initialize directory layout, SQLite databases with WAL pragmas, and ICM scaffolding."""
        if sync_icm and not scaffold_icm:
            raise ValueError("Cannot combine sync_icm=True with scaffold_icm=False")
        self._validate_icm_paths(include_icm_files=scaffold_icm)
        if scaffold_icm and sync_icm:
            agents_file = self.rig_dir.parent / "AGENTS.md"
            if agents_file.exists():
                _parse_icm_markers(self._read_icm_text(agents_file))

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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_priority_created_id "
                "ON tasks(status, priority, created_at, id);"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent_status ON tasks(parent_id, status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, id);")
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
            audit_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()
            }
            if "entry_hash" not in audit_cols:
                raise RuntimeError(
                    f"Legacy audit_log schema detected in {self.audit_db_path} "
                    "(missing 'entry_hash'). Pass --rig-dir to use an isolated v2 "
                    "state directory or archive the legacy audit.db."
                )
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
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS enforce_contiguous_audit_id
                AFTER INSERT ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'Audit ID break: id must be contiguous 1..N')
                    WHERE NEW.id < 1 OR NEW.id IS NOT (SELECT COUNT(*) FROM audit_log);
                END;
            """)
            conn.commit()

        if not self.board_path.exists():
            self.sync_board()

        hint: Optional[str] = None
        if scaffold_icm:
            hint = self.scaffold_icm(sync_icm=sync_icm)

        return hint

    def scaffold_icm(self, sync_icm: bool = False) -> Optional[str]:
        """Scaffold or synchronize ICM files (AGENTS.md, CONTEXT.md, rig.json)."""
        self._validate_icm_paths(include_icm_files=True)
        parent_dir = self.rig_dir.parent
        rig_json = self.rig_dir / "rig.json"
        if _is_link_or_junction(rig_json):
            raise ValueError(f"Refusing symlink or junction for rig.json: {rig_json}")
        if not rig_json.exists():
            _atomic_write_text(rig_json, DEFAULT_RIG_JSON_TEMPLATE)

        agents_file = parent_dir / "AGENTS.md"
        sidecar_file = self.rig_dir / "AGENTS.stigmerig.md"
        hint: Optional[str] = None

        if not agents_file.exists():
            _atomic_write_text(agents_file, DEFAULT_AGENTS_TEMPLATE)
        elif sync_icm:
            raw_text = self._read_icm_text(agents_file)
            parsed = _parse_icm_markers(raw_text)
            if parsed is not None:
                lines, begin_idx, end_idx, _, _ = parsed
                updated_text = _replace_icm_block(lines, begin_idx, end_idx)
                if updated_text != raw_text:
                    _atomic_write_text(agents_file, updated_text)
                hint = (
                    f"Synchronized bounded ICM block in {agents_file.name} "
                    f"(sha256={CANONICAL_AGENTS_SHA256})"
                )
            else:
                _atomic_write_text(sidecar_file, DEFAULT_AGENTS_TEMPLATE)
                hint = (
                    f"AGENTS.md already exists without stigmerig markers; wrote sidecar instructions "
                    f"to .stigmerig/{sidecar_file.name}. Embed the bounded block in AGENTS.md:\n"
                    f"<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->\n"
                    f"<!-- stigmerig:end -->"
                )
        else:
            if not sidecar_file.exists():
                _atomic_write_text(sidecar_file, DEFAULT_AGENTS_TEMPLATE)
            hint = f"AGENTS.md already exists; wrote sidecar instructions to .stigmerig/{sidecar_file.name} (include it in your AGENTS.md)"

        context_file = parent_dir / "CONTEXT.md"
        if _is_link_or_junction(context_file):
            raise ValueError(f"Refusing symlink or junction for CONTEXT.md: {context_file}")
        if not context_file.exists():
            _atomic_write_text(context_file, DEFAULT_CONTEXT_TEMPLATE)

        return hint

    def check_icm(self) -> Tuple[bool, str]:
        """Verify ICM directive digests read-only without creating files or databases."""
        self._validate_icm_paths(include_icm_files=True)
        agents_file = self.rig_dir.parent / "AGENTS.md"
        sidecar_file = self.rig_dir / "AGENTS.stigmerig.md"

        if agents_file.exists():
            raw_agents = self._read_icm_text(agents_file)
            parsed_agents = _parse_icm_markers(raw_agents)
            if parsed_agents is not None:
                _, _, _, header_sha, actual_sha = parsed_agents
                if actual_sha == header_sha == CANONICAL_AGENTS_SHA256:
                    return (
                        True,
                        f"ICM directives in {agents_file.name} match canonical sha256={CANONICAL_AGENTS_SHA256}",
                    )
                return (
                    False,
                    f"ICM directive drift in {agents_file.name} "
                    f"(canonical={CANONICAL_AGENTS_SHA256}, header={header_sha}, actual={actual_sha}); "
                    f"run 'stigmerig init --sync-icm'",
                )

        if not sidecar_file.exists():
            if agents_file.exists():
                return (
                    False,
                    f"{agents_file.name} contains no bounded stigmerig markers and "
                    f".stigmerig/{sidecar_file.name} is missing; run 'stigmerig init --sync-icm'",
                )
            return (
                False,
                f"ICM directives missing ({agents_file.name} and .stigmerig/{sidecar_file.name} not found); "
                f"run 'stigmerig init --sync-icm'",
            )

        raw_sidecar = self._read_icm_text(sidecar_file)
        parsed_sidecar = _parse_icm_markers(raw_sidecar)
        if parsed_sidecar is None:
            return (
                False,
                f"Sidecar .stigmerig/{sidecar_file.name} contains no bounded stigmerig markers; "
                f"run 'stigmerig init --sync-icm'",
            )
        _, _, _, header_sha, actual_sha = parsed_sidecar
        if actual_sha == header_sha == CANONICAL_AGENTS_SHA256:
            return (
                True,
                f"ICM directives in .stigmerig/{sidecar_file.name} match canonical sha256={CANONICAL_AGENTS_SHA256}",
            )
        return (
            False,
            f"ICM directive drift in .stigmerig/{sidecar_file.name} "
            f"(canonical={CANONICAL_AGENTS_SHA256}, header={header_sha}, actual={actual_sha}); "
            f"run 'stigmerig init --sync-icm'",
        )

    @contextmanager
    def get_queue_conn(self):
        """Create a connection to queue.db with WAL pragmas, closing on context exit."""
        conn = sqlite3.connect(str(self.queue_db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    @contextmanager
    def get_audit_conn(self):
        """Create a connection to audit.db with WAL pragmas, closing on context exit."""
        conn = sqlite3.connect(str(self.audit_db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _ensure_queue_ready(self) -> None:
        if not self.queue_db_path.is_file():
            raise FileNotFoundError(f"Uninitialized queue database: {self.queue_db_path}; run 'stigmerig init'")
        with self.get_queue_conn() as conn:
            rows = {
                r["name"]: r["sql"] or ""
                for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('tasks', 'task_events')")
            }
            if any(t not in rows or re.search(r"\bVIRTUAL\s+TABLE\b", rows[t], re.IGNORECASE) for t in ("tasks", "task_events")):
                raise sqlite3.OperationalError("tasks and task_events must be non-virtual base tables")

    def _ensure_audit_ready(self) -> None:
        if not self.audit_db_path.is_file():
            raise FileNotFoundError(f"Uninitialized audit database: {self.audit_db_path}")
        with self.get_audit_conn() as conn:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone()
            if not row or "VIRTUAL TABLE" in (row["sql"] or "").upper():
                raise sqlite3.OperationalError("audit_log must be a non-virtual base table")

    def _validate_task_id(self, task_id: str) -> str:
        """Validate task identifier syntax preventing directory traversal."""
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task_id '{task_id}': must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$")
        return task_id

    def get_task_file(self, task_id: str) -> Path:
        """Resolve task markdown file supporting both flat tasks/<ID>.md and nested tasks/<ID>/context.md."""
        self._validate_task_id(task_id)
        tasks_root = self.tasks_dir.resolve()
        nested = self.tasks_dir / task_id / "context.md"
        flat = self.tasks_dir / f"{task_id}.md"

        if nested.exists():
            candidate = nested
        elif flat.exists():
            candidate = flat
        else:
            candidate = nested if (self.tasks_dir / task_id).is_dir() else flat

        resolved_candidate = candidate.resolve()
        try:
            if not resolved_candidate.is_relative_to(tasks_root):
                raise ValueError(f"Task file traversal detected: '{candidate}' escapes '{tasks_root}'")
        except (ValueError, AttributeError):
            raise ValueError(f"Task file traversal detected: '{candidate}' escapes '{tasks_root}'")

        if candidate.is_symlink() and not candidate.resolve().is_relative_to(tasks_root):
            raise ValueError(f"Task symlink escapes tasks directory: '{candidate}'")

        return candidate

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
        self._validate_task_id(task_id)
        if not isinstance(title, str) or not title.strip() or not title.replace("\t", " ").replace("\n", " ").replace("\r", " ").isprintable():
            raise ValueError("Invalid title: must be a non-empty string without control characters")
        if not isinstance(archetype, str) or not archetype.strip() or not archetype.isprintable():
            raise ValueError("Invalid archetype: must be a non-empty printable string")
        if isinstance(priority, bool) or not isinstance(priority, int) or not (-(1 << 63) <= priority < (1 << 63)):
            raise ValueError("Invalid priority: must be a 64-bit signed integer")
        if parent_id is not None and (not isinstance(parent_id, str) or not TASK_ID_PATTERN.fullmatch(parent_id) or parent_id == task_id):
            raise ValueError(f"Invalid parent_id {parent_id!r}: must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$ and not reference itself")
        if author is not None and (not isinstance(author, str) or not author.strip() or not author.isprintable()):
            raise ValueError("Invalid author: must be a non-empty printable string")
        if tags is not None and (not isinstance(tags, str) or not tags.isprintable() or any(not seg.strip() for seg in tags.split(","))):
            raise ValueError("Invalid tags: must be comma-separated non-empty printable segments")
        self._ensure_queue_ready()
        self._ensure_audit_ready()
        now = utc_now_iso()
        task_file = self.get_task_file(task_id)
        tasks_root = self.tasks_dir.resolve()
        resolved_file = task_file.resolve()
        if not resolved_file.is_relative_to(tasks_root):
            raise ValueError(f"Task file traversal detected: '{task_file}' escapes '{tasks_root}'")
        context_path = str(resolved_file.relative_to(self.rig_dir.parent.resolve()))

        with self.get_queue_conn() as conn:
            try:
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
            except sqlite3.IntegrityError as exc:
                if "tasks.id" in str(exc) or getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_PRIMARYKEY":
                    raise sqlite3.IntegrityError(f"Task '{task_id}' already exists") from exc
                raise
            conn.execute(
                """
                INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
                VALUES (?, 'created', ?, ?, ?)
                """,
                (task_id, author or "system", json.dumps({"title": title, "archetype": archetype}), now),
            )
            conn.commit()

        if not task_file.exists():
            task_file.parent.mkdir(parents=True, exist_ok=True)
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
        self._ensure_queue_ready()
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
        self._ensure_queue_ready()
        query = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if parent_id:
            query += " AND parent_id = ?"
            params.append(parent_id)
        query += " ORDER BY priority ASC, created_at ASC, id ASC"

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def claim_task(
        self,
        task_id: str,
        agent: str,
        ttl_seconds: int = 1800,
        ignore_backpressure: bool = False,
    ) -> bool:
        """Atomic Compare-And-Swap task lease claiming.

        A claim succeeds if:
        1. Task status is 'todo' or 'working'.
        2. Task is either unclaimed, expired, or already claimed by the same agent.
        3. The version in the DB matches the in-memory read version.
        4. Review-queue backpressure permits the claim (when configured).
        """
        self._validate_task_id(task_id)
        if not isinstance(ignore_backpressure, bool):
            raise ValueError("ignore_backpressure must be a bool")
        if not isinstance(agent, str) or not agent.strip() or not agent.replace("\r", " ").replace("\n", " ").isprintable():
            raise ValueError("agent must be a non-empty string")
        try:
            if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
                raise ValueError(f"ttl_seconds must be a finite number > 0, got {ttl_seconds!r}")
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            lease_until_dt = now_dt + datetime.timedelta(seconds=ttl_seconds)
        except OverflowError as exc:
            raise ValueError(f"ttl_seconds out of range: {ttl_seconds!r}") from exc
        self._ensure_queue_ready()
        self._ensure_audit_ready()
        task = self.get_task(task_id)
        if not task:
            return False

        status = task.get("status")
        if status not in ("todo", "working"):
            return False

        rig_cfg = self._load_rig_config()
        max_pending_review = rig_cfg["max_pending_review"]
        archetype = (task.get("archetype") or "diff_synthesizer").strip().lower()
        apply_backpressure = (
            status == "todo"
            and max_pending_review is not None
            and archetype not in VERIFY_ONLY_ARCHETYPES
            and not ignore_backpressure
        )

        now_str = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
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

        backpressure_clause = ""
        update_params: List[Any] = [agent, lease_until_str, now_str, task_id, version, agent, now_str]
        if apply_backpressure:
            backpressure_clause = "AND (status = 'working' OR (SELECT COUNT(*) FROM tasks WHERE status = 'review') < ?)"
            update_params.append(max_pending_review)

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
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
                  {backpressure_clause}
                """,
                tuple(update_params),
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
                agent=agent.replace("\r", " ").replace("\n", " "),
                verification_status="in_progress",
                payload=json.dumps({"lease_until": lease_until_str}),
            )
            self.sync_board()
        return success

    def _validate_and_hash_evidence(
        self,
        evidence_paths: Sequence[str | Path],
    ) -> List[Dict[str, Any]]:
        """Validate evidence paths against workspace boundaries and return SHA-256 metadata."""
        if isinstance(evidence_paths, (str, bytes, bytearray, Path)) or not isinstance(evidence_paths, Sequence):
            raise ValueError("evidence_paths must be a sequence of paths, not a bare string/bytes")
        if not evidence_paths:
            raise ValueError("evidence_paths must not be empty")
        rig_resolved = self.rig_dir.resolve()
        allowed_root = self.workspace_root if self.workspace_root is not None else rig_resolved.parent
        strict_ws_ancs = set(allowed_root.parents)
        ws_ancs = {allowed_root, *strict_ws_ancs}
        case_fold = os.path.normcase("A") == "a"
        seen_paths: set[str] = set()
        seen_files: set[Tuple[int, int]] = set()
        records: List[Dict[str, Any]] = []
        for raw_path in evidence_paths:
            if not isinstance(raw_path, (str, Path)):
                raise ValueError(f"Invalid evidence path: {raw_path!r}")
            raw_str = str(raw_path)
            if not raw_str.strip() or not raw_str.isprintable():
                raise ValueError(f"Invalid evidence path: {raw_path!r}")
            p = Path(raw_str)
            cand = p if p.is_absolute() else (allowed_root / p)
            abs_cand = Path(os.path.abspath(str(cand)))
            if (
                _is_link_or_junction(cand)
                or _is_link_or_junction(abs_cand)
                or any(
                    _is_link_or_junction(a)
                    for a in (*cand.parents, *abs_cand.parents)
                    if a not in strict_ws_ancs
                )
            ):
                raise ValueError(f"Evidence path must not be or traverse a symlink or junction: {raw_path}")
            try:
                resolved = cand.resolve()
                anc_res: List[Path] = []
                for a in cand.parents:
                    if a in ws_ancs:
                        continue
                    r = a.resolve()
                    if ".." not in a.parts and r in ws_ancs:
                        continue
                    anc_res.append(r)
            except OSError as exc:
                raise ValueError(f"Invalid evidence path: {raw_path!r}") from exc

            if (
                resolved == allowed_root
                or not resolved.is_relative_to(allowed_root)
                or any(not r.is_relative_to(allowed_root) for r in anc_res)
            ):
                raise ValueError(f"Evidence path escapes allowed root: {raw_path}")

            if _is_link_or_junction(resolved) or any(
                _is_link_or_junction(a) for a in resolved.parents if a not in ws_ancs
            ):
                raise ValueError(f"Evidence path must not be or traverse a symlink or junction: {raw_path}")

            rel = resolved.relative_to(allowed_root)
            sub_ancs = [r for r in anc_res if r != allowed_root and r.is_relative_to(allowed_root)]
            check_parts = (
                *(pt for c in (resolved, *sub_ancs) for pt in c.relative_to(allowed_root).parts),
                *(() if p.is_absolute() else p.parts),
            )
            norm_parts = [part.rstrip(". ").lower() for part in check_parts]
            name_lower = resolved.name.rstrip(". ").lower()

            if (
                resolved.is_relative_to(rig_resolved)
                or any(r.is_relative_to(rig_resolved) for r in sub_ancs)
                or ".stigmerig" in norm_parts
                or ".git" in norm_parts
                or ".worktrees" in norm_parts
                or any(part == ".env" or part.startswith(".env.") for part in norm_parts)
                or name_lower == ".env"
                or name_lower.startswith(".env.")
                or name_lower.startswith("queue.db")
                or name_lower.startswith("audit.db")
            ):
                raise ValueError(f"Evidence path matches denied pattern: {raw_path}")

            if not resolved.exists() or not resolved.is_file():
                raise FileNotFoundError(f"Evidence file not found: {raw_path}")

            st = resolved.stat()
            size = st.st_size
            if size <= 0:
                raise ValueError(f"Evidence file must be non-empty: {raw_path}")
            path_key = os.path.normcase(str(resolved)).casefold() if case_fold else os.path.normcase(str(resolved))
            file_id = (st.st_dev, st.st_ino) if (st.st_dev and st.st_ino) else None
            if path_key in seen_paths or (file_id is not None and file_id in seen_files):
                raise ValueError(f"Duplicate evidence path: {raw_path}")
            seen_paths.add(path_key)
            if file_id is not None:
                seen_files.add(file_id)

            digest = hashlib.sha256()
            with resolved.open("rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)

            records.append({
                "path": rel.as_posix(),
                "sha256": digest.hexdigest(),
                "bytes": size,
            })
        return records

    @staticmethod
    def _format_follow_up_prefix(task_id: str) -> str:
        """Build sanitized uppercase follow-up ID prefix truncated to stay within 64 chars."""
        sanitized = re.sub(r"[^A-Z0-9._-]", "", task_id.upper())[:56]
        if not sanitized:
            sanitized = "TASK"
        return f"FU-{sanitized}-"

    @staticmethod
    def _validate_follow_ups(
        follow_ups: Optional[Sequence[str]],
        follow_up_ids_out: Optional[List[str]],
    ) -> Tuple[str, ...]:
        """Validate optional follow-up titles and output list upfront."""
        if follow_up_ids_out is not None and not isinstance(follow_up_ids_out, list):
            raise ValueError("follow_up_ids_out must be a list when provided")
        if follow_ups is None:
            return ()
        if isinstance(follow_ups, (str, bytes, bytearray, os.PathLike)) or not isinstance(follow_ups, (list, tuple)):
            raise ValueError("follow_ups must be a list or tuple of strings")
        if len(follow_ups) > 10:
            raise ValueError("follow_ups supports at most 10 items per completion")
        normalized: List[str] = []
        for item in follow_ups:
            if (
                not isinstance(item, str)
                or any(c in item for c in ("\r", "\n", "\t", "\x00"))
                or not item.isprintable()
            ):
                raise ValueError("Each follow_up title must be a single-line printable string")
            stripped = item.strip()
            if not stripped or len(stripped) > 240:
                raise ValueError("Each follow_up title must have stripped length between 1 and 240 characters")
            normalized.append(stripped)
        return tuple(normalized)

    def _insert_follow_up_tasks(
        self,
        conn: sqlite3.Connection,
        *,
        origin_task_id: str,
        origin_parent_id: Optional[str],
        fu_author: str,
        follow_ups: Tuple[str, ...],
        now: str,
    ) -> List[Tuple[str, str, Path]]:
        """Allocate sequential FU-<ORIGIN>-NNN IDs and insert follow-up rows inside an active write lock."""
        prefix = self._format_follow_up_prefix(origin_task_id)
        like_pattern = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        suffix_re = re.compile(rf"^{re.escape(prefix)}(\d+)$")

        max_seq = 0
        existing_upper: set[str] = set()
        rows = conn.execute(
            "SELECT id FROM tasks WHERE UPPER(id) LIKE ? ESCAPE '!'",
            (like_pattern,),
        ).fetchall()
        for row in rows:
            cand_upper = str(row["id"]).upper()
            existing_upper.add(cand_upper)
            match = suffix_re.fullmatch(cand_upper)
            if match:
                max_seq = max(max_seq, int(match.group(1)))

        if self.tasks_dir.is_dir():
            for entry in self.tasks_dir.iterdir():
                name = entry.name
                stem = (
                    name[:-3]
                    if name.lower().endswith(".md")
                    else (name if (entry.is_dir() or _is_link_or_junction(entry)) else "")
                )
                if not stem:
                    continue
                cand_upper = stem.upper()
                existing_upper.add(cand_upper)
                match = suffix_re.fullmatch(cand_upper)
                if match:
                    max_seq = max(max_seq, int(match.group(1)))

        tasks_root = self.tasks_dir.resolve()
        rig_parent_root = self.rig_dir.parent.resolve()
        created: List[Tuple[str, str, Path]] = []
        seq = max_seq + 1
        created_payload_base = {
            "archetype": "diff_synthesizer",
            "origin_task_id": origin_task_id,
            "origin_parent_id": origin_parent_id,
        }

        for fu_title in follow_ups:
            while f"{prefix}{seq:03d}".upper() in existing_upper:
                seq += 1
            fu_id = f"{prefix}{seq:03d}"
            seq += 1
            existing_upper.add(fu_id.upper())
            self._validate_task_id(fu_id)
            fu_file = self.get_task_file(fu_id)
            if (
                _is_link_or_junction(self.tasks_dir)
                or _is_link_or_junction(fu_file)
                or _is_link_or_junction(fu_file.parent)
                or _is_link_or_junction(self.tasks_dir / fu_id)
            ):
                raise ValueError(f"Refusing symlink or junction for follow-up task file: {fu_file}")
            resolved_file = fu_file.resolve()
            if not resolved_file.is_relative_to(tasks_root):
                raise ValueError(f"Task file traversal detected: '{fu_file}' escapes '{tasks_root}'")
            context_path = str(resolved_file.relative_to(rig_parent_root))

            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, status, priority, created_at, updated_at,
                    claimed_by, lease_until, version, context_path,
                    parent_id, tags, author, archetype
                ) VALUES (?, ?, 'todo', 100, ?, ?, NULL, NULL, 0, ?, NULL, 'follow-up', ?, 'diff_synthesizer')
                """,
                (fu_id, fu_title, now, now, context_path, fu_author),
            )
            conn.execute(
                """
                INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
                VALUES (?, 'created', ?, ?, ?)
                """,
                (
                    fu_id,
                    fu_author,
                    json.dumps({"title": fu_title, **created_payload_base}),
                    now,
                ),
            )
            created.append((fu_id, fu_title, fu_file))

        return created

    def _commit_completion_and_follow_ups(
        self,
        *,
        task: Dict[str, Any],
        cas_sql: str,
        cas_params: Tuple[Any, ...],
        cas_fail_msg: str,
        event_agent: str,
        audit_agent: str,
        verification_status: str,
        event_payload: Dict[str, Any],
        fu_author: str,
        norm_follow_ups: Tuple[str, ...],
        follow_up_ids_out: Optional[List[str]],
        now: str,
        success_msg: str,
    ) -> Tuple[bool, str]:
        """Execute CAS completion, optional follow-up inserts, scaffolds, and audit logs."""
        task_id = str(task["id"])
        origin_parent_id = task.get("parent_id")
        created_fu_records: List[Tuple[str, str, Path]] = []

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(cas_sql, cas_params)
            if cur.rowcount != 1:
                return False, cas_fail_msg

            if norm_follow_ups:
                created_fu_records = self._insert_follow_up_tasks(
                    conn,
                    origin_task_id=task_id,
                    origin_parent_id=origin_parent_id,
                    fu_author=fu_author,
                    follow_ups=norm_follow_ups,
                    now=now,
                )
                created_fu_ids = [fu_id for fu_id, _, _ in created_fu_records]
                if created_fu_ids:
                    event_payload["follow_up_ids"] = list(created_fu_ids)

            conn.execute(
                """
                INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
                VALUES (?, 'completed', ?, ?, ?)
                """,
                (task_id, event_agent, json.dumps(event_payload), now),
            )
            conn.commit()

        for fu_id, fu_title, fu_file in created_fu_records:
            if not fu_file.exists() and not _is_link_or_junction(fu_file):
                scaffold = (
                    f"# {fu_id}: {fu_title}\n\n"
                    f"- **Parent**: None\n"
                    f"- **Origin**: {task_id}\n"
                    f"- **Archetype**: `diff_synthesizer`\n"
                    f"- **Tags**: follow-up\n"
                    f"- **Author**: {fu_author}\n"
                    f"- **Created**: {now}\n\n"
                    f"## Description\n\n"
                    f"Follow-up task spawned from {task_id}.\n"
                )
                _atomic_write_text(fu_file, scaffold)

        self.log_audit(
            task_id=task_id,
            action="task_completed",
            agent=audit_agent,
            verification_status=verification_status,
            payload=json.dumps(event_payload),
        )

        fu_audit_author = fu_author.replace("\r", " ").replace("\n", " ").strip()
        for fu_id, fu_title, _ in created_fu_records:
            self.log_audit(
                task_id=fu_id,
                action="task_created",
                agent=fu_audit_author,
                verification_status="unverified",
                payload=json.dumps({
                    "title": fu_title,
                    "parent_id": None,
                    "origin_task_id": task_id,
                    "origin_parent_id": origin_parent_id,
                }),
            )

        if follow_up_ids_out is not None and created_fu_records:
            follow_up_ids_out.extend(fu_id for fu_id, _, _ in created_fu_records)

        self.sync_board()
        return True, success_msg

    def complete_task(
        self,
        task_id: str,
        agent: str,
        verifier: Optional[str] = None,
        allow_self_approval: bool = False,
        session_id: Optional[str] = None,
        evidence_paths: Optional[Sequence[str | Path]] = None,
        touched_paths: Optional[Sequence[str | Path]] = None,
        model_id: Optional[str] = None,
        follow_ups: Optional[Sequence[str]] = None,
        follow_up_ids_out: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """Mark task as done enforcing anti-self-approval and intentional friction when required."""
        self._validate_task_id(task_id)
        if not isinstance(allow_self_approval, bool):
            raise ValueError("allow_self_approval must be a bool")
        if not isinstance(agent, str) or not agent.strip() or not agent.replace("\r", " ").replace("\n", " ").isprintable():
            raise ValueError("agent must be a non-empty printable string")
        if verifier is not None:
            if not isinstance(verifier, str):
                raise ValueError("verifier must be a string")
            verifier = verifier.strip() or None
            if verifier is not None and not verifier.isprintable():
                raise ValueError("verifier must be printable")
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip() or not session_id.isprintable()):
            raise ValueError("session_id must be a non-empty printable string")
        resolved_model = resolve_model_id(model_id)
        norm_touched = self._validate_touched_paths(touched_paths)
        norm_follow_ups = self._validate_follow_ups(follow_ups, follow_up_ids_out)
        if norm_follow_ups:
            if _is_link_or_junction(self.tasks_dir):
                raise ValueError(f"Refusing symlink or junction for tasks directory: {self.tasks_dir}")
            if self.tasks_dir.exists() and not self.tasks_dir.is_dir():
                raise ValueError(f"Tasks path is not a directory: {self.tasks_dir}")
        self._ensure_queue_ready()
        self._ensure_audit_ready()
        task = self.get_task(task_id)
        if not task:
            return False, f"Task {task_id} not found"

        if task["status"] == "done":
            if norm_follow_ups:
                return False, f"Cannot attach follow-up tasks to '{task_id}': task is already completed"
            return True, f"Task {task_id} already completed"

        # D3-04: Open child guard
        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, status FROM tasks WHERE parent_id = ? AND status NOT IN ('done', 'cancelled', 'canceled', 'archived')",
                (task_id,),
            )
            open_children = [dict(r) for r in cur.fetchall()]
        if open_children:
            child_ids = ", ".join(r["id"] for r in open_children)
            return False, f"Cannot complete {task_id}: open child tasks remain ({child_ids})"

        now = utc_now_iso()

        # F-02: Active lease ownership check
        if task["status"] != "working":
            return False, f"Cannot complete task '{task_id}': status is '{task['status']}' (must be 'working')"
        if task.get("claimed_by") != agent:
            return False, f"Cannot complete task '{task_id}': claimed by '{task.get('claimed_by')}', not '{agent}'"
        if task.get("lease_until") is not None and task.get("lease_until") < now:
            return False, f"Cannot complete task '{task_id}': lease expired at {task.get('lease_until')}"

        implementer = task.get("claimed_by") or agent
        friction_triggered = self._matches_intentional_friction(task, norm_touched)

        if friction_triggered and allow_self_approval:
            raise PermissionError(
                f"Self-approval forbidden by intentional friction gate on task '{task_id}'."
            )

        # Anti-self-approval rule
        if not allow_self_approval:
            if verifier is None:
                raise PermissionError("Independent --verifier is required unless --allow-self-approval is set.")
            if _is_self_approval(agent, verifier) or _is_self_approval(task.get("claimed_by"), verifier):
                raise PermissionError(
                    f"Self-approval forbidden: verifier '{verifier}' matches agent '{agent}' or claimant '{task.get('claimed_by')}'."
                )

        evidence_records: List[Dict[str, Any]] = []
        if evidence_paths is not None:
            evidence_records = self._validate_and_hash_evidence(evidence_paths)
        if friction_triggered and not evidence_records:
            raise ValueError(
                f"Intentional friction gate requires non-empty SHA-256 evidence on task '{task_id}'."
            )

        seat = derive_seat(agent)
        event_payload: Dict[str, Any] = {
            "verifier": verifier,
            "implementer": implementer,
            "seat": seat,
            "session_id": session_id,
        }
        if resolved_model is not None:
            event_payload["model_id"] = resolved_model
        if evidence_records:
            event_payload["evidence"] = evidence_records
        version = task.get("version", 0)

        return self._commit_completion_and_follow_ups(
            task=task,
            cas_sql="""
                UPDATE tasks
                SET status = 'done',
                    lease_until = NULL,
                    version = version + 1,
                    updated_at = ?
                WHERE id = ?
                  AND status = 'working'
                  AND claimed_by = ?
                  AND version = ?
                  AND (lease_until IS NULL OR lease_until >= ?)
            """,
            cas_params=(now, task_id, agent, version, now),
            cas_fail_msg=f"Concurrent modification detected or lease lost on task '{task_id}' (expected version {version})",
            event_agent=agent,
            audit_agent=agent.replace("\r", " ").replace("\n", " "),
            verification_status="verified" if verifier else "unverified",
            event_payload=event_payload,
            fu_author=(verifier or agent).strip(),
            norm_follow_ups=norm_follow_ups,
            follow_up_ids_out=follow_up_ids_out,
            now=now,
            success_msg="Task completed successfully",
        )

    def submit_for_review(
        self,
        task_id: str,
        agent: str,
        summary: str,
        verification_cmd: Optional[str] = None,
        diffstat: Optional[str] = None,
        evidence_paths: Optional[Sequence[str | Path]] = None,
        session_id: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Transition a working task to review with verification receipt and SHA-256 evidence."""
        self._validate_task_id(task_id)
        if not isinstance(agent, str) or not agent.strip() or not agent.replace("\r", " ").replace("\n", " ").isprintable():
            raise ValueError("agent must be a non-empty printable string")
        if not isinstance(summary, str) or not summary.strip() or not summary.replace("\t", " ").replace("\n", " ").replace("\r", " ").isprintable():
            raise ValueError("summary must be a non-empty printable string")
        if verification_cmd is not None:
            if not isinstance(verification_cmd, str) or not verification_cmd.strip() or not verification_cmd.replace("\t", " ").replace("\n", " ").replace("\r", " ").isprintable():
                raise ValueError("verification_cmd must be a non-empty printable string")
            verification_cmd = verification_cmd.strip()
        if diffstat is not None:
            if not isinstance(diffstat, str) or not diffstat.strip() or not diffstat.replace("\t", " ").replace("\n", " ").replace("\r", " ").isprintable():
                raise ValueError("diffstat must be a non-empty printable string")
            diffstat = diffstat.strip()
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip() or not session_id.isprintable()):
            raise ValueError("session_id must be a non-empty printable string")
        resolved_model = resolve_model_id(model_id)

        self._ensure_queue_ready()
        self._ensure_audit_ready()
        task = self.get_task(task_id)
        if not task:
            return False, f"Task {task_id} not found"

        now = utc_now_iso()
        if task["status"] != "working":
            return False, f"Cannot submit task '{task_id}' for review: status is '{task['status']}' (must be 'working')"
        if task.get("claimed_by") != agent:
            return False, f"Cannot submit task '{task_id}' for review: claimed by '{task.get('claimed_by')}', not '{agent}'"
        if task.get("lease_until") is not None and task.get("lease_until") < now:
            return False, f"Cannot submit task '{task_id}' for review: lease expired at {task.get('lease_until')}"

        evidence_records: List[Dict[str, Any]] = []
        if evidence_paths is not None:
            evidence_records = self._validate_and_hash_evidence(evidence_paths)

        event_payload: Dict[str, Any] = {
            "summary": summary.strip(),
            "implementer": task.get("claimed_by") or agent,
            "seat": derive_seat(agent),
            "session_id": session_id,
        }
        if resolved_model is not None:
            event_payload["model_id"] = resolved_model
        if verification_cmd is not None:
            event_payload["verification_cmd"] = verification_cmd
        if diffstat is not None:
            event_payload["diffstat"] = diffstat
        if evidence_records:
            event_payload["evidence"] = evidence_records

        version = task.get("version", 0)
        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE tasks
                SET status = 'review',
                    lease_until = NULL,
                    version = version + 1,
                    updated_at = ?
                WHERE id = ?
                  AND status = 'working'
                  AND claimed_by = ?
                  AND version = ?
                  AND (lease_until IS NULL OR lease_until >= ?)
                """,
                (now, task_id, agent, version, now),
            )
            if cur.rowcount != 1:
                return False, f"Concurrent modification detected or lease lost on task '{task_id}' (expected version {version})"

            conn.execute(
                """
                INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
                VALUES (?, 'review_submitted', ?, ?, ?)
                """,
                (task_id, agent, json.dumps(event_payload), now),
            )
            conn.commit()

        self.log_audit(
            task_id=task_id,
            action="task_review_submitted",
            agent=agent.replace("\r", " ").replace("\n", " "),
            verification_status="pending_review",
            payload=json.dumps(event_payload),
        )
        self.sync_board()
        return True, "Task submitted for review"

    def approve_review(
        self,
        task_id: str,
        verifier: str,
        session_id: Optional[str] = None,
        evidence_paths: Optional[Sequence[str | Path]] = None,
        touched_paths: Optional[Sequence[str | Path]] = None,
        model_id: Optional[str] = None,
        follow_ups: Optional[Sequence[str]] = None,
        follow_up_ids_out: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """Approve a task in review and transition it to done with independent verifier enforcement."""
        self._validate_task_id(task_id)
        if not isinstance(verifier, str) or not verifier.strip() or not verifier.replace("\r", " ").replace("\n", " ").isprintable():
            raise ValueError("verifier must be a non-empty printable string")
        verifier = verifier.strip()
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip() or not session_id.isprintable()):
            raise ValueError("session_id must be a non-empty printable string")
        resolved_model = resolve_model_id(model_id)
        norm_touched = self._validate_touched_paths(touched_paths)
        norm_follow_ups = self._validate_follow_ups(follow_ups, follow_up_ids_out)
        if norm_follow_ups:
            if _is_link_or_junction(self.tasks_dir):
                raise ValueError(f"Refusing symlink or junction for tasks directory: {self.tasks_dir}")
            if self.tasks_dir.exists() and not self.tasks_dir.is_dir():
                raise ValueError(f"Tasks path is not a directory: {self.tasks_dir}")

        self._ensure_queue_ready()
        self._ensure_audit_ready()
        task = self.get_task(task_id)
        if not task:
            return False, f"Task {task_id} not found"

        if task["status"] == "done":
            if norm_follow_ups:
                return False, f"Cannot attach follow-up tasks to '{task_id}': task is already completed"
            return True, f"Task {task_id} already completed"

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, status FROM tasks WHERE parent_id = ? AND status NOT IN ('done', 'cancelled', 'canceled', 'archived')",
                (task_id,),
            )
            open_children = [dict(r) for r in cur.fetchall()]
        if open_children:
            child_ids = ", ".join(r["id"] for r in open_children)
            return False, f"Cannot complete {task_id}: open child tasks remain ({child_ids})"

        if task["status"] != "review":
            return False, f"Cannot approve task '{task_id}': status is '{task['status']}' (must be 'review')"

        implementers: set[str] = set()
        if task.get("claimed_by"):
            implementers.add(str(task["claimed_by"]))
        latest_review_evidence: List[Dict[str, Any]] = []
        latest_implementer: Optional[str] = task.get("claimed_by")

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT event_type, agent, payload FROM task_events WHERE task_id = ? ORDER BY id DESC",
                (task_id,),
            )
            for row in cur.fetchall():
                ev_type = row["event_type"]
                if ev_type == "blocked":
                    break
                if ev_type in ("claimed", "review_submitted"):
                    if row["agent"]:
                        implementers.add(str(row["agent"]))
                    raw_payload = row["payload"]
                    if raw_payload:
                        try:
                            parsed = json.loads(raw_payload)
                        except json.JSONDecodeError:
                            parsed = None
                        if isinstance(parsed, dict):
                            if parsed.get("implementer"):
                                implementers.add(str(parsed["implementer"]))
                                if latest_implementer is None:
                                    latest_implementer = str(parsed["implementer"])
                            if ev_type == "review_submitted" and not latest_review_evidence:
                                ev_list = parsed.get("evidence")
                                if isinstance(ev_list, list):
                                    valid_ev = [
                                        item for item in ev_list
                                        if isinstance(item, dict)
                                        and isinstance(item.get("path"), str)
                                        and isinstance(item.get("sha256"), str)
                                        and SHA256_HEX_PATTERN.fullmatch(item["sha256"])
                                    ]
                                    if valid_ev:
                                        latest_review_evidence = valid_ev

        for imp in sorted(implementers):
            if _is_self_approval(imp, verifier):
                raise PermissionError(
                    f"Self-approval forbidden: verifier '{verifier}' matches pre-review implementer '{imp}'."
                )

        if evidence_paths is not None:
            evidence_records = self._validate_and_hash_evidence(evidence_paths)
        else:
            evidence_records = latest_review_evidence

        if self._matches_intentional_friction(task, norm_touched) and not evidence_records:
            raise ValueError(
                f"Intentional friction gate requires non-empty SHA-256 evidence on task '{task_id}'."
            )

        now = utc_now_iso()
        version = task.get("version", 0)
        event_payload: Dict[str, Any] = {
            "verifier": verifier,
            "implementer": latest_implementer or verifier,
            "seat": derive_seat(verifier),
            "session_id": session_id,
        }
        if resolved_model is not None:
            event_payload["model_id"] = resolved_model
        if evidence_records:
            event_payload["evidence"] = evidence_records

        return self._commit_completion_and_follow_ups(
            task=task,
            cas_sql="""
                UPDATE tasks
                SET status = 'done',
                    lease_until = NULL,
                    version = version + 1,
                    updated_at = ?
                WHERE id = ?
                  AND status = 'review'
                  AND version = ?
            """,
            cas_params=(now, task_id, version),
            cas_fail_msg=f"Concurrent modification detected on task '{task_id}' (expected version {version})",
            event_agent=verifier,
            audit_agent=verifier.replace("\r", " ").replace("\n", " "),
            verification_status="verified",
            event_payload=event_payload,
            fu_author=verifier.strip(),
            norm_follow_ups=norm_follow_ups,
            follow_up_ids_out=follow_up_ids_out,
            now=now,
            success_msg="Task review approved and completed",
        )

    def get_next_workflow_step(self, task_id: str) -> Dict[str, Any]:
        """Compute next workflow step under parent task or epic enforcing workflow rails."""
        self._ensure_queue_ready()
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
                matching.sort(key=lambda s: (int(s.get("priority") if s.get("priority") is not None else 100), s.get("created_at") or "", s.get("id") or ""))
                selected = matching[0]
                break

        if not selected:
            todo_siblings.sort(key=lambda s: (int(s.get("priority") if s.get("priority") is not None else 100), s.get("created_at") or "", s.get("id") or ""))
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
        self._validate_task_id(task_id)
        header_err = _validate_audit_header_fields(task_id, "task_blocked", agent, "failed")
        if (
            header_err is not None
            or not isinstance(reason, str)
            or not reason.strip()
            or not reason.replace("\t", " ").replace("\n", " ").replace("\r", " ").isprintable()
        ):
            raise ValueError(header_err or "reason must be a non-empty printable string without control characters")
        self._ensure_queue_ready()
        self._ensure_audit_ready()
        task = self.get_task(task_id)
        if not task:
            raise KeyError(f"Task '{task_id}' not found.")

        now = utc_now_iso()
        expected_version = task.get("version", 0)

        # Block terminal overwrites
        if task["status"] in ("done", "cancelled", "canceled", "archived"):
            raise PermissionError(f"Cannot fail task '{task_id}' in terminal status '{task['status']}'.")

        # Block stale-claimant and NULL-lease non-owner split-brain overwrites (F-02)
        claimed_by = task.get("claimed_by")
        lease_until = task.get("lease_until")
        if (
            task["status"] == "working"
            and claimed_by is not None
            and str(claimed_by).strip() != ""
            and claimed_by != agent
            and (lease_until is None or not str(lease_until).strip() or lease_until >= now)
        ):
            raise PermissionError(
                f"Task '{task_id}' is actively leased by '{claimed_by}'; '{agent}' cannot fail it."
            )

        with self.get_queue_conn() as conn:
            cur = conn.cursor()
            if task["status"] == "todo" and task.get("claimed_by") is None:
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = 'blocked',
                        lease_until = NULL,
                        version = version + 1,
                        updated_at = ?
                    WHERE id = ?
                      AND version = ?
                      AND status = 'todo'
                      AND claimed_by IS NULL
                    """,
                    (now, task_id, expected_version),
                )
            else:
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = 'blocked',
                        lease_until = NULL,
                        version = version + 1,
                        updated_at = ?
                    WHERE id = ?
                      AND version = ?
                      AND status NOT IN ('done', 'cancelled', 'canceled', 'archived')
                      AND NOT (
                          status = 'working'
                          AND claimed_by IS NOT NULL
                          AND TRIM(claimed_by) != ''
                          AND claimed_by != ?
                          AND (lease_until IS NULL OR TRIM(lease_until) = '' OR lease_until >= ?)
                      )
                    """,
                    (now, task_id, expected_version, agent, now),
                )

            if cur.rowcount != 1:
                raise PermissionError(
                    f"Concurrent modification detected on task '{task_id}' (expected version {expected_version})."
                )

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
        except OSError as exc:
            logging.getLogger(__name__).warning("Failed to persist trap for %s: %s", task_id, exc)
        return True

    def _record_blocker_trap(
        self,
        task_id: str,
        agent: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        """Persist deduplicated blocker trap candidates into traps.json (failure-soft)."""
        with self._lock:
            return self._record_blocker_trap_unlocked(task_id=task_id, agent=agent, reason=reason)

    def _record_blocker_trap_unlocked(self, task_id: str, agent: str, reason: str) -> Optional[Dict[str, Any]]:
        clean_reason = (reason or "").strip() or "unspecified blocker"
        trigger_pattern = _normalize_blocker_signature(clean_reason)
        traps_path = self.rig_dir / "traps.json"
        now = utc_now_iso()
        trace_entry = f"Task {task_id} ({agent})"

        with trap_file_lock(traps_path):
            doc: Dict[str, Any] = {"version": "1.0.0", "traps": []}
            if traps_path.exists():
                try:
                    loaded = json.loads(traps_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("traps"), list):
                        doc = loaded
                        doc.setdefault("version", "1.0.0")
                except (OSError, ValueError):
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
                raw_occ, raw_traces = matched.get("occurrences"), matched.get("source_traces")
                occurrences = (int(raw_occ) if not isinstance(raw_occ, bool) and (isinstance(raw_occ, int) or (isinstance(raw_occ, float) and math.isfinite(raw_occ))) and raw_occ >= 0 else 0) + 1
                matched["occurrences"] = occurrences
                matched["last_seen_at"] = now
                traces = [str(x) for x in raw_traces] if isinstance(raw_traces, list) else []
                traces.append(trace_entry)
                matched["source_traces"] = traces[-10:]
                matched["promotion_candidate"] = occurrences >= 3
                result = matched
            else:
                max_num = 0
                for entry in traps:
                    if isinstance(entry, dict):
                        m = re.match(r"^TRAP-(\d+)$", str(entry.get("id") or entry.get("trap_id") or ""))
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

            _atomic_write_text(traps_path, json.dumps(doc, indent=2) + "\n")
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
        self._validate_task_id(task_id)
        header_err = _validate_audit_header_fields(task_id, action, agent, verification_status)
        if header_err is not None or not isinstance(payload, str):
            raise ValueError(header_err or "payload must be a string")
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._lock:
            if not self.audit_db_path.is_file():
                raise FileNotFoundError(f"Uninitialized audit database: {self.audit_db_path}")
            return self._log_audit_unlocked(task_id, action, agent, verification_status, payload_hash)

    def _log_audit_unlocked(self, task_id: str, action: str, agent: str, verification_status: str, payload_hash: str) -> str:
        now = utc_now_iso()

        conn = sqlite3.connect(str(self.audit_db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            if not (tbl := cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone()) or "VIRTUAL TABLE" in (tbl["sql"] or "").upper():
                raise sqlite3.OperationalError("audit_log table does not exist")
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
            except sqlite3.Error as rb_exc:
                logging.getLogger(__name__).debug("Audit rollback failed: %s", rb_exc)
            raise
        finally:
            conn.close()

    def verify_audit_chain(self) -> Tuple[bool, str]:
        """Validate hash chain link integrity across all entries in audit_log supporting Tier A and Tier B."""
        if not self.audit_db_path.is_file():
            return False, "audit.db does not exist"
        tier_b_cols = ("timestamp", "task_id", "action", "agent", "verification_status", "payload_hash", "previous_hash")
        try:
            with self.get_audit_conn() as conn:
                cur = conn.cursor()
                master = cur.execute("SELECT type, sql FROM sqlite_master WHERE name='audit_log'").fetchone()
                if not master or master["type"] != "table" or "VIRTUAL TABLE" in (master["sql"] or "").upper():
                    return False, "audit_log table does not exist" if not master else "audit_log must be a base table"
                cols = {r["name"] for r in cur.execute("PRAGMA table_info(audit_log)").fetchall()}
                cols_lower = {c.lower() for c in cols}
                hash_col = "entry_hash" if "entry_hash" in cols else "hash"
                is_tier_b = bool(cols_lower & TIER_B_AUDIT_MARKERS)
                required = {"id", hash_col, *(tier_b_cols if is_tier_b else ("previous_hash",))}
                if (is_tier_b and hash_col != "entry_hash") or not required.issubset(cols):
                    return False, "audit_log schema invalid"

                cur.execute("SELECT * FROM audit_log ORDER BY id ASC")
                rows = [dict(r) for r in cur.fetchall()]
                seq_vals = [r["seq"] for r in cur.execute("SELECT seq FROM sqlite_sequence WHERE name='audit_log'").fetchall()] if cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
                ).fetchone() else []
        except sqlite3.DatabaseError as exc:
            return False, f"audit.db database error: {exc}"

        if len(seq_vals) > 1 or ("AUTOINCREMENT" in (master["sql"] or "").upper() and rows and not seq_vals):
            return False, "Audit sequence metadata corrupted"
        if seq_vals and (isinstance(seq_vals[0], bool) or not isinstance(seq_vals[0], int) or seq_vals[0] < 1 or seq_vals[0] != len(rows)):
            return False, f"Invalid or tampered audit sequence: seq={seq_vals[0]!r}, rows={len(rows)}"
        if not rows:
            return True, "Audit log is empty"

        allowed_genesis = (GENESIS_HASH,) if is_tier_b else (GENESIS_HASH, None, "")
        prev_hash: Optional[str] = None
        for i, row in enumerate(rows):
            if isinstance(row.get("id"), bool) or not isinstance(row.get("id"), int) or row.get("id") != i + 1:
                return False, f"Non-contiguous audit ID: expected {i + 1}, got {row.get('id')!r}"
            current_hash = row.get(hash_col)
            previous_hash = row.get("previous_hash")

            if is_tier_b and not all(isinstance(row.get(c), str) for c in tier_b_cols):
                return False, f"Invalid Tier B header column type at ID {row['id']}"

            if i == 0:
                if previous_hash not in allowed_genesis:
                    return (
                        False,
                        f"Hash break at genesis ID {row['id']}: expected genesis, got {previous_hash}",
                    )
            else:
                if not isinstance(previous_hash, str) or not SHA256_HEX_PATTERN.fullmatch(previous_hash):
                    return False, f"Invalid previous_hash format at ID {row['id']}"
                if previous_hash != prev_hash:
                    return (
                        False,
                        f"Hash break at ID {row['id']}: expected previous {prev_hash}, got {previous_hash}",
                    )

            if is_tier_b:
                if not _is_canonical_utc_iso(row["timestamp"]):
                    return False, f"Invalid Tier B timestamp at ID {row['id']}"
                header_err = _validate_audit_header_fields(row["task_id"], row["action"], row["agent"], row["verification_status"])
                if header_err is not None:
                    return False, f"Invalid Tier B header at ID {row['id']}: {header_err}"
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
                if not isinstance(row.get("payload_hash"), str) or not SHA256_HEX_PATTERN.fullmatch(row["payload_hash"]):
                    return False, f"Invalid payload_hash format at ID {row['id']}"

            if not isinstance(current_hash, str) or not SHA256_HEX_PATTERN.fullmatch(current_hash):
                return False, f"Invalid hash format at ID {row['id']}"

            prev_hash = current_hash

        return True, f"Verified {len(rows)} audit entries with intact hash chain"

    def sync_board(self) -> Path:
        """Render markdown board partitioned by status into Task_Board.md."""
        with self._lock:
            return self._sync_board_unlocked()

    def _sync_board_unlocked(self) -> Path:
        self._ensure_queue_ready()
        tasks = self.list_tasks()
        sections: Dict[str, List[Dict[str, Any]]] = {
            "todo": [],
            "working": [],
            "done": [],
            "blocked": [],
        }

        for t in tasks:
            st = "cancelled" if t["status"] in ("cancelled", "canceled", "archived") else t["status"]
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
        if sections.get("review"):
            status_display.insert(1, ("review", "Pending Review / Verification"))
        if sections.get("cancelled"):
            status_display.append(("cancelled", "Cancelled / Archived"))

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
                    claimant = _escape_md_cell(item.get("claimed_by") or "-")
                    arch = _escape_md_cell(item.get("archetype") or "diff_synthesizer")
                    clean_id = _escape_md_cell(item["id"])
                    clean_title = _escape_md_cell(item["title"])
                    clean_updated = _escape_md_cell(item["updated_at"])
                    content.append(f"| `{clean_id}` | {clean_title} | `{arch}` | {claimant} | {clean_updated} |")
            content.append("")

        _atomic_write_text(self.board_path, "\n".join(content) + "\n")
        return self.board_path

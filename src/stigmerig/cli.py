"""Command-line interface for StigmeRig."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from stigmerig.db import TASK_ID_PATTERN, StigmeRigDB, _atomic_write_text, _is_self_approval, resolve_rig_dir
from stigmerig.traps import add_traps_parser, run_traps_command, validate_traps_cli_args

VALID_TASK_STATUSES = frozenset({"todo", "working", "review", "done", "blocked", "cancelled", "canceled", "archived"})


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _has_ctrl(text: str, *, allow_ws: bool = False) -> bool:
    return any(not c.isprintable() and (not allow_ws or c not in "\t\n\r") for c in text)


def _bad_part(part: str, *, out: bool = False) -> bool:
    s = part.rstrip(". ").lower()
    return s == ".git" or (out and (s in {".worktrees", ".env"} or s.startswith(".env.")))


def _validate_cli_args(args: argparse.Namespace) -> None:
    """Validate CLI flag invariants before filesystem or database access."""
    for name, val in vars(args).items():
        for item in (val if isinstance(val, (list, tuple)) else (val,)):
            if isinstance(item, (str, os.PathLike)):
                s_item = str(os.fspath(item))
                if "\x00" in s_item:
                    raise ValueError(f"--{name.replace('_', '-')} must not contain embedded NUL bytes")
                if name not in ("id", "task", "parent") and _has_ctrl(s_item, allow_ws=name in ("title", "reason", "payload", "positive_fixture", "negative_fixture", "remediation", "forbidden", "summary", "verification_cmd", "diffstat")):
                    raise ValueError(f"--{name.replace('_', '-')} must not contain control or non-printable Unicode characters")
    dir_checks = [(f"--{f.replace('_', '-')}", getattr(args, f, None)) for f in ("rig_dir", "path")]
    env_root = os.environ.get("STIGMERIG_ROOT") if dir_checks[0][1] is None and dir_checks[1][1] is None else None
    if env_root is not None:
        dir_checks.append(("STIGMERIG_ROOT", env_root))
    for flag, raw_dir in dir_checks:
        if raw_dir is not None and (not (raw_str := str(os.fspath(raw_dir))).strip() or _has_ctrl(raw_str)):
            raise ValueError(f"{flag} must not be empty, whitespace, or contain control characters")
    ttl = getattr(args, "ttl", None)
    if ttl is not None:
        if isinstance(ttl, bool) or ttl <= 0:
            raise ValueError("--ttl must be a positive integer (> 0)")
        try:
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=ttl)
        except OverflowError as exc:
            raise ValueError("--ttl exceeds maximum supported timestamp range") from exc
    priority = getattr(args, "priority", None)
    if priority is not None and (isinstance(priority, bool) or not isinstance(priority, int) or not (-(1 << 63) <= priority <= (1 << 63) - 1)):
        raise ValueError("--priority must be a 64-bit signed integer")
    for field in ("title", "agent", "reason", "action", "status", "parent", "author", "archetype", "verifier", "session_id", "model", "output", "summary", "verification_cmd", "diffstat"):
        val = getattr(args, field, None)
        if val is not None and not str(os.fspath(val) if isinstance(val, os.PathLike) else val).strip():
            raise ValueError(f"--{field.replace('_', '-')} must not be empty or whitespace")
    tags = getattr(args, "tags", None)
    if tags is not None and any(not part.strip() for part in tags.split(",")):
        raise ValueError("--tags must not be empty or contain whitespace-only entries")
    evidence = getattr(args, "evidence", None)
    ev_strs: list[str] = []
    if evidence is not None:
        items = [evidence] if isinstance(evidence, (str, os.PathLike)) else evidence
        if not isinstance(items, (list, tuple)) or not items or any(not isinstance(item, (str, os.PathLike)) or not str(os.fspath(item)).strip() for item in items):
            raise ValueError("--evidence must not be empty or whitespace")
        ev_strs = [str(os.fspath(item)) for item in items]
        if any(_has_ctrl(s) for s in ev_strs):
            raise ValueError("--evidence must not contain CR/LF, control, or ANSI escape characters")
    for id_field in ("id", "task", "parent"):
        tid = getattr(args, id_field, None)
        if tid is not None and (not isinstance(tid, str) or _has_ctrl(tid) or not TASK_ID_PATTERN.fullmatch(tid)):
            raise ValueError(f"Invalid task_id '{tid}': must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$")
    if getattr(args, "parent", None) is not None and getattr(args, "parent", None) == getattr(args, "id", None):
        raise ValueError("--parent must not equal --id")
    cmd, sub = getattr(args, "command", None), getattr(args, "subcommand", None)
    if cmd == "task" and sub in ("done", "review", "approve") and getattr(args, "model", None) is None:
        if (env_m := os.environ.get("STIGMERIG_MODEL")) is not None and env_m != "" and (not env_m.strip() or _has_ctrl(env_m)):
            raise ValueError("STIGMERIG_MODEL must not be empty, whitespace, or contain control characters")
    if ":" in (getattr(args, "action", None) or "") or (cmd == "audit" and ":" in (getattr(args, "status", None) or "")):
        raise ValueError("audit log --action and --status must not contain ':'")
    if cmd == "task" and sub == "list" and getattr(args, "status", None) is not None and args.status not in VALID_TASK_STATUSES:
        raise ValueError(f"Invalid --status '{args.status}'; must be one of {sorted(VALID_TASK_STATUSES)}")
    if cmd == "task" and sub == "done" and not getattr(args, "allow_self_approval", False):
        if getattr(args, "verifier", None) is None:
            raise ValueError("Independent --verifier is required unless --allow-self-approval is set")
        if _is_self_approval(getattr(args, "agent", None), args.verifier):
            raise ValueError(f"Self-approval forbidden: --verifier '{args.verifier}' matches --agent '{args.agent}'")
    if dir_checks[0][1] is None and dir_checks[1][1] is None and env_root is None:
        cwd_rig = str(Path.cwd() / ".stigmerig")
        if not cwd_rig.strip() or _has_ctrl(cwd_rig):
            raise ValueError(".stigmerig must not be empty, whitespace, or contain control characters")
        dir_checks.append((".stigmerig", cwd_rig))
    res_dir: Optional[Path] = None
    for flag, raw_dir in dir_checks:
        if raw_dir is not None:
            raw_str = str(os.fspath(raw_dir))
            dir_path, abs_dir = Path(raw_str), Path(os.path.abspath(raw_str))
            res_dir, anc_res = dir_path.resolve(), tuple(a.resolve() for a in dir_path.parents)
            if any(_bad_part(p) for c in (dir_path, abs_dir, res_dir, *anc_res) for p in c.parts):
                raise ValueError(f"{flag} '{dir_path}' must not target or traverse inside '.git'")
            if any((a.exists() or _is_link(a)) and not a.is_dir() for a in (dir_path, *dir_path.parents, abs_dir, *abs_dir.parents, res_dir, *res_dir.parents)):
                raise ValueError(f"{flag} '{dir_path}' targets or traverses an existing non-directory file")
    assert res_dir is not None
    if ev_strs:
        ws_root, ev_keys = res_dir.parent, []
        ws_ancs = {ws_root, *ws_root.parents}
        for raw_ev in ev_strs:
            ev_p = Path(raw_ev)
            cand = ev_p if ev_p.is_absolute() else ws_root / ev_p
            abs_cand = Path(os.path.abspath(str(cand)))
            if _is_link(cand) or _is_link(abs_cand) or any(_is_link(a) for a in (*cand.parents, *abs_cand.parents) if a not in ws_ancs):
                raise ValueError(f"--evidence path '{raw_ev}' must not be or traverse a symlink or junction")
            ev_res = cand.resolve()
            anc_res: list[Path] = []
            for a in cand.parents:
                if a in ws_ancs:
                    continue
                r = a.resolve()
                if ".." not in a.parts and r in ws_ancs:
                    continue
                anc_res.append(r)
            if ev_res == ws_root or not ev_res.is_relative_to(ws_root) or (".." in ev_p.parts and any(not r.is_relative_to(ws_root) for r in anc_res)):
                raise ValueError(f"--evidence path '{raw_ev}' escapes workspace root '{ws_root}'")
            sub_ancs = [r for r in anc_res if r != ws_root and r.is_relative_to(ws_root)]
            if ev_res.is_relative_to(res_dir) or any(a.is_relative_to(res_dir) for a in sub_ancs):
                raise ValueError(f"--evidence path '{raw_ev}' must not target active rig '{res_dir}'")
            rel_parts = (*(p for c in (ev_res, *sub_ancs) for p in c.relative_to(ws_root).parts), *(() if ev_p.is_absolute() else ev_p.parts))
            if any(_bad_part(p, out=True) or (s := p.rstrip(". ").lower()) == ".stigmerig" or s.startswith(".env") for p in rel_parts) or ev_res.name.lower().startswith(("queue.db", "audit.db")):
                raise ValueError(f"--evidence path '{raw_ev}' targets denied path ('.git', '.worktrees', '.env*', or rig state)")
            ev_keys.append(os.path.normcase(str(ev_res)))
        if len(set(ev_keys)) != len(ev_keys):
            raise ValueError("Duplicate --evidence paths resolving to the same target are not allowed")
    if getattr(args, "output", None) is not None:
        _write_output(args.output, None, res_dir)
    if cmd == "traps":
        validate_traps_cli_args(args, res_dir)


def _write_output(raw_path: str | os.PathLike[str], content: Optional[str] = None, rig_dir: Optional[Path | str] = None) -> None:
    """Write CLI output to target file, creating missing parents and rejecting directories."""
    raw_str = str(os.fspath(raw_path))
    if not raw_str.strip() or _has_ctrl(raw_str):
        raise ValueError("--output path must not be empty, whitespace, or contain control or ANSI escape characters")
    target, abs_target = Path(raw_str), Path(os.path.abspath(raw_str))
    if _is_link(target) or _is_link(abs_target):
        raise ValueError(f"Output path '{target}' must not be a symlink or junction")
    resolved, parent_resolved = target.resolve(), target.parent.resolve()
    if any(_bad_part(p, out=True) for c in (target, abs_target, resolved, parent_resolved, *(a.resolve() for a in target.parents)) for p in c.parts):
        raise ValueError(f"Output path '{target}' must not target '.git', '.worktrees', or '.env' paths")
    if rig_dir is not None:
        rig_resolved = Path(rig_dir).resolve()
        if resolved.is_relative_to(rig_resolved) or parent_resolved.is_relative_to(rig_resolved):
            raise ValueError(f"Output path '{target}' must not reside inside active rig '{rig_resolved}'")
    if target.exists() and target.is_dir():
        raise IsADirectoryError(f"Output path '{target}' is an existing directory")
    if any((a.exists() or _is_link(a)) and not a.is_dir() for a in (*target.parents, *abs_target.parents, *resolved.parents)):
        raise ValueError(f"Output path '{target}' traverses an existing non-directory file")
    if content is not None:
        _atomic_write_text(target, content)


def _claim_failure_reason(rig: StigmeRigDB, task_id: str, agent: str) -> str:
    if not (task := rig.get_task(task_id)):
        return "task not found"
    if (status := task.get("status")) not in ("todo", "working"):
        return f"non-claimable status '{status}'"
    h, lu = task.get("claimed_by"), task.get("lease_until")
    return f"active lease held by '{h}' until {lu}" if h and h != agent and lu and lu >= datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") else "CAS version conflict"


def _prune_worktrees(
    rig: StigmeRigDB, repo_root: Path, delete_branches: bool = False, dry_run: bool = False, wt_mgr: Optional[Any] = None
) -> dict[str, object]:
    from stigmerig.wave import TERMINAL_STATUSES, WorktreeManager, _git_dir, _in_progress_git_op, _is_reparse_link, _run_checked, _symbolic_head
    mgr = wt_mgr if wt_mgr is not None else WorktreeManager(repo_root=repo_root)

    def _own_adm(adm: Path, wt: Path) -> bool:
        if not (adm.exists() or _is_reparse_link(adm)):
            return False
        gf = adm / "gitdir"
        if not (gf.exists() or _is_reparse_link(gf)):
            return True
        try:
            raw = gf.read_text(encoding="utf-8", errors="replace").strip() if gf.is_file() and not _is_reparse_link(gf) else ""
            return bool(raw) and os.path.normcase(str((adm / raw).resolve())) == os.path.normcase(str((wt / ".git").resolve()))
        except (OSError, ValueError):
            return False

    with mgr._lock:
        if _is_reparse_link(mgr.worktrees_dir):
            raise ValueError(f"Worktrees directory '{mgr.worktrees_dir}' must not be a symlink or junction")
        tasks, gd_wts = rig.list_tasks(), _git_dir(mgr.repo_root) / "worktrees"
        protected = {tid.strip().upper() for t in tasks if isinstance(tid := t.get("id"), str) and t.get("status") not in TERMINAL_STATUSES}
        porcelain = _run_checked(["git", "worktree", "list", "--porcelain"], cwd=str(mgr.repo_root)).stdout
        wt_rows = [
            (
                os.path.normcase(str((p := Path(lns[0][9:])).resolve())),
                p,
                next((ln[7:] for ln in lns[1:] if ln.startswith("branch ")), ""),
                any(ln == "locked" or ln.startswith("locked ") for ln in lns[1:]),
                any(ln == "prunable" or ln.startswith("prunable ") for ln in lns[1:]),
            )
            for blk in porcelain.replace("\r\n", "\n").strip().split("\n\n")
            if (lns := [ln for ln in blk.splitlines() if ln]) and lns[0].startswith("worktree ")
        ]
        wt_keys, locked_keys = {k for k, *_ in wt_rows}, {k for k, _, _, lck, _ in wt_rows if lck}
        candidates: list[tuple[str, str, Path, bool]] = []
        pruned_keys: set[str] = set()
        for tid in dict.fromkeys(
            tid for t in tasks
            if isinstance(tid := t.get("id"), str) and TASK_ID_PATTERN.fullmatch(tid) and t.get("status") in TERMINAL_STATUSES and tid.upper() not in protected
        ):
            clean_id, wt_path = mgr._resolve_worktree_target(tid)
            wt_key, adm_gd = os.path.normcase(str(wt_path)), gd_wts / clean_id
            own_adm = _own_adm(adm_gd, wt_path)
            gds = list(dict.fromkeys([
                *([adm_gd] if own_adm else []),
                *([_git_dir(wt_path)] if wt_path.is_dir() and not _is_reparse_link(wt_path) and not _is_reparse_link(wt_path / ".git") and (wt_path / ".git").exists() else []),
            ]))
            if wt_key in locked_keys or any((gd / "locked").exists() or _is_reparse_link(gd / "locked") or _in_progress_git_op(gd) for gd in gds):
                continue
            has_wt = wt_path.exists() or _is_reparse_link(wt_path) or own_adm or wt_key in wt_keys
            if has_wt:
                pruned_keys.add(wt_key)
            candidates.append((tid, clean_id, wt_path, has_wt))
        active_refs = {_symbolic_head(mgr.repo_root)} - {""}
        for k, p, br, lck, prn in wt_rows:
            if k not in pruned_keys and (not prn or lck):
                if br:
                    active_refs.add(br)
                if p.is_dir() and not _is_reparse_link(p) and not _is_reparse_link(p / ".git") and (p / ".git").exists():
                    wgd = _git_dir(p)
                    active_refs.update(r for s in ("rebase-merge", "rebase-apply") if (hn := wgd / s / "head-name").is_file() and (r := hn.read_text(encoding="utf-8").strip()))
        branches = set(_run_checked(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/task/"], cwd=str(mgr.repo_root)).stdout.splitlines()) if delete_branches else set()
        plan = [(tid, clean_id, wt_path, has_wt, [b for b in dict.fromkeys((f"task/{clean_id}", f"task/{tid}")) if b in branches and f"refs/heads/{b}" not in active_refs]) for tid, clean_id, wt_path, has_wt in candidates]
        removed_wts: list[str] = []
        deleted_brs: list[str] = []
        if dry_run:
            for _, clean_id, _, has_wt, cand_brs in plan:
                if has_wt and clean_id not in removed_wts:
                    removed_wts.append(clean_id)
                deleted_brs.extend(b for b in cand_brs if b not in deleted_brs)
        else:
            for tid, clean_id, wt_path, has_wt, _ in plan:
                if has_wt:
                    mgr.remove_worktree(tid, delete_branch=False)
                    if not wt_path.exists() and not _is_reparse_link(wt_path) and not _own_adm(gd_wts / clean_id, wt_path) and clean_id not in removed_wts:
                        removed_wts.append(clean_id)
            _run_checked(["git", "worktree", "prune"], cwd=str(mgr.repo_root))
            for _, clean_id, wt_path, _, cand_brs in plan:
                if cand_brs and not wt_path.exists() and not _is_reparse_link(wt_path) and not _own_adm(gd_wts / clean_id, wt_path):
                    for b in cand_brs:
                        subprocess.run(["git", "branch", "-D", b], cwd=str(mgr.repo_root), capture_output=True, text=True)
                        if b not in deleted_brs and subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{b}"], cwd=str(mgr.repo_root), capture_output=True).returncode != 0:
                            deleted_brs.append(b)
        return {"ok": True, "dry_run": bool(dry_run), "repo_root": str(mgr.repo_root), "removed_worktrees": removed_wts, "deleted_branches": deleted_brs}


def build_parser() -> argparse.ArgumentParser:
    """Build StigmeRig argument parser."""
    parser = argparse.ArgumentParser(
        prog="stigmerig",
        description="Sovereign local-first multi-agent stigmergy coordination engine",
    )
    parser.add_argument(
        "--rig-dir",
        type=str,
        default=None,
        help="Path to .stigmerig state directory (defaults to $STIGMERIG_ROOT or .stigmerig)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser("init", help="Initialize coordination state directory")
    p_init.add_argument("--path", type=str, default=None, help="Custom directory path to initialize")
    p_init.add_argument("--no-icm", action="store_true", help="Skip ICM scaffolding (AGENTS.md, CONTEXT.md, rig.json)")

    # task
    p_task = subparsers.add_parser("task", help="Manage task lifecycle")
    task_sub = p_task.add_subparsers(dest="subcommand", required=True)

    p_tcreate = task_sub.add_parser("create", help="Create a new task")
    p_tcreate.add_argument("--id", required=True, help="Unique task ID (e.g. TASK-001)")
    p_tcreate.add_argument("--title", required=True, help="Task summary title")
    p_tcreate.add_argument("--parent", default=None, help="Parent task or epic ID")
    p_tcreate.add_argument("--tags", default=None, help="Comma-separated tags")
    p_tcreate.add_argument("--author", default=None, help="Author agent or operator name")
    p_tcreate.add_argument(
        "--archetype",
        default="diff_synthesizer",
        help="Agent archetype (diff_synthesizer, unit_tester, prose_auditor, topology_reader, etc.)",
    )
    p_tcreate.add_argument("--priority", type=int, default=100, help="Integer priority (default 100)")

    p_tlist = task_sub.add_parser("list", help="List tasks")
    p_tlist.add_argument("--status", default=None, help="Filter by status (todo, working, done, blocked)")
    p_tlist.add_argument("--parent", default=None, help="Filter by parent ID")

    p_tget = task_sub.add_parser("get", help="Retrieve task details")
    p_tget.add_argument("--id", required=True, help="Task ID to inspect")

    p_tclaim = task_sub.add_parser("claim", help="Claim a task with lease TTL")
    p_tclaim.add_argument("--id", required=True, help="Task ID to claim")
    p_tclaim.add_argument("--agent", required=True, help="Agent claiming the task")
    p_tclaim.add_argument("--ttl", type=int, default=1800, help="Lease TTL in seconds (default 1800)")
    p_tclaim.add_argument("--force-backpressure", action="store_true", help="Override review-queue backpressure")

    p_tdone = task_sub.add_parser("done", help="Complete a task with anti-self-approval gate")
    p_tdone.add_argument("--id", required=True, help="Task ID to mark done")
    p_tdone.add_argument("--agent", required=True, help="Agent completing the task")
    p_tdone.add_argument("--verifier", default=None, help="Independent verifying agent")
    p_tdone.add_argument(
        "--allow-self-approval",
        action="store_true",
        help="Bypass anti-self-approval enforcement",
    )
    p_tdone.add_argument("--session-id", default=None, help="Optional session or run identifier")
    p_tdone.add_argument("--model", default=None, help="Optional foundation model identifier (falls back to STIGMERIG_MODEL)")
    p_tdone.add_argument(
        "--evidence",
        action="append",
        default=None,
        dest="evidence",
        help="Repeatable path to evidence artifact to hash-bind on completion",
    )

    p_trev = task_sub.add_parser("review", help="Submit a working task for review")
    p_trev.add_argument("--id", required=True, help="Task ID to submit for review")
    p_trev.add_argument("--agent", required=True, help="Agent submitting for review")
    p_trev.add_argument("--summary", required=True, help="Summary of changes and verification")
    p_trev.add_argument("--verification-cmd", default=None, help="Verification command run")
    p_trev.add_argument("--diffstat", default=None, help="Diffstat summary")
    p_trev.add_argument("--model", default=None, help="Optional foundation model identifier (falls back to STIGMERIG_MODEL)")
    p_trev.add_argument("--evidence", action="append", default=None, dest="evidence", help="Repeatable evidence artifact path")

    p_tapp = task_sub.add_parser("approve", help="Approve a task in review")
    p_tapp.add_argument("--id", required=True, help="Task ID to approve")
    p_tapp.add_argument("--verifier", required=True, help="Independent verifying agent")
    p_tapp.add_argument("--session-id", default=None, help="Optional session or run identifier")
    p_tapp.add_argument("--model", default=None, help="Optional foundation model identifier (falls back to STIGMERIG_MODEL)")
    p_tapp.add_argument("--evidence", action="append", default=None, dest="evidence", help="Repeatable evidence artifact path")

    p_tfail = task_sub.add_parser("fail", help="Mark a task as blocked or failed")
    p_tfail.add_argument("--id", required=True, help="Task ID")
    p_tfail.add_argument("--agent", required=True, help="Reporting agent")
    p_tfail.add_argument("--reason", required=True, help="Blocker reason description")

    # audit
    p_audit = subparsers.add_parser("audit", help="Manage WORM hash-chain audit log")
    audit_sub = p_audit.add_subparsers(dest="subcommand", required=True)

    p_alog = audit_sub.add_parser("log", help="Append an audit record")
    p_alog.add_argument("--task", required=True, help="Task ID")
    p_alog.add_argument("--action", required=True, help="Action description")
    p_alog.add_argument("--agent", required=True, help="Acting agent")
    p_alog.add_argument("--status", required=True, help="Verification status")
    p_alog.add_argument("--payload", default="", help="Optional event payload string")

    p_averify = audit_sub.add_parser("verify", help="Verify SHA-256 hash-chain integrity")

    # board
    p_board = subparsers.add_parser("board", help="Sync Task_Board.md")
    board_sub = p_board.add_subparsers(dest="subcommand", required=True)
    board_sub.add_parser("sync", help="Synchronize markdown board from queue.db")

    # telemetry
    p_telem = subparsers.add_parser("telemetry", help="Export telemetry and sequence diagrams")
    telem_sub = p_telem.add_subparsers(dest="subcommand", required=True)
    p_tseq = telem_sub.add_parser("sequence", help="Generate Mermaid sequence diagram")
    p_tseq.add_argument("--task", default=None, help="Filter sequence by task ID")
    p_tseq.add_argument("--output", default=None, help="Output file path (prints to stdout if omitted)")
    p_tmet = telem_sub.add_parser("metrics", help="Generate task duration metrics markdown or JSON")
    p_tmet.add_argument("--output", default=None, help="Output file path")
    p_tmet.add_argument("--json", action="store_true", help="Emit structured JSON metrics instead of markdown")

    p_wprune = subparsers.add_parser("worktree", help="Manage isolated git worktrees").add_subparsers(dest="subcommand", required=True).add_parser("prune", help="Prune stale metadata and terminal task worktrees")
    p_wprune.add_argument("--delete-branches", action="store_true", help="Delete task/<ID> branches for terminal tasks")
    p_wprune.add_argument("--dry-run", action="store_true", help="Preview prunable worktrees and branches without deleting")
    p_wprune.add_argument("--output", default=None, help="Output file path for JSON prune report")

    add_traps_parser(subparsers)
    return parser


def _format_cli_error(exc: BaseException, rig_dir: Optional[Path]) -> str:
    msg = (exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)) or type(exc).__name__
    if isinstance(exc, subprocess.CalledProcessError):
        detail = next((t for r in (exc.stderr, exc.output) if (t := (r.decode("utf-8", errors="replace") if isinstance(r, bytes) else (r or "")).strip())), "")
        return f"{msg}: {detail}" if detail and detail not in msg else msg
    if isinstance(exc, sqlite3.DatabaseError):
        if rig_dir is not None and str(rig_dir) not in msg:
            msg = f"{msg} (rig: {rig_dir})"
        if isinstance(exc, sqlite3.DatabaseError) and any(s in msg.lower() for s in ("no such table", "table does not exist", "non-virtual base table")) and "stigmerig init" not in msg:
            msg = f"{msg}; run 'stigmerig init'"
    return msg


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI main execution routine."""
    parser = build_parser()
    args = parser.parse_args(argv)
    rig: Optional[StigmeRigDB] = None

    try:
        _validate_cli_args(args)
        db_dir = args.path if args.command == "init" and args.path is not None else args.rig_dir
        rig = StigmeRigDB(rig_dir=db_dir)

        if args.command == "init":
            hint = rig.init_rig(scaffold_icm=not getattr(args, "no_icm", False))
            print(f"Initialized StigmeRig in {rig.rig_dir}")
            if hint:
                print(hint)
            return 0

        if args.command == "traps":
            return run_traps_command(args, rig.rig_dir)

        if args.command == "worktree":
            from stigmerig.wave import get_git_repo_root
            ws_cwd = next((p for p in (rig.rig_dir.parent, *rig.rig_dir.parents) if p.is_dir()), Path.cwd())
            probe_cwd = rig.rig_dir if rig.rig_dir.is_dir() else ws_cwd
            explicit_rig = args.rig_dir is not None or os.environ.get("STIGMERIG_ROOT") is not None
            if not (repo_root := (None if not explicit_rig and not get_git_repo_root(Path.cwd()) else get_git_repo_root(probe_cwd))):
                raise RuntimeError(f"Not inside a git repository: {ws_cwd}")
        if missing_dbs := [n for n, p in (("queue.db", rig.queue_db_path), ("audit.db", rig.audit_db_path)) if not p.is_file()]:
            raise FileNotFoundError(f"Uninitialized StigmeRig directory {rig.rig_dir} (missing {', '.join(missing_dbs)}); run 'stigmerig init'")
        if args.command == "worktree" and args.subcommand == "prune":
            payload = json.dumps(_prune_worktrees(rig, repo_root, args.delete_branches, args.dry_run), indent=2)
            if args.output is not None:
                _write_output(args.output, payload + "\n", rig.rig_dir)
            print(payload)
            return 0

        if args.command == "task":
            if args.subcommand == "create":
                task = rig.create_task(
                    task_id=args.id,
                    title=args.title,
                    parent_id=args.parent,
                    tags=args.tags,
                    author=args.author,
                    archetype=args.archetype,
                    priority=args.priority,
                )
                print(json.dumps(task, indent=2))
                return 0

            if args.subcommand == "list":
                tasks = rig.list_tasks(status=args.status, parent_id=args.parent)
                print(json.dumps(tasks, indent=2))
                return 0

            if args.subcommand == "get":
                task = rig.get_task(args.id)
                if not task:
                    print(f"[ERROR] Task {args.id} not found", file=sys.stderr)
                    return 1
                print(json.dumps(task, indent=2))
                return 0

            if args.subcommand == "claim":
                ok = rig.claim_task(task_id=args.id, agent=args.agent, ttl_seconds=args.ttl, ignore_backpressure=args.force_backpressure)
                if ok:
                    print(f"Task {args.id} claimed by {args.agent}")
                    return 0
                print(f"[ERROR] Failed to claim task {args.id}: {_claim_failure_reason(rig, args.id, args.agent)}", file=sys.stderr)
                return 1

            if args.subcommand == "done":
                ok, msg = rig.complete_task(
                    task_id=args.id,
                    agent=args.agent,
                    verifier=args.verifier,
                    allow_self_approval=args.allow_self_approval,
                    session_id=getattr(args, "session_id", None),
                    evidence_paths=args.evidence,
                    model_id=args.model,
                )
                if ok:
                    next_step = rig.get_next_workflow_step(args.id)
                    res = {
                        "ok": True,
                        "task_id": args.id,
                        "message": msg,
                        "next_workflow_step": next_step,
                    }
                    print(json.dumps(res, indent=2))
                    return 0
                print(f"[ERROR] Failed to complete task {args.id}: {msg}", file=sys.stderr)
                return 1

            if args.subcommand == "review":
                ok, msg = rig.submit_for_review(
                    task_id=args.id, agent=args.agent, summary=args.summary,
                    verification_cmd=args.verification_cmd, diffstat=args.diffstat, evidence_paths=args.evidence,
                    model_id=args.model,
                )
                if ok:
                    print(json.dumps({"ok": True, "task_id": args.id, "message": msg}, indent=2))
                    return 0
                print(f"[ERROR] Failed to submit task {args.id} for review: {msg}", file=sys.stderr)
                return 1

            if args.subcommand == "approve":
                ok, msg = rig.approve_review(
                    task_id=args.id, verifier=args.verifier, session_id=args.session_id, evidence_paths=args.evidence,
                    model_id=args.model,
                )
                if ok:
                    print(json.dumps({"ok": True, "task_id": args.id, "message": msg, "next_workflow_step": rig.get_next_workflow_step(args.id)}, indent=2))
                    return 0
                print(f"[ERROR] Failed to approve task {args.id}: {msg}", file=sys.stderr)
                return 1

            if args.subcommand == "fail":
                ok = rig.fail_task(task_id=args.id, agent=args.agent, reason=args.reason)
                if ok:
                    print(f"Task {args.id} marked blocked: {args.reason}")
                    return 0
                print(f"[ERROR] Failed to mark task {args.id} blocked", file=sys.stderr)
                return 1

        if args.command == "audit":
            if args.subcommand == "log":
                entry_hash = rig.log_audit(
                    task_id=args.task,
                    action=args.action,
                    agent=args.agent,
                    verification_status=args.status,
                    payload=args.payload,
                )
                print(f"Logged audit entry: {entry_hash}")
                return 0

            if args.subcommand == "verify":
                ok, msg = rig.verify_audit_chain()
                if ok:
                    print(f"Audit verification PASSED: {msg}")
                    return 0
                print(f"[ERROR] Audit verification FAILED: {msg}", file=sys.stderr)
                return 1

        if args.command == "board":
            if args.subcommand == "sync":
                path = rig.sync_board()
                print(f"Board synchronized: {path}")
                return 0

        if args.command == "telemetry":
            from stigmerig.telemetry import TelemetryExporter
            exporter = TelemetryExporter(rig)
            if args.subcommand == "sequence":
                diag = exporter.generate_mermaid_sequence(task_id=args.task)
                if args.output is not None:
                    _write_output(args.output, diag, rig.rig_dir)
                    print(f"Wrote sequence diagram to {args.output}")
                else:
                    print(diag)
                return 0
            if args.subcommand == "metrics":
                if getattr(args, "json", False):
                    payload = json.dumps(exporter.export_metrics_json(), indent=2)
                    if args.output is not None:
                        _write_output(args.output, payload + "\n", rig.rig_dir)
                        print(f"Wrote metrics JSON to {args.output}")
                    else:
                        print(payload)
                    return 0
                tbl = exporter.generate_metrics_markdown()
                if args.output is not None:
                    _write_output(args.output, tbl, rig.rig_dir)
                    print(f"Wrote metrics table to {args.output}")
                else:
                    print(tbl)
                return 0

        return 0
    except (
        PermissionError,
        KeyError,
        ValueError,
        OverflowError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        sqlite3.OperationalError,
        sqlite3.IntegrityError,
        sqlite3.DatabaseError,
    ) as exc:
        target_rig = (rig.rig_dir if rig is not None else resolve_rig_dir(args.path if args.command == "init" and getattr(args, "path", None) is not None else getattr(args, "rig_dir", None))) if isinstance(exc, sqlite3.DatabaseError) else None
        print(f"[ERROR] {_format_cli_error(exc, target_rig)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

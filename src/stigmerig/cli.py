"""Command-line interface for StigmeRig."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional, Sequence

from stigmerig.db import StigmeRigDB


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

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI main execution routine."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        db_dir = args.path if args.command == "init" and args.path else args.rig_dir
        rig = StigmeRigDB(rig_dir=db_dir)

        if args.command == "init":
            hint = rig.init_rig(scaffold_icm=not getattr(args, "no_icm", False))
            print(f"Initialized StigmeRig in {rig.rig_dir}")
            if hint:
                print(hint)
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
                    print(f"Task {args.id} not found", file=sys.stderr)
                    return 1
                print(json.dumps(task, indent=2))
                return 0

            if args.subcommand == "claim":
                ok = rig.claim_task(task_id=args.id, agent=args.agent, ttl_seconds=args.ttl)
                if ok:
                    print(f"Task {args.id} claimed by {args.agent}")
                    return 0
                print(f"Failed to claim task {args.id} (already held or version conflict)", file=sys.stderr)
                return 1

            if args.subcommand == "done":
                ok, msg = rig.complete_task(
                    task_id=args.id,
                    agent=args.agent,
                    verifier=args.verifier,
                    allow_self_approval=args.allow_self_approval,
                    session_id=getattr(args, "session_id", None),
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
                print(f"Failed to complete task {args.id}: {msg}", file=sys.stderr)
                return 1

            if args.subcommand == "fail":
                ok = rig.fail_task(task_id=args.id, agent=args.agent, reason=args.reason)
                if ok:
                    print(f"Task {args.id} marked blocked: {args.reason}")
                    return 0
                print(f"Failed to mark task {args.id} blocked", file=sys.stderr)
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
                print(f"Audit verification FAILED: {msg}", file=sys.stderr)
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
                if args.output:
                    Path(args.output).write_text(diag, encoding="utf-8")
                    print(f"Wrote sequence diagram to {args.output}")
                else:
                    print(diag)
                return 0
            if args.subcommand == "metrics":
                if getattr(args, "json", False):
                    payload = json.dumps(exporter.export_metrics_json(), indent=2)
                    if args.output:
                        Path(args.output).write_text(payload + "\n", encoding="utf-8")
                        print(f"Wrote metrics JSON to {args.output}")
                    else:
                        print(payload)
                    return 0
                tbl = exporter.generate_metrics_markdown()
                if args.output:
                    Path(args.output).write_text(tbl, encoding="utf-8")
                    print(f"Wrote metrics table to {args.output}")
                else:
                    print(tbl)
                return 0

        return 0
    except (
        PermissionError,
        KeyError,
        ValueError,
        FileNotFoundError,
        sqlite3.OperationalError,
        sqlite3.IntegrityError,
    ) as exc:
        err_msg = exc.args[0] if exc.args else str(exc)
        print(f"[ERROR] {err_msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

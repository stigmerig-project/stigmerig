"""Hermetic tests for repeatable --follow-up task spawning on completion and approval."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import io
import json
import sqlite3
import threading
from pathlib import Path

import pytest
import stigmerig.cli as cli
from stigmerig.cli import _validate_cli_args, main
from stigmerig.db import TASK_ID_PATTERN, StigmeRigDB


def _run_cli(rig_dir: Path, *argv: str) -> tuple[int, str, str]:
    """Invoke CLI main with isolated stdio buffers and explicit rig directory."""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--rig-dir", str(rig_dir), *argv])
    return code, out.getvalue(), err.getvalue()


def _init_seeded_rig(tmp_path: Path, name: str = "ws") -> tuple[Path, StigmeRigDB]:
    """Create an initialized isolated StigmeRig workspace."""
    rig_dir = tmp_path / name / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig(scaffold_icm=False)
    return rig_dir, db


def _make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory symlink or Windows junction; return False if unsupported."""
    target.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return True
        except (ImportError, OSError):
            return False


def test_task_done_follow_up_single_multiple_and_author_fallback(tmp_path: Path) -> None:
    """Verify repeatable --follow-up spawning on task done via CLI and DB API."""
    rig_dir, db = _init_seeded_rig(tmp_path)

    # 1. Single --follow-up via CLI task done
    db.create_task("TASK-10", "Primary delivery", author="planner-1")
    assert db.claim_task("TASK-10", agent="builder-1") is True
    code, out, err = _run_cli(
        rig_dir,
        "task", "done",
        "--id", "TASK-10",
        "--agent", "builder-1",
        "--verifier", "verifier-1",
        "--follow-up", "  Add regression coverage  ",
    )
    assert code == 0, err
    cli_res = json.loads(out)
    assert cli_res["ok"] is True
    assert cli_res["follow_up_ids"] == ["FU-TASK-10-001"]

    fu1 = db.get_task("FU-TASK-10-001")
    assert fu1 is not None
    assert fu1["title"] == "Add regression coverage"
    assert fu1["status"] == "todo"
    assert fu1["priority"] == 100
    assert fu1["parent_id"] is None
    assert fu1["tags"] == "follow-up"
    assert fu1["author"] == "verifier-1"
    assert fu1["archetype"] == "diff_synthesizer"
    expected_rel = str((rig_dir / "tasks" / "FU-TASK-10-001.md").resolve().relative_to(rig_dir.parent.resolve()))
    assert fu1["context_path"] == expected_rel

    scaffold_text = (rig_dir / "tasks" / "FU-TASK-10-001.md").read_text(encoding="utf-8")
    assert "# FU-TASK-10-001: Add regression coverage" in scaffold_text
    assert "- **Parent**: None" in scaffold_text
    assert "- **Origin**: TASK-10" in scaffold_text
    assert "- **Archetype**: `diff_synthesizer`" in scaffold_text
    assert "- **Tags**: follow-up" in scaffold_text

    # 2. Multiple --follow-up flags via CLI task done
    db.create_task("TASK-11", "Multi follow-up task")
    assert db.claim_task("TASK-11", agent="builder-2") is True
    code, out, err = _run_cli(
        rig_dir,
        "task", "done",
        "--id", "TASK-11",
        "--agent", "builder-2",
        "--verifier", "verifier-2",
        "--follow-up", "Update docs",
        "--follow-up", "Benchmark latency",
    )
    assert code == 0, err
    cli_multi = json.loads(out)
    assert cli_multi["follow_up_ids"] == ["FU-TASK-11-001", "FU-TASK-11-002"]
    assert db.get_task("FU-TASK-11-001")["status"] == "todo"  # type: ignore[index]
    assert db.get_task("FU-TASK-11-002")["status"] == "todo"  # type: ignore[index]
    assert (rig_dir / "tasks" / "FU-TASK-11-001.md").is_file()
    assert (rig_dir / "tasks" / "FU-TASK-11-002.md").is_file()

    # 3. DB API complete_task with allow_self_approval=True falls back author to agent
    db.create_task("TASK-12", "Self approved work")
    assert db.claim_task("TASK-12", agent="solo-builder") is True
    fu_ids: list[str] = []
    ok, msg = db.complete_task(
        "TASK-12",
        agent="solo-builder",
        verifier=None,
        allow_self_approval=True,
        follow_ups=["Verify metrics", "Archive logs"],
        follow_up_ids_out=fu_ids,
    )
    assert ok is True, msg
    assert fu_ids == ["FU-TASK-12-001", "FU-TASK-12-002"]
    assert db.get_task("FU-TASK-12-001")["author"] == "solo-builder"  # type: ignore[index]
    assert db.get_task("FU-TASK-12-002")["author"] == "solo-builder"  # type: ignore[index]
    board_text = db.board_path.read_text(encoding="utf-8")
    assert "`FU-TASK-10-001`" in board_text
    assert "`FU-TASK-12-002`" in board_text


def test_task_approve_follow_up_and_parent_epic_closure_unblocked(tmp_path: Path) -> None:
    """Verify task approve spawns unparented follow-ups with origin_parent_id without blocking epic done."""
    rig_dir, db = _init_seeded_rig(tmp_path)

    db.create_task("EPIC-01", "Parent release epic")
    db.create_task("TASK-20", "Feature slice A", parent_id="EPIC-01", archetype="diff_synthesizer")
    db.create_task("TASK-21", "Feature slice B", parent_id="EPIC-01", archetype="diff_synthesizer")

    assert db.claim_task("TASK-20", agent="builder-a") is True
    ok, msg = db.submit_for_review("TASK-20", agent="builder-a", summary="Slice A ready")
    assert ok is True, msg

    code, out, err = _run_cli(
        rig_dir,
        "task", "approve",
        "--id", "TASK-20",
        "--verifier", "reviewer-a",
        "--session-id", "sess-app-20",
        "--follow-up", "Tighten edge assertions",
    )
    assert code == 0, err
    app_res = json.loads(out)
    assert app_res["follow_up_ids"] == ["FU-TASK-20-001"]

    assert db.claim_task("TASK-21", agent="builder-b") is True
    ok, msg = db.submit_for_review("TASK-21", agent="builder-b", summary="Slice B ready")
    assert ok is True, msg

    db_fu_ids: list[str] = []
    ok, msg = db.approve_review(
        "TASK-21",
        verifier="reviewer-b",
        session_id="sess-app-21",
        follow_ups=("Add stress scenario",),
        follow_up_ids_out=db_fu_ids,
    )
    assert ok is True, msg
    assert db_fu_ids == ["FU-TASK-21-001"]

    for fu_id, origin_id in (("FU-TASK-20-001", "TASK-20"), ("FU-TASK-21-001", "TASK-21")):
        fu_row = db.get_task(fu_id)
        assert fu_row is not None
        assert fu_row["status"] == "todo"
        assert fu_row["parent_id"] is None
        assert fu_row["tags"] == "follow-up"
        assert fu_row["archetype"] == "diff_synthesizer"
        assert (rig_dir / "tasks" / f"{fu_id}.md").is_file()

        with db.get_queue_conn() as qconn:
            ev = qconn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND event_type = 'created'",
                (fu_id,),
            ).fetchone()
        assert ev is not None
        payload = json.loads(ev["payload"])
        assert payload["origin_task_id"] == origin_id
        assert payload["origin_parent_id"] == "EPIC-01"
        assert payload["archetype"] == "diff_synthesizer"

    assert db.get_next_workflow_step("TASK-20") == {}
    assert db.get_next_workflow_step("TASK-21") == {}

    # Parent epic EPIC-01 must close cleanly (D3-04) while follow-ups remain todo
    assert db.claim_task("EPIC-01", agent="coord-1") is True
    epic_ok, epic_msg = db.complete_task("EPIC-01", agent="coord-1", verifier="reviewer-lead")
    assert epic_ok is True, epic_msg
    assert db.get_task("EPIC-01")["status"] == "done"  # type: ignore[index]
    assert db.get_task("FU-TASK-20-001")["status"] == "todo"  # type: ignore[index]
    assert db.get_task("FU-TASK-21-001")["status"] == "todo"  # type: ignore[index]


def test_case_insensitive_collision_avoidance_and_sequential_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify case-insensitive DB and filesystem collision checks plus wildcard safety."""
    rig_dir, db = _init_seeded_rig(tmp_path)

    # Pre-seed lowercase DB row and pre-existing files on disk
    db.create_task("fu-alpha-001", "Existing lowercase DB task")
    pre_file = rig_dir / "tasks" / "fu-alpha-002.md"
    pre_file.write_text("# Pre-existing lowercase file\n", encoding="utf-8")
    nested_dir = rig_dir / "tasks" / "Fu-Alpha-003"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (nested_dir / "context.md").write_text("# Pre-existing nested file\n", encoding="utf-8")

    db.create_task("alpha", "Lowercase origin")
    assert db.claim_task("alpha", agent="worker-1") is True
    fu_a: list[str] = []
    ok, msg = db.complete_task(
        "alpha",
        agent="worker-1",
        verifier="rev-1",
        follow_ups=["First new follow-up", "Second new follow-up"],
        follow_up_ids_out=fu_a,
    )
    assert ok is True, msg
    assert fu_a == ["FU-ALPHA-004", "FU-ALPHA-005"]
    assert pre_file.read_text(encoding="utf-8") == "# Pre-existing lowercase file\n"

    # Subsequent completion on uppercase origin sharing the same sanitized prefix
    db.create_task("ALPHA", "Uppercase origin")
    assert db.claim_task("ALPHA", agent="worker-2") is True
    fu_b: list[str] = []
    ok, msg = db.complete_task(
        "ALPHA",
        agent="worker-2",
        verifier="rev-2",
        follow_ups=["Third follow-up"],
        follow_up_ids_out=fu_b,
    )
    assert ok is True, msg
    assert fu_b == ["FU-ALPHA-006"]

    # Underscore in origin ID must NOT match unrelated characters in SQLite LIKE
    db.create_task("FU-AXB-009", "Unrelated wildcard decoy")
    db.create_task("a_b", "Underscore origin")
    assert db.claim_task("a_b", agent="worker-3") is True
    fu_u: list[str] = []
    ok, msg = db.complete_task(
        "a_b",
        agent="worker-3",
        verifier="rev-3",
        follow_ups=["Underscore safe"],
        follow_up_ids_out=fu_u,
    )
    assert ok is True, msg
    assert fu_u == ["FU-A_B-001"]

    # Symlink trap on target follow-up file must abort transaction before committing
    db.create_task("beta", "Symlink target check")
    assert db.claim_task("beta", agent="worker-4") is True
    blocked_file = rig_dir / "tasks" / "FU-BETA-001.md"
    orig_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self == blocked_file or orig_is_symlink(self),
    )
    with pytest.raises(ValueError, match="symlink or junction"):
        db.complete_task(
            "beta",
            agent="worker-4",
            verifier="rev-4",
            follow_ups=["Should rollback"],
        )
    assert db.get_task("beta")["status"] == "working"  # type: ignore[index]
    assert db.get_task("FU-BETA-001") is None


def test_audit_trail_integrity_and_multiline_agent_sanitization(tmp_path: Path) -> None:
    """Verify hash-chained WORM audit records for both origin completion and spawned tasks."""
    rig_dir, db = _init_seeded_rig(tmp_path)

    db.create_task("EPIC-9", "Audit epic")
    db.create_task("TASK-30", "Audited origin", parent_id="EPIC-9")
    assert db.claim_task("TASK-30", agent="builder\nmultiline") is True

    fu_ids: list[str] = []
    ok, msg = db.complete_task(
        "TASK-30",
        agent="builder\nmultiline",
        verifier=None,
        allow_self_approval=True,
        session_id="sess-audit-30",
        follow_ups=["Audit check 1", "Audit check 2"],
        follow_up_ids_out=fu_ids,
    )
    assert ok is True, msg
    assert fu_ids == ["FU-TASK-30-001", "FU-TASK-30-002"]

    with db.get_queue_conn() as qconn:
        ev_completed = qconn.execute(
            "SELECT payload FROM task_events WHERE task_id = 'TASK-30' AND event_type = 'completed'"
        ).fetchone()
    assert ev_completed is not None
    comp_raw = ev_completed["payload"]
    comp_obj = json.loads(comp_raw)
    assert comp_obj["follow_up_ids"] == ["FU-TASK-30-001", "FU-TASK-30-002"]

    with db.get_audit_conn() as aconn:
        comp_audit = aconn.execute(
            "SELECT * FROM audit_log WHERE task_id = 'TASK-30' AND action = 'task_completed'"
        ).fetchone()
        fu1_audit = aconn.execute(
            "SELECT * FROM audit_log WHERE task_id = 'FU-TASK-30-001' AND action = 'task_created'"
        ).fetchone()
        fu2_audit = aconn.execute(
            "SELECT * FROM audit_log WHERE task_id = 'FU-TASK-30-002' AND action = 'task_created'"
        ).fetchone()

    assert comp_audit is not None
    assert comp_audit["payload_hash"] == hashlib.sha256(comp_raw.encode("utf-8")).hexdigest()
    for fu_aud, title in ((fu1_audit, "Audit check 1"), (fu2_audit, "Audit check 2")):
        assert fu_aud is not None
        assert fu_aud["verification_status"] == "unverified"
        assert fu_aud["agent"] == "builder multiline"
        expected_payload = json.dumps({
            "title": title,
            "parent_id": None,
            "origin_task_id": "TASK-30",
            "origin_parent_id": "EPIC-9",
        })
        assert fu_aud["payload_hash"] == hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()

    chain_ok, chain_msg = db.verify_audit_chain()
    assert chain_ok is True, chain_msg


def test_unused_follow_up_parity_omits_key_from_events_audit_and_stdout(tmp_path: Path) -> None:
    """Verify completions without --follow-up omit follow_up_ids everywhere for hash parity."""
    rig_dir, db = _init_seeded_rig(tmp_path)

    db.create_task("TASK-40", "Done without follow-up")
    assert db.claim_task("TASK-40", agent="builder-1") is True
    code, out, err = _run_cli(
        rig_dir,
        "task", "done",
        "--id", "TASK-40",
        "--agent", "builder-1",
        "--verifier", "verifier-1",
        "--session-id", "sess-40",
    )
    assert code == 0, err
    assert "follow_up_ids" not in json.loads(out)

    db.create_task("TASK-41", "Approve without follow-up")
    assert db.claim_task("TASK-41", agent="builder-1") is True
    assert db.submit_for_review("TASK-41", agent="builder-1", summary="Ready")[0] is True
    code, out, err = _run_cli(
        rig_dir,
        "task", "approve",
        "--id", "TASK-41",
        "--verifier", "verifier-1",
        "--session-id", "sess-41",
    )
    assert code == 0, err
    assert "follow_up_ids" not in json.loads(out)

    db.create_task("TASK-42", "Empty list follow-up")
    assert db.claim_task("TASK-42", agent="builder-1") is True
    fu_ids: list[str] = []
    ok, msg = db.complete_task(
        "TASK-42",
        agent="builder-1",
        verifier="verifier-1",
        session_id="sess-42",
        follow_ups=[],
        follow_up_ids_out=fu_ids,
    )
    assert ok is True, msg
    assert fu_ids == []

    for tid in ("TASK-40", "TASK-41", "TASK-42"):
        with db.get_queue_conn() as qconn:
            raw = qconn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND event_type = 'completed'",
                (tid,),
            ).fetchone()["payload"]
        assert "follow_up_ids" not in raw
        with db.get_audit_conn() as aconn:
            aud_hash = aconn.execute(
                "SELECT payload_hash FROM audit_log WHERE task_id = ? AND action = 'task_completed'",
                (tid,),
            ).fetchone()["payload_hash"]
        assert aud_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_already_done_task_behavior_with_and_without_follow_ups(tmp_path: Path) -> None:
    """Verify idempotent no-follow-up behavior and rejection when attaching follow-ups to done tasks."""
    rig_dir, db = _init_seeded_rig(tmp_path)

    db.create_task("TASK-50", "Completed via done")
    assert db.claim_task("TASK-50", agent="builder-1") is True
    assert db.complete_task("TASK-50", agent="builder-1", verifier="rev-1")[0] is True

    db.create_task("TASK-51", "Completed via approve")
    assert db.claim_task("TASK-51", agent="builder-1") is True
    assert db.submit_for_review("TASK-51", agent="builder-1", summary="Ready")[0] is True
    assert db.approve_review("TASK-51", verifier="rev-1")[0] is True

    # Idempotent preservation when follow_ups is None or empty
    assert db.complete_task("TASK-50", agent="builder-1", verifier="rev-1") == (
        True,
        "Task TASK-50 already completed",
    )
    assert db.approve_review("TASK-51", verifier="rev-1") == (
        True,
        "Task TASK-51 already completed",
    )

    # Non-empty follow_ups on done task returns False and spawns zero tasks
    fu_done: list[str] = []
    ok_d, msg_d = db.complete_task(
        "TASK-50", agent="builder-1", verifier="rev-1", follow_ups=["Late item"], follow_up_ids_out=fu_done
    )
    assert ok_d is False
    assert msg_d == "Cannot attach follow-up tasks to 'TASK-50': task is already completed"
    assert fu_done == []

    fu_app: list[str] = []
    ok_a, msg_a = db.approve_review(
        "TASK-51", verifier="rev-1", follow_ups=["Late item"], follow_up_ids_out=fu_app
    )
    assert ok_a is False
    assert msg_a == "Cannot attach follow-up tasks to 'TASK-51': task is already completed"
    assert fu_app == []

    # CLI exits 1 on both
    code_d, _, err_d = _run_cli(
        rig_dir, "task", "done", "--id", "TASK-50", "--agent", "builder-1", "--verifier", "rev-1", "--follow-up", "Late"
    )
    assert code_d == 1 and "[ERROR]" in err_d
    code_a, _, err_a = _run_cli(
        rig_dir, "task", "approve", "--id", "TASK-51", "--verifier", "rev-1", "--follow-up", "Late"
    )
    assert code_a == 1 and "[ERROR]" in err_a

    assert db.get_task("FU-TASK-50-001") is None
    assert db.get_task("FU-TASK-51-001") is None
    assert not (rig_dir / "tasks" / "FU-TASK-50-001.md").exists()
    assert not (rig_dir / "tasks" / "FU-TASK-51-001.md").exists()


def test_lost_cas_and_open_child_fail_cleanly_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify CAS version mismatch and D3-04 guard fail cleanly with zero follow-ups or scaffolds."""
    rig_dir, db = _init_seeded_rig(tmp_path)

    db.create_task("TASK-60", "Stale CAS done")
    assert db.claim_task("TASK-60", agent="builder-1") is True
    db.create_task("TASK-61", "Stale CAS approve")
    assert db.claim_task("TASK-61", agent="builder-1") is True
    assert db.submit_for_review("TASK-61", agent="builder-1", summary="Ready")[0] is True

    real_get_task = db.get_task

    def _stale_get_task(tid: str):
        row = real_get_task(tid)
        if row is not None and tid in ("TASK-60", "TASK-61"):
            row["version"] = row["version"] + 99
        return row

    monkeypatch.setattr(db, "get_task", _stale_get_task)
    fu_d: list[str] = []
    ok_d, msg_d = db.complete_task(
        "TASK-60",
        agent="builder-1",
        verifier="rev-1",
        follow_ups=["Never spawned"],
        follow_up_ids_out=fu_d,
    )
    assert ok_d is False
    assert "Concurrent modification" in msg_d
    assert fu_d == []

    fu_a: list[str] = []
    ok_a, msg_a = db.approve_review(
        "TASK-61",
        verifier="rev-1",
        follow_ups=["Never spawned approve"],
        follow_up_ids_out=fu_a,
    )
    assert ok_a is False
    assert "Concurrent modification" in msg_a
    assert fu_a == []

    monkeypatch.undo()

    # D3-04 open child guard blocks completion without spawning follow-ups
    db.create_task("EPIC-62", "Parent with open child")
    db.create_task("TASK-62", "Open child", parent_id="EPIC-62")
    assert db.claim_task("EPIC-62", agent="coord-1") is True
    fu_e: list[str] = []
    ok_e, msg_e = db.complete_task(
        "EPIC-62",
        agent="coord-1",
        verifier="rev-1",
        follow_ups=["Blocked by open child"],
        follow_up_ids_out=fu_e,
    )
    assert ok_e is False
    assert "open child tasks remain" in msg_e
    assert fu_e == []

    assert [t["id"] for t in db.list_tasks() if t["id"].startswith("FU-")] == []
    with db.get_queue_conn() as qconn:
        fu_events = qconn.execute("SELECT COUNT(*) FROM task_events WHERE task_id LIKE 'FU-%'").fetchone()[0]
    assert fu_events == 0
    assert [p for p in (rig_dir / "tasks").iterdir() if p.name.upper().startswith("FU-")] == []


def test_upfront_validation_bounds_and_origin_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify upfront ValueError checks, CLI exit 1 bounds, and 64-char origin truncation."""
    # 1. Uninitialized rig raises ValueError BEFORE touching SQLite or filesystem
    uninit_rig = tmp_path / "uninit" / ".stigmerig"
    uninit_db = StigmeRigDB(rig_dir=uninit_rig)

    bad_inputs: list[object] = [
        "bare string",
        b"bare bytes",
        Path("bare/path"),
        {"set-item"},
        {"dict": "item"},
        bytearray(b"arr"),
        [""],
        ["   "],
        ["a" * 241],
        ["item"] * 11,
        [123],
        [Path("item")],
        ["line1\nline2"],
        ["line1\rline2"],
        ["col1\tcol2"],
        ["nul\x00byte"],
        ["ansi\x1b[31m"],
        ["zwsp\u200b"],
        ["bidi\u202e"],
    ]
    for bad in bad_inputs:
        with pytest.raises(ValueError):
            uninit_db.complete_task("TASK-1", agent="w", verifier="r", follow_ups=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            uninit_db.approve_review("TASK-1", verifier="r", follow_ups=bad)  # type: ignore[arg-type]

    for bad_out in ("not-a-list", (), {}, 42):
        with pytest.raises(ValueError):
            uninit_db.complete_task("TASK-1", agent="w", verifier="r", follow_ups=["Ok"], follow_up_ids_out=bad_out)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            uninit_db.approve_review("TASK-1", verifier="r", follow_ups=["Ok"], follow_up_ids_out=bad_out)  # type: ignore[arg-type]

    assert not uninit_rig.exists()

    # 2. Symlink/junction tasks_dir rejected upfront before DB access
    linked_rig = tmp_path / "linked" / ".stigmerig"
    linked_tasks = linked_rig / "tasks"
    linked_rig.mkdir(parents=True)
    target_dir = tmp_path / "outside_tasks"
    if not _make_dir_link(linked_tasks, target_dir):
        orig_is_symlink = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == linked_tasks or orig_is_symlink(self))
    linked_db = StigmeRigDB(rig_dir=linked_rig)
    with pytest.raises(ValueError, match="symlink or junction"):
        linked_db.complete_task("TASK-1", agent="w", verifier="r", follow_ups=["Valid"])
    with pytest.raises(ValueError, match="symlink or junction"):
        linked_db.approve_review("TASK-1", verifier="r", follow_ups=["Valid"])
    assert not linked_db.queue_db_path.exists()

    # 3. CLI _validate_cli_args and main() reject bad --follow-up forms with exit code 1
    rig_dir, db = _init_seeded_rig(tmp_path)
    db.create_task("TASK-70", "CLI validation target")
    assert db.claim_task("TASK-70", agent="w") is True

    for ns_bad in ("bare", [], 123, [Path("x")], [123], [""], ["   "], ["x" * 241], ["x"] * 11, ["a\nb"], ["a\x85b"]):
        ns = argparse.Namespace(
            rig_dir=str(rig_dir),
            command="task",
            subcommand="done",
            id="TASK-70",
            agent="w",
            verifier="r",
            allow_self_approval=False,
            follow_ups=ns_bad,
        )
        with pytest.raises(ValueError):
            _validate_cli_args(ns)

    for bad_cli_val in ("", "   ", "x" * 241, "a\nb", "a\rb", "a\tb", "a\x00b", "a\x1b[31m"):
        code_d, _, err_d = _run_cli(
            rig_dir, "task", "done", "--id", "TASK-70", "--agent", "w", "--verifier", "r", "--follow-up", bad_cli_val
        )
        assert code_d == 1 and "[ERROR]" in err_d

    too_many_flags = [arg for i in range(11) for arg in ("--follow-up", f"Item {i}")]
    code_11, _, err_11 = _run_cli(
        rig_dir, "task", "done", "--id", "TASK-70", "--agent", "w", "--verifier", "r", *too_many_flags
    )
    assert code_11 == 1 and "[ERROR]" in err_11

    # 4. Boundary valid: 10 items & stripped length == 240
    ten_flags = [arg for i in range(9) for arg in ("--follow-up", f"Item {i}")]
    max_240 = "  " + ("z" * 240) + "  "
    code_10, out_10, err_10 = _run_cli(
        rig_dir, "task", "done", "--id", "TASK-70", "--agent", "w", "--verifier", "r", "--follow-up", max_240, *ten_flags
    )
    assert code_10 == 0, err_10
    res_10 = json.loads(out_10)
    assert len(res_10["follow_up_ids"]) == 10
    assert db.get_task("FU-TASK-70-001")["title"] == "z" * 240  # type: ignore[index]

    # 5. Long 64-char origin truncation and special-character prefix sanitization
    long_origin = "task." + ("a_b-c" * 11) + "1234"
    assert len(long_origin) == 64 and TASK_ID_PATTERN.fullmatch(long_origin)
    db.create_task(long_origin, "Long origin task")
    assert db.claim_task(long_origin, agent="w") is True
    long_fu: list[str] = []
    ok_long, msg_long = db.complete_task(
        long_origin, agent="w", verifier="r", follow_ups=["Truncated ID"], follow_up_ids_out=long_fu
    )
    assert ok_long is True, msg_long
    assert len(long_fu) == 1
    assert len(long_fu[0]) == 63 <= 64
    assert TASK_ID_PATTERN.fullmatch(long_fu[0])
    assert StigmeRigDB._format_follow_up_prefix("!!!") == "FU-TASK-"


def test_concurrent_completion_follow_up_stress_no_busy_or_duplicate_ids(tmp_path: Path) -> None:
    """Stress concurrent completions spawning follow-ups and shared-task CAS contention."""
    rig_dir, db = _init_seeded_rig(tmp_path, name="stress_ws")

    # 8 distinct tasks (including 2 sharing sanitized uppercase prefix) + 1 shared CAS task
    distinct_ids = [f"TASK-ST-{i:02d}" for i in range(6)] + ["shared.id", "SHARED.ID"]
    for idx, tid in enumerate(distinct_ids):
        db.create_task(tid, f"Stress task {tid}")
        assert db.claim_task(tid, agent=f"builder-{idx}") is True

    db.create_task("TASK-RACE", "Shared CAS contention task")
    assert db.claim_task("TASK-RACE", agent="racer-agent") is True

    barrier = threading.Barrier(10, timeout=10.0)

    def _complete_distinct(idx: int, tid: str) -> tuple[bool, list[str]]:
        wdb = StigmeRigDB(rig_dir=rig_dir)
        out_ids: list[str] = []
        barrier.wait()
        ok, _ = wdb.complete_task(
            tid,
            agent=f"builder-{idx}",
            verifier=f"verifier-{idx}",
            follow_ups=[f"Follow A {tid}", f"Follow B {tid}"],
            follow_up_ids_out=out_ids,
        )
        return ok, out_ids

    def _complete_race(slot: int) -> tuple[bool, list[str]]:
        wdb = StigmeRigDB(rig_dir=rig_dir)
        out_ids: list[str] = []
        barrier.wait()
        ok, _ = wdb.complete_task(
            "TASK-RACE",
            agent="racer-agent",
            verifier=f"verifier-race-{slot}",
            follow_ups=["Winner follow-up"],
            follow_up_ids_out=out_ids,
        )
        return ok, out_ids

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        distinct_futs = [pool.submit(_complete_distinct, i, tid) for i, tid in enumerate(distinct_ids)]
        race_futs = [pool.submit(_complete_race, s) for s in range(2)]
        distinct_res = [f.result() for f in distinct_futs]
        race_res = [f.result() for f in race_futs]

    assert all(ok and len(ids) == 2 for ok, ids in distinct_res)
    # Exactly one racer wins CAS or sees already-done after commit; never both spawn follow-ups
    spawned_race = [ids for ok, ids in race_res if ok and ids]
    assert len(spawned_race) == 1
    assert spawned_race[0] == ["FU-TASK-RACE-001"]

    all_spawned = [fid for _, ids in (*distinct_res, *race_res) for fid in ids]
    assert len(all_spawned) == 17
    assert len(set(all_spawned)) == 17
    assert set(all_spawned) & {"FU-SHARED.ID-001", "FU-SHARED.ID-002", "FU-SHARED.ID-003", "FU-SHARED.ID-004"} == {
        "FU-SHARED.ID-001",
        "FU-SHARED.ID-002",
        "FU-SHARED.ID-003",
        "FU-SHARED.ID-004",
    }

    for fid in all_spawned:
        row = db.get_task(fid)
        assert row is not None and row["status"] == "todo"
        assert (rig_dir / "tasks" / f"{fid}.md").is_file()

    assert db.verify_audit_chain()[0] is True
    assert [p for p in rig_dir.rglob("*") if p.name.endswith(".tmp")] == []

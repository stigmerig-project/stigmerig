"""Hermetic tests for upfront _ensure_audit_ready, complete_task guards, contiguous WORM IDs, and id ASC tie-breaking."""

import json
import sqlite3
from pathlib import Path
import pytest
from stigmerig.db import GENESIS_HASH, StigmeRigDB


def test_ensure_audit_ready_prevents_split_brain_across_mutations(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "ws" / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    db.create_task("TSK-TODO", "Todo")
    db.create_task("TSK-WORK", "Work")
    assert db.claim_task("TSK-WORK", "WorkerA") is True
    with db.get_queue_conn() as qc:
        snap = ([dict(r) for r in qc.execute("SELECT * FROM tasks ORDER BY id")], [dict(r) for r in qc.execute("SELECT * FROM task_events ORDER BY id")])

    corruptions = [
        ("ALTER TABLE audit_log RENAME TO _raw; CREATE VIEW audit_log AS SELECT * FROM _raw;", sqlite3.OperationalError),
        ("DROP VIEW audit_log; CREATE VIRTUAL TABLE audit_log USING fts5(content);", sqlite3.OperationalError),
        ("DROP TABLE audit_log;", sqlite3.OperationalError),
        (None, FileNotFoundError),
    ]
    for sql, expected_exc in corruptions:
        if sql is not None:
            with db.get_audit_conn() as ac:
                ac.executescript(sql)
        else:
            db.audit_db_path.unlink()
        for fn in (
            lambda: db.create_task("TSK-NEW", "New"),
            lambda: db.claim_task("TSK-TODO", "WorkerB"),
            lambda: db.complete_task("TSK-WORK", "WorkerA", verifier="Rev1"),
            lambda: db.fail_task("TSK-WORK", "WorkerA", "broken"),
        ):
            with pytest.raises(expected_exc):
                fn()
        assert not db.get_task_file("TSK-NEW").exists() and not (db.rig_dir / "traps.json").exists()
        if sql is None:
            assert not db.audit_db_path.exists()
        with db.get_queue_conn() as qc:
            assert ([dict(r) for r in qc.execute("SELECT * FROM tasks ORDER BY id")], [dict(r) for r in qc.execute("SELECT * FROM task_events ORDER BY id")]) == snap


def test_complete_task_printability_and_evidence_dedup_guards(tmp_path: Path) -> None:
    ws, db = tmp_path / "ws", StigmeRigDB(rig_dir=tmp_path / "ws" / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    (ws / "sub").mkdir(parents=True)
    (ws / "ev1.txt").write_text("alpha", encoding="utf-8")
    (ws / "sub" / "ev2.txt").write_text("beta", encoding="utf-8")
    db.create_task("TSK-1", "Guard check")
    assert db.claim_task("TSK-1", "Worker\r\nOne") is True
    for bad_ag in ("", "   ", "\r\n", "Worker\x00One", "Worker\tOne", None, 42):
        with pytest.raises(ValueError):
            db.complete_task("TSK-1", bad_ag, verifier="Rev1")  # type: ignore[arg-type]
    for bad_ver in ("Rev\n1", "Rev\r1", "Rev\t1", "  Rev\x001  ", 123, True):
        with pytest.raises(ValueError):
            db.complete_task("TSK-1", "Worker\r\nOne", verifier=bad_ver, allow_self_approval=True)  # type: ignore[arg-type]
    for bad_sid in ("", "   ", "\t\n", "sess\n1", "sess\r1", "sess\x001", "sess\t1", 99, False):
        with pytest.raises(ValueError):
            db.complete_task("TSK-1", "Worker\r\nOne", verifier="Rev1", session_id=bad_sid)  # type: ignore[arg-type]
    for bad_ev in ([], (), ["ev1.txt", "./ev1.txt"], [ws / "ev1.txt", "ev1.txt"], ["sub/ev2.txt", "sub/../sub/ev2.txt"]):
        with pytest.raises(ValueError):
            db.complete_task("TSK-1", "Worker\r\nOne", verifier="Rev1", evidence_paths=bad_ev)
    with pytest.raises(PermissionError):
        db.complete_task("TSK-1", "Worker\r\nOne", verifier="  \t\r\n  ", allow_self_approval=False)
    assert db.get_task("TSK-1")["status"] == "working"  # type: ignore[index]
    assert db.complete_task("TSK-1", "Worker\r\nOne", verifier="  Rev1  ", session_id="sess-01", evidence_paths=["ev1.txt", "sub/ev2.txt"])[0]
    db.create_task("TSK-2", "Blank verifier")
    assert db.claim_task("TSK-2", "Solo") and db.complete_task("TSK-2", "Solo", verifier=" \t\n ", allow_self_approval=True)[0]
    with db.get_queue_conn() as qc:
        ev_map = {r["task_id"]: json.loads(r["payload"]) for r in qc.execute("SELECT task_id, payload FROM task_events WHERE event_type='completed'")}
    assert ev_map["TSK-1"]["verifier"] == "Rev1" and ev_map["TSK-1"]["session_id"] == "sess-01" and ev_map["TSK-2"]["verifier"] is None
    assert db.verify_audit_chain()[0] is True


def test_contiguous_audit_id_trigger_and_deterministic_id_tie_breaking(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "rig")
    db.init_rig(scaffold_icm=False)
    ins_sql = "INSERT INTO audit_log (id, timestamp, task_id, action, agent, verification_status, payload_hash, previous_hash, entry_hash) VALUES (?, '2026-01-01T00:00:00Z', 'T1', 'act', 'Ag', 'ok', ?, ?, ?)"
    for bad_id in (0, -1, -50, 2, 7):
        with db.get_audit_conn() as ac, pytest.raises(sqlite3.IntegrityError):
            ac.execute(ins_sql, (bad_id, "a" * 64, GENESIS_HASH, "b" * 64))
    h1 = db.log_audit("T1", "act", "Ag", "ok")
    for bad_id in (0, -2, 3, 9):
        with db.get_audit_conn() as ac, pytest.raises(sqlite3.IntegrityError):
            ac.execute(ins_sql, (bad_id, "a" * 64, h1, "c" * 64))
    db.log_audit("T2", "act", "Ag", "ok")
    assert db.verify_audit_chain() == (True, "Verified 2 audit entries with intact hash chain")

    db.create_task("EPIC-1", "Epic")
    for tid, arch in (("TSK-D", "diff_synthesizer"), ("TSK-C", "unit_tester"), ("TSK-A", "unit_tester"), ("TSK-B", "unit_tester"), ("TSK-P", "policy_gatekeeper")):
        db.create_task(tid, tid, parent_id="EPIC-1", archetype=arch, priority=10)
    with db.get_queue_conn() as qc:
        qc.execute("UPDATE tasks SET created_at = '2026-06-01T00:00:00Z' WHERE parent_id = 'EPIC-1'")
        qc.commit()
    assert [t["id"] for t in db.list_tasks(status="todo", parent_id="EPIC-1")] == ["TSK-A", "TSK-B", "TSK-C", "TSK-D", "TSK-P"]
    assert db.get_next_workflow_step("TSK-D")["task_id"] == "TSK-A"
    assert db.get_next_workflow_step("TSK-P")["task_id"] == "TSK-A"

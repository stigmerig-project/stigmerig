"""Hermetic tests for Tier B audit/queue readiness guards, TTL bounds, blank verifiers, and create_task field errors."""

import hashlib
import json
import sqlite3
from pathlib import Path
import pytest
from stigmerig.db import StigmeRigDB
from stigmerig.telemetry import TelemetryExporter


def test_audit_tier_b_column_spoofing_corruption_and_header_guards(tmp_path: Path):
    db = StigmeRigDB(rig_dir=tmp_path / "spoof")
    db.init_rig(scaffold_icm=False)
    db.log_audit("TSK-1", "task_completed", "AgentA", "unverified", "p1")
    with db.get_audit_conn() as conn:
        conn.execute("DROP TRIGGER prevent_audit_update")
        conn.execute("ALTER TABLE audit_log ADD COLUMN hash TEXT")
        conn.execute("UPDATE audit_log SET hash = ?, verification_status = 'verified' WHERE id = 1", ("a" * 64,))
        conn.commit()
    ok, msg = db.verify_audit_chain()
    assert not ok and "Hash mismatch at ID 1" in msg
    with db.get_audit_conn() as conn:
        r = dict(conn.execute("SELECT * FROM audit_log WHERE id = 1").fetchone())
        hdr = f"{r['timestamp']}:{r['task_id']}:{r['action']}:{b'A'}:verified:{r['payload_hash']}:{r['previous_hash']}"
        conn.execute("UPDATE audit_log SET agent = ?, entry_hash = ? WHERE id = 1", (b"A", hashlib.sha256(hdr.encode()).hexdigest()))
        conn.commit()
    assert db.verify_audit_chain() == (False, "Invalid Tier B header column type at ID 1")
    db_bad = StigmeRigDB(rig_dir=tmp_path / "bad")
    db_bad.rig_dir.mkdir()
    db_bad.audit_db_path.write_bytes(b"corrupted non-sqlite header bytes" * 8)
    ok_corrupt, msg_corrupt = db_bad.verify_audit_chain()
    assert not ok_corrupt and "database error" in msg_corrupt.lower()
    db_bad.audit_db_path.unlink()
    with db_bad.get_audit_conn() as conn:
        conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, previous_hash TEXT, entry_hash TEXT)")
    assert db_bad.verify_audit_chain() == (False, "audit_log schema invalid")


def test_claim_task_ttl_bounds_and_blank_verifier_normalization(tmp_path: Path):
    db = StigmeRigDB(rig_dir=tmp_path / "rig")
    db.init_rig(scaffold_icm=False)
    db.create_task("TSK-TTL", "TTL bounds")
    for bad_ttl in (float("inf"), float("-inf"), float("nan"), 1e100, 10**18, 10**400, 300_000_000_000):
        with pytest.raises(ValueError):
            db.claim_task("TSK-TTL", agent="AgentA", ttl_seconds=bad_ttl)
    assert db.get_task("TSK-TTL")["claimed_by"] is None and db.claim_task("TSK-TTL", "AgentA", 120.5) is True
    for idx, raw in enumerate(("", "   \t\n", "  ReviewerB  "), start=1):
        db.create_task(f"TSK-V-{idx}", "Verifier check")
        assert db.claim_task(f"TSK-V-{idx}", "AgentSolo") and db.complete_task(f"TSK-V-{idx}", "AgentSolo", raw, True)[0]
    with db.get_queue_conn() as conn, db.get_audit_conn() as aconn:
        verifiers = {r["task_id"]: json.loads(r["payload"])["verifier"] for r in conn.execute("SELECT task_id, payload FROM task_events WHERE event_type = 'completed'")}
        statuses = {r["task_id"]: r["verification_status"] for r in aconn.execute("SELECT task_id, verification_status FROM audit_log WHERE action = 'task_completed'")}
    assert verifiers == {"TSK-V-1": None, "TSK-V-2": None, "TSK-V-3": "ReviewerB"}
    assert statuses == {"TSK-V-1": "unverified", "TSK-V-2": "unverified", "TSK-V-3": "verified"}
    assert db.verify_audit_chain()[0] is True


def test_ensure_queue_ready_prevents_phantom_db_and_rejects_partial_or_virtual_schemas(tmp_path: Path) -> None:
    uninit, partial = StigmeRigDB(rig_dir=tmp_path / "uninit" / ".stigmerig"), StigmeRigDB(rig_dir=tmp_path / "partial" / ".stigmerig")
    partial.init_rig(scaffold_icm=False)
    for p in partial.rig_dir.glob("queue.db*"):
        p.unlink()
    ops = lambda d: (
        lambda: d.get_task("TSK-1"), lambda: d.list_tasks(), lambda: d.create_task("TSK-1", "Title"),
        lambda: d.claim_task("TSK-1", "AgentA"), lambda: d.complete_task("TSK-1", "AgentA", verifier="RevB"),
        lambda: d.get_next_workflow_step("TSK-1"), lambda: d.fail_task("TSK-1", "AgentA", "reason"), d.sync_board,
    )
    for target in (uninit, partial):
        for fn in ops(target):
            with pytest.raises(FileNotFoundError) as exc_info:
                fn()
            assert "stigmerig init" in str(exc_info.value) and not target.queue_db_path.exists()
    assert not uninit.rig_dir.exists() and TelemetryExporter(uninit).export_metrics_json()["durations"] == [] and not uninit.queue_db_path.exists()

    db = StigmeRigDB(rig_dir=tmp_path / "corrupt" / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    for sql in (
        "ALTER TABLE tasks RENAME TO _t; CREATE VIEW tasks AS SELECT * FROM _t;",
        "DROP VIEW tasks; CREATE VIRTUAL TABLE tasks USING fts5(id, title);",
        "DROP TABLE tasks; DROP TABLE _t;",
        "RESTORE",
        "ALTER TABLE task_events RENAME TO _e; CREATE VIEW task_events AS SELECT * FROM _e;",
        "DROP VIEW task_events; CREATE VIRTUAL TABLE task_events USING fts5(task_id, event_type);",
        "DROP TABLE task_events; DROP TABLE _e;",
    ):
        if sql == "RESTORE":
            db.init_rig(scaffold_icm=False)
            continue
        with db.get_queue_conn() as qc:
            qc.executescript(sql)
        for fn in (db._ensure_queue_ready, *ops(db)):
            with pytest.raises(sqlite3.OperationalError):
                fn()
        assert db.verify_audit_chain() == (True, "Audit log is empty")
    assert TelemetryExporter(db).export_metrics_json()["governance_and_topology"]["total_tasks"] == 0
    db.init_rig(scaffold_icm=False)
    assert db.create_task("TSK-REC", "Recovered")["id"] == "TSK-REC"


def test_create_task_field_specific_errors_and_tags_validation(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "rig" / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    bad_by_field = {
        "title": ("", "   ", "\t\r\n", "a\x00b", "a\x1bb", None, 42),
        "archetype": ("", "   ", "a\nb", "a\tb", "a\x00b", None, 99),
        "priority": (True, False, 1.5, "10", None, 1 << 63, -(1 << 63) - 1),
        "parent_id": ("", "   ", "../bad", "EPC\n1", "TSK-1", 123, True),
        "author": ("", "   ", "ag\n1", "ag\r1", "ag\t1", "ag\x001", 123, False),
        "tags": (123, True, ["core"], b"core", "", "   ", ",", "core,", ",core", "a,,b", "a,  ,b", "a\nb", "a\rb", "a\tb", "a\x00b", "a\x1bb", "a\x7fb"),
    }
    for expected_field, bad_vals in bad_by_field.items():
        for val in bad_vals:
            with pytest.raises(ValueError) as exc_info:
                db.create_task("TSK-1", **{"title": "Valid Title", expected_field: val})  # type: ignore[arg-type]
            msg = str(exc_info.value)
            assert expected_field in msg and all(other not in msg for other in bad_by_field if other != expected_field)
    assert db.list_tasks() == [] and not db.get_task_file("TSK-1").exists() and db.verify_audit_chain() == (True, "Audit log is empty")
    created = db.create_task("TSK-1", "Line 1\nLine 2", parent_id="EPIC-1", tags="core, test, v2", author="lead:arch", priority=0)
    assert created["tags"] == "core, test, v2" and created["parent_id"] == "EPIC-1" and db.verify_audit_chain()[0] is True


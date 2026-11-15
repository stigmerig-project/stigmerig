"""Tests for WORM audit tamper detection, claim lease guards, and atomic trap persistence."""

import concurrent.futures
import hashlib
import json
from pathlib import Path

import pytest
from stigmerig.db import StigmeRigDB


def test_audit_missing_dropped_and_deleted_rows(tmp_path: Path):
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    assert db.verify_audit_chain() == (False, "audit.db does not exist")
    assert not db.audit_db_path.exists()
    db.init_rig(scaffold_icm=False)
    for i in range(1, 4):
        db.log_audit(f"TSK-{i}", "act", "AgentA", "verified", f"p{i}")
    with db.get_audit_conn() as conn:
        conn.execute("DROP TRIGGER prevent_audit_delete")
        for victim in (2, 1, 3):
            conn.execute("DELETE FROM audit_log WHERE id = ?", (victim,))
            conn.commit()
            assert db.verify_audit_chain()[0] is False
        conn.execute("DROP TABLE audit_log")
        conn.commit()
    assert db.verify_audit_chain() == (False, "audit_log table does not exist")


def test_audit_malformed_hashes_and_tier_a(tmp_path: Path):
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    db.log_audit("TSK-1", "act", "AgentA", "verified", "p1")
    with db.get_audit_conn() as conn:
        conn.execute("DROP TRIGGER prevent_audit_update")
        conn.execute("UPDATE audit_log SET payload_hash = 'bad' WHERE id = 1")
        conn.commit()
    ok, msg = db.verify_audit_chain()
    assert ok is False and "Hash mismatch at ID 1" in msg

    db_a = StigmeRigDB(rig_dir=tmp_path / "tier_a")
    db_a.rig_dir.mkdir(parents=True)
    h1, h2 = hashlib.sha256(b"e1").hexdigest(), hashlib.sha256(b"e2").hexdigest()
    with db_a.get_audit_conn() as conn:
        conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, previous_hash TEXT, hash TEXT)")
        conn.executemany("INSERT INTO audit_log VALUES (?, ?, ?)", [(1, "", h1), (2, h1, h2)])
        conn.commit()
    assert db_a.verify_audit_chain()[0] is True
    with db_a.get_audit_conn() as conn:
        conn.execute("UPDATE audit_log SET hash = 'not-hex' WHERE id = 2")
        conn.commit()
    assert db_a.verify_audit_chain()[0] is False
    with db_a.get_audit_conn() as conn:
        conn.execute("DELETE FROM audit_log WHERE id = 1")
        conn.execute("UPDATE audit_log SET previous_hash = '', hash = ? WHERE id = 2", (h2,))
        conn.commit()
    assert db_a.verify_audit_chain()[0] is False


def test_claim_guards_and_concurrent_trap_persistence(tmp_path: Path):
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    db.create_task("TSK-VAL", "Lease validation")
    for bad_agent, bad_ttl in [("", 60), ("   ", 60), ("AgentA", 0), ("AgentA", -5), ("AgentA", False)]:
        with pytest.raises(ValueError):
            db.claim_task("TSK-VAL", agent=bad_agent, ttl_seconds=bad_ttl)

    for idx in range(12):
        db.create_task(f"TSK-CONC-{idx}", f"Task {idx}")
    blocker = "Database migration lock timeout during schema upgrade"
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: db.fail_task(f"TSK-CONC-{i}", f"Worker{i}", blocker), range(12)))

    doc = json.loads((db.rig_dir / "traps.json").read_text(encoding="utf-8"))
    assert len(doc["traps"]) == 1 and doc["traps"][0]["occurrences"] == 12
    assert doc["traps"][0]["promotion_candidate"] is True
    assert list(db.rig_dir.glob("*.tmp")) == []
    with db._lock:
        assert db.sync_board().exists()

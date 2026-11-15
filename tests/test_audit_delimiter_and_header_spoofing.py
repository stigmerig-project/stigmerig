"""Hermetic tests for Tier B colon-delimiter spoofing, header validation, and Tier A previous_hash guards."""

import hashlib
from pathlib import Path
import pytest
from stigmerig.db import GENESIS_HASH, StigmeRigDB


def _tier_b_hash(r: dict) -> str:
    hdr = f"{r['timestamp']}:{r['task_id']}:{r['action']}:{r['agent']}:{r['verification_status']}:{r['payload_hash']}:{r['previous_hash']}"
    return hashlib.sha256(hdr.encode("utf-8")).hexdigest()


def test_log_audit_validates_fields_and_preserves_colon_agent_hash(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "rig")
    db.init_rig(scaffold_icm=False)
    for tid, act, ag, vs in [("", "act", "Ag", "ok"), ("  ", "act", "Ag", "ok"), ("../x", "act", "Ag", "ok"), ("TSK:1", "act", "Ag", "ok"), ("TSK-1", "act", "", "ok"), ("TSK-1", "act", " \t\n ", "ok"), ("TSK-1", "", "Ag", "ok"), ("TSK-1", "  ", "Ag", "ok"), ("TSK-1", "a:b", "Ag", "ok"), ("TSK-1", "a\nb", "Ag", "ok"), ("TSK-1", "a\rb", "Ag", "ok"), ("TSK-1", "act", "Ag", ""), ("TSK-1", "act", "Ag", "  "), ("TSK-1", "act", "Ag", "bad:verified"), ("TSK-1", "act", "Ag", "ok\n"), ("TSK-1", "act", "Ag", "ok\r")]:
        with pytest.raises(ValueError):
            db.log_audit(tid, act, ag, vs, "payload")
    assert db.verify_audit_chain() == (True, "Audit log is empty")
    eh = db.log_audit("TSK-1", "task_completed", "lead-seat:coder", "verified", "p1")
    with db.get_audit_conn() as conn:
        row = dict(conn.execute("SELECT * FROM audit_log WHERE id = 1").fetchone())
    assert eh == row["entry_hash"] == _tier_b_hash(row) and db.verify_audit_chain()[0] is True


def test_verify_audit_chain_defeats_colon_shift_and_header_spoofing(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "spoof")
    db.init_rig(scaffold_icm=False)
    db.log_audit("TSK-1", "task_blocked", "lead:TSK-2:task_completed:verified", "failed", "p1")
    db.log_audit("TSK-1", "task_claimed", "lead-seat:coder", "in_progress", "p2")
    with db.get_audit_conn() as conn:
        conn.executescript("DROP TRIGGER prevent_audit_update; DROP TRIGGER prevent_audit_delete;")
        orig = dict(conn.execute("SELECT * FROM audit_log WHERE id = 1").fetchone())
        conn.execute("UPDATE audit_log SET timestamp = ? WHERE id = 1", (b"123",))
        conn.commit()
    assert db.verify_audit_chain() == (False, "Invalid Tier B header column type at ID 1")
    for patch in [
        {"timestamp": f"{orig['timestamp']}:TSK-1:task_blocked:lead", "task_id": "TSK-2", "action": "task_completed", "agent": "verified"},
        {"task_id": "TSK-1:task_blocked:lead", "action": "TSK-2", "agent": "task_completed:verified"},
        {"action": "task_blocked:lead", "agent": "TSK-2:task_completed:verified"},
        {"agent": "lead:TSK-2:task_completed", "verification_status": "verified:failed"},
        {"agent": "lead:TSK-2:task_completed", "verification_status": "verified", "payload_hash": f"failed:{orig['payload_hash']}"},
    ]:
        c = {**orig, **patch}
        assert _tier_b_hash(c) == orig["entry_hash"]
        with db.get_audit_conn() as conn:
            conn.execute("UPDATE audit_log SET timestamp=?, task_id=?, action=?, agent=?, verification_status=?, payload_hash=? WHERE id=1", (c["timestamp"], c["task_id"], c["action"], c["agent"], c["verification_status"], c["payload_hash"]))
            conn.commit()
        assert db.verify_audit_chain()[0] is False
    with db.get_audit_conn() as conn:
        conn.executescript("DELETE FROM audit_log WHERE id = 2; UPDATE sqlite_sequence SET seq = 1 WHERE name = 'audit_log';")
        for patch in [{"timestamp": "2026-11-13 20:00:00Z"}, {"timestamp": "2026-11-13T20:00:00+00:00"}, {"timestamp": "2026-02-30T00:00:00Z"}, {"timestamp": "2026-1-1T0:0:0Z"}, {"task_id": "../bad"}, {"agent": "   "}, {"action": "  "}, {"action": "a:b"}, {"action": "act\n1"}, {"action": "act\r1"}, {"verification_status": "  "}, {"verification_status": "bad:verified"}, {"verification_status": "verified\n"}, {"verification_status": "verified\r"}]:
            c = {**orig, **patch}
            conn.execute("UPDATE audit_log SET timestamp=?, task_id=?, action=?, agent=?, verification_status=?, payload_hash=?, entry_hash=? WHERE id=1", (c["timestamp"], c["task_id"], c["action"], c["agent"], c["verification_status"], c["payload_hash"], _tier_b_hash(c)))
            conn.commit()
            assert db.verify_audit_chain()[0] is False


def test_tier_a_non_genesis_previous_hash_hex_validation(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "tier_a")
    db.rig_dir.mkdir(parents=True)
    h1, h2 = hashlib.sha256(b"r1").hexdigest(), hashlib.sha256(b"r2").hexdigest()
    with db.get_audit_conn() as conn:
        conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, previous_hash TEXT, hash TEXT)")
        conn.executemany("INSERT INTO audit_log VALUES (?, ?, ?)", [(1, GENESIS_HASH, h1), (2, h1, h2)])
        conn.commit()
    assert db.verify_audit_chain()[0] is True
    for bad_prev in ("", None, "not-hex", "g" * 64, "0" * 63):
        with db.get_audit_conn() as conn:
            conn.execute("UPDATE audit_log SET previous_hash = ? WHERE id = 2", (bad_prev,))
            conn.commit()
        assert db.verify_audit_chain() == (False, "Invalid previous_hash format at ID 2")

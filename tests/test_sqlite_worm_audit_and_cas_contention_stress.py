"""Stress tests for concurrent SQLite WAL WORM audit verification, tamper matrices, and seat attribution."""

from __future__ import annotations
import concurrent.futures, hashlib, json, sqlite3, threading
from pathlib import Path
import pytest
from stigmerig.db import GENESIS_HASH, StigmeRigDB, _is_self_approval, derive_seat

SEAT_ENV_KEYS = ("STIGMERIG_SEAT", "AGENT_SEAT", "SEAT")


def _build_writer_schedule(worker_idx: int, task_count: int = 24) -> list[tuple[str, bool]]:
    """Pure schedule rotating task traversal across writers with deterministic fail targets."""
    return [(f"TSK-RW-{(step + worker_idx * 2) % task_count:02d}", ((step + worker_idx * 2) % task_count) % 4 == 0) for step in range(task_count)]


def _tier_b_entry_hash(row: dict[str, object]) -> str:
    """Pure SHA-256 Tier B entry_hash calculator over colon-delimited audit header fields."""
    return hashlib.sha256(f"{row['timestamp']}:{row['task_id']}:{row['action']}:{row['agent']}:{row['verification_status']}:{row['payload_hash']}:{row['previous_hash']}".encode("utf-8")).hexdigest()


def _expected_completion_record(agent: str, verifier: str | None, seat: str, session_id: str) -> tuple[dict[str, object], str]:
    """Pure builder returning expected task_events completion payload and SHA-256 payload_hash."""
    payload: dict[str, object] = {"verifier": verifier, "implementer": agent, "seat": seat, "session_id": session_id}
    return payload, hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def test_concurrent_twelve_writers_and_four_audit_readers_stress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for env_key in SEAT_ENV_KEYS: monkeypatch.delenv(env_key, raising=False)
    db, barrier, writers_done = StigmeRigDB(rig_dir=tmp_path / ".stigmerig"), threading.Barrier(16, timeout=10.0), threading.Event()
    db.init_rig(scaffold_icm=False); [db.create_task(f"TSK-RW-{idx:02d}", f"Concurrent WAL task {idx:02d}") for idx in range(24)]

    def _writer(w: int) -> None:
        wdb, agent = StigmeRigDB(db.rig_dir), f"seat-{w:02d}/writer-{w:02d}"
        barrier.wait()
        wdb.log_audit(f"TSK-RW-{w * 2:02d}", "probe_started", agent, "in_progress", json.dumps({"writer": w}))
        for tid, should_fail in _build_writer_schedule(w, 24):
            if wdb.claim_task(tid, agent=agent, ttl_seconds=600):
                assert (wdb.fail_task(tid, agent, "Concurrent sqlite wal stress blocker") if should_fail else wdb.complete_task(tid, agent, f"qa-{w:02d}@audit")[0]) is True
        wdb.log_audit(f"TSK-RW-{w * 2 + 1:02d}", "probe_finished", agent, "verified", json.dumps({"writer": w}))

    def _reader() -> list[int]:
        rdb, counts = StigmeRigDB(db.rig_dir), []
        barrier.wait()
        while not writers_done.is_set() or len(counts) < 6:
            with rdb._lock: ok, msg = rdb.verify_audit_chain()
            assert ok is True and msg.startswith("Verified ") and len(rdb.list_tasks()) == 24, msg
            counts.append(int(msg.split()[1]))
        return counts

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        w_futs, r_futs = [pool.submit(_writer, w) for w in range(12)], [pool.submit(_reader) for _ in range(4)]
        [f.result() for f in w_futs]; writers_done.set()
        reader_runs = [f.result() for f in r_futs]
    assert all(len(r) >= 6 and r == sorted(r) and 24 <= r[0] and r[-1] <= 96 for r in reader_runs)
    assert StigmeRigDB(db.rig_dir).verify_audit_chain() == (True, "Verified 96 audit entries with intact hash chain")
    assert len(db.list_tasks(status="done")) == 18 and len(db.list_tasks(status="blocked")) == 6 and [p for p in db.rig_dir.rglob("*") if p.name.endswith(".tmp")] == []


def test_audit_worm_tamper_matrix_and_tier_a_genesis_variants(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "tamper"); db.init_rig(scaffold_icm=False)
    for idx in range(1, 5): db.log_audit(f"TSK-TM-{idx}", "state_saved", f"seat-{idx}/agent", "verified", f"body-{idx}")
    assert db.verify_audit_chain() == (True, "Verified 4 audit entries with intact hash chain")
    with db.get_audit_conn() as conn:
        for stmt in ("UPDATE audit_log SET action = 'tampered' WHERE id = 2", "DELETE FROM audit_log WHERE id = 4"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"): conn.execute(stmt)
        conn.executescript("DROP TRIGGER prevent_audit_update; DROP TRIGGER prevent_audit_delete; DROP TRIGGER verify_hash_chain; DROP TRIGGER enforce_contiguous_audit_id;")
        base = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id ASC")]
    r2_forged, r3_forged = {**base[1], "payload_hash": base[3]["payload_hash"]}, {**base[2], "previous_hash": base[0]["entry_hash"]}
    tampers = [("UPDATE audit_log SET previous_hash = CASE id WHEN 2 THEN ? WHEN 3 THEN ? END WHERE id IN (2, 3)", (base[2]["previous_hash"], base[1]["previous_hash"]), "Hash break at ID 2"),
               ("UPDATE audit_log SET previous_hash = ?, entry_hash = ? WHERE id = 3", (r3_forged["previous_hash"], _tier_b_entry_hash(r3_forged)), "Hash break at ID 3"),
               ("UPDATE audit_log SET payload_hash = CASE id WHEN 2 THEN ? WHEN 4 THEN ? END WHERE id IN (2, 4)", (base[3]["payload_hash"], base[1]["payload_hash"]), "Hash mismatch at ID 2"),
               ("UPDATE audit_log SET payload_hash = ?, entry_hash = ? WHERE id = 2", (r2_forged["payload_hash"], _tier_b_entry_hash(r2_forged)), "Hash break at ID 3"),
               *[("UPDATE audit_log SET previous_hash = ?, entry_hash = ? WHERE id = 1", (g, _tier_b_entry_hash({**base[0], "previous_hash": g})), "Hash break at genesis ID 1") for g in ("0" * 63 + "1", "8" + "0" * 63, "")],
               ("DELETE FROM audit_log WHERE id = 4", (), "seq=4, rows=3"), ("UPDATE sqlite_sequence SET seq = 9 WHERE name = 'audit_log'", (), "seq=9, rows=4")]
    for sql, prm, expected in tampers:
        with db.get_audit_conn() as conn:
            conn.executescript("DELETE FROM audit_log; UPDATE sqlite_sequence SET seq = 4 WHERE name = 'audit_log';")
            conn.executemany("INSERT INTO audit_log VALUES (:id,:timestamp,:task_id,:action,:agent,:verification_status,:payload_hash,:previous_hash,:entry_hash)", base)
            conn.execute(sql, prm); conn.commit()
        ok, msg = db.verify_audit_chain(); assert ok is False and expected in msg, msg
    db_a, (h1, h2, h3) = StigmeRigDB(rig_dir=tmp_path / "tier_a"), [hashlib.sha256(f"a-{k}".encode()).hexdigest() for k in (1, 2, 3)]; db_a.rig_dir.mkdir(parents=True)
    with db_a.get_audit_conn() as conn:
        conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, previous_hash TEXT, hash TEXT)")
        for genesis_val in (GENESIS_HASH, "", None):
            conn.execute("DELETE FROM audit_log"); conn.executemany("INSERT INTO audit_log VALUES (?, ?, ?)", [(1, genesis_val, h1), (2, h1, h2), (3, h2, h3)]); conn.commit()
            assert db_a.verify_audit_chain() == (True, "Verified 3 audit entries with intact hash chain")
        conn.execute("UPDATE audit_log SET previous_hash = ? WHERE id = 1", ("0" * 63 + "1",)); conn.commit()
    assert db_a.verify_audit_chain()[0] is False


def test_derive_seat_precedence_and_cross_delimiter_self_approval_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for env_key in SEAT_ENV_KEYS: monkeypatch.delenv(env_key, raising=False)
    assert (derive_seat("s/u@h:r"), derive_seat("u@h:r"), derive_seat("c:w"), derive_seat(" solo "), derive_seat(None), derive_seat("")) == ("s", "u", "c", "solo", "default", "default")
    for env_key, env_val, exp in [("SEAT", " env-low ", "env-low"), ("AGENT_SEAT", " env-mid ", "env-mid"), ("STIGMERIG_SEAT", " env-top ", "env-top")]:
        monkeypatch.setenv(env_key, env_val); assert derive_seat("s/u@h:r") == exp
    assert all(_is_self_approval(a, v) is True for a, v in [("seat/a", "seat:b"), ("seat/a", "SEAT@b"), ("user@host", "USER"), ("USER", "team:user"), ("org/shared", "dept:SHARED")])
    assert all(_is_self_approval(a, v) is False for a, v in [("seat-a/a", "seat-b:b"), ("user1@h1", "user2@h2"), ("solo-a", "solo-b"), (None, "rev"), ("act", "")])
    db, expected = StigmeRigDB(rig_dir=tmp_path / "seat_rig"), {}; db.init_rig(scaffold_icm=False)
    matrix = [("TSK-ST-1", {"STIGMERIG_SEAT": "env-top", "AGENT_SEAT": "env-mid", "SEAT": "env-low"}, "seat/a", ("seat:b", "SEAT@c", "A"), "qa-team:rev-1", False, "env-top"),
              ("TSK-ST-2", {"AGENT_SEAT": "env-mid", "SEAT": "env-low"}, "user@host:dev", ("USER", "dev@node"), "indep@verify", False, "env-mid"),
              ("TSK-ST-3", {"SEAT": "env-low"}, "colon-s:worker", ("COLON-S/x", "worker@y"), "auditor/lead", False, "env-low"),
              ("TSK-ST-4", {}, "slash-s/dev@host:tag", ("slash-s:qa", "qa/tag"), "gatekeeper:ok", False, "slash-s"),
              ("TSK-ST-5", {}, "at-s@host:tag", ("AT-S:qa", "tag/qa"), "external/rev", False, "at-s"),
              ("TSK-ST-6", {}, "colon-only:dev", ("dev", "colon-only/qa"), None, True, "colon-only")]
    for tid, env_map, agent, bad_verifiers, valid_verifier, allow_self, seat in matrix:
        for k in SEAT_ENV_KEYS: monkeypatch.setenv(k, env_map[k]) if k in env_map else monkeypatch.delenv(k, raising=False)
        db.create_task(tid, f"Seat matrix {tid}"); assert db.claim_task(tid, agent=agent, ttl_seconds=600) is True
        for bad_v in bad_verifiers:
            with pytest.raises(PermissionError, match="Self-approval forbidden"): db.complete_task(tid, agent=agent, verifier=bad_v)
        assert db.complete_task(tid, agent=agent, verifier=valid_verifier, allow_self_approval=allow_self, session_id=f"sess-{tid}")[0] is True
        expected[tid] = _expected_completion_record(agent, valid_verifier, seat, f"sess-{tid}")
    with db.get_queue_conn() as qconn, db.get_audit_conn() as aconn:
        ev_rows = {r["task_id"]: r["payload"] for r in qconn.execute("SELECT task_id, payload FROM task_events WHERE event_type = 'completed'")}
        aud_rows = {r["task_id"]: dict(r) for r in aconn.execute("SELECT task_id, payload_hash, verification_status FROM audit_log WHERE action = 'task_completed'")}
    assert len(ev_rows) == 6 and len(aud_rows) == 6 and db.verify_audit_chain()[0] is True
    assert all(json.loads(ev_rows[t]) == expected[t][0] and aud_rows[t]["payload_hash"] == expected[t][1] == hashlib.sha256(ev_rows[t].encode("utf-8")).hexdigest() for t in expected)

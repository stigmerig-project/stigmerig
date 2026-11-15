"""Multi-threaded SQLite stress tests for SHA-256 evidence retries, trap ring-buffers, and task layouts."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import threading
import pytest
from stigmerig.db import StigmeRigDB


def _expected_evidence_binding(agent: str, verifier: str, session_id: str, artifacts: list[tuple[str, bytes]]) -> tuple[dict[str, object], str]:
    """Pure builder returning expected completion payload and SHA-256 audit payload_hash."""
    payload: dict[str, object] = {"verifier": verifier, "implementer": agent, "seat": agent.split("/")[0], "session_id": session_id, "evidence": [{"path": r, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for r, b in artifacts]}
    return payload, hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def _build_trap_schedule() -> list[tuple[str, str, str]]:
    """Pure 16-task failure schedule spanning multi-variant signatures and stop-word-only short reasons."""
    dl = ("Task failed because sqlite wal checkpoint deadlock!", "Blocked over SQLITE checkpoint deadlock error", "Unable during sqlite tx checkpoint deadlock")
    cs = ("Failed during schema migration checksum", "Blocked over schema migration checksum error", "Task unable: schema migration checksum")
    sw = ("Task blocked!", "task BLOCKED: a?")
    return [(f"TSK-TRP-{i:02d}", f"TrapWorker-{i:02d}", dl[i % 3] if i < 11 else (cs[i - 11] if i < 14 else sw[i - 14])) for i in range(16)]


def test_multithreaded_evidence_binding_retry_and_payload_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for env_key in ("STIGMERIG_SEAT", "AGENT_SEAT", "SEAT"):
        monkeypatch.delenv(env_key, raising=False)
    ws, db = tmp_path / "ws", StigmeRigDB(rig_dir=tmp_path / "ws" / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    for p, raw in [(ws / ".env.staging", b"1"), (ws / "cfg/.env.local", b"2"), (ws / ".stigmerig/sec.txt", b"r"), (ws / ".worktrees/w1/f.txt", b"w"), (ws / "art/zero.bin", b""), (tmp_path / "out.txt", b"x")]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    rejected = [(".env.staging", ValueError, "denied pattern"), ("cfg/.env.local", ValueError, "denied pattern"), (".stigmerig/sec.txt", ValueError, "denied pattern"),
                (".worktrees/w1/f.txt", ValueError, "denied pattern"), ("art/zero.bin", ValueError, "non-empty"), ("art/missing.txt", FileNotFoundError, "not found"), ("../out.txt", ValueError, "escapes allowed root")]
    expected: dict[str, tuple[dict[str, object], str]] = {}
    for i in range(12):
        tid, agent = f"TSK-EV-{i:02d}", f"Seat{i}/Worker-{i:02d}"
        db.create_task(tid, f"Evidence stress {i:02d}")
        assert db.claim_task(tid, agent=agent, ttl_seconds=900) is True
        ra, ba, rb, bb = f"art/ev_{i:02d}_a.txt", f"alpha-{i:02d}\n".encode(), f"art/ev_{i:02d}_b.bin", f"beta-{i:02d}-".encode() * 9000
        (ws / ra).write_bytes(ba)
        (ws / rb).write_bytes(bb)
        expected[tid] = _expected_evidence_binding(agent, "QA-Lead", f"sess-{i:02d}", [(ra, ba), (rb, bb)])
    barrier = threading.Barrier(12, timeout=5.0)

    def _worker(i: int) -> None:
        tid, agent, ldb, valid = f"TSK-EV-{i:02d}", f"Seat{i}/Worker-{i:02d}", StigmeRigDB(db.rig_dir), [f"art/ev_{i:02d}_a.txt", f"art/ev_{i:02d}_b.bin"]
        barrier.wait()
        if i < len(rejected):
            bad_path, exc_cls, err_sub = rejected[i]
            with pytest.raises(exc_cls, match=err_sub):
                ldb.complete_task(tid, agent, "QA-Lead", session_id=f"sess-{i:02d}", evidence_paths=[valid[0], bad_path])
            kept = ldb.get_task(tid)
            assert kept is not None and (kept["status"], kept["claimed_by"], kept["version"]) == ("working", agent, 1) and kept["lease_until"] is not None
        ok, msg = ldb.complete_task(tid, agent, "QA-Lead", session_id=f"sess-{i:02d}", evidence_paths=valid)
        assert ok is True, msg

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        [f.result() for f in [pool.submit(_worker, i) for i in range(12)]]
    with db.get_queue_conn() as qconn, db.get_audit_conn() as aconn:
        ev_rows = {r["task_id"]: json.loads(r["payload"]) for r in qconn.execute("SELECT task_id, payload FROM task_events WHERE event_type = 'completed'")}
        aud_rows = [dict(r) for r in aconn.execute("SELECT task_id, payload_hash, verification_status FROM audit_log WHERE action = 'task_completed'")]
    assert len(ev_rows) == 12 and len(aud_rows) == 12 and all((t["status"], t["version"], t["lease_until"]) == ("done", 2, None) for t in db.list_tasks())
    assert all(ev_rows[r["task_id"]] == expected[r["task_id"]][0] and r["payload_hash"] == expected[r["task_id"]][1] and r["verification_status"] == "verified" for r in aud_rows)
    assert [p for p in db.rig_dir.rglob("*") if p.name.endswith(".tmp")] == [] and db.verify_audit_chain()[0] is True


def test_sixteen_thread_fail_task_trap_ring_buffer_and_dismissed_isolation(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "ws" / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    dismissed = {"id": "TRAP-004", "title": "Sqlite wal checkpoint deadlock", "reason": "Sqlite wal checkpoint deadlock", "trigger_pattern": "sqlite checkpoint deadlock", "status": "dismissed", "occurrences": 5, "promotion_candidate": False, "source_traces": ["Task OLD-1 (Legacy)"]}
    (db.rig_dir / "traps.json").write_text(json.dumps({"version": "1.0.0", "traps": [dismissed]}), encoding="utf-8")
    schedule = _build_trap_schedule()
    for tid, agent, _ in schedule:
        db.create_task(tid, f"Trap {tid}")
        assert db.claim_task(tid, agent=agent, ttl_seconds=600) is True
    barrier = threading.Barrier(16, timeout=5.0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        assert all(f.result() is True for f in [pool.submit(lambda r=r: (barrier.wait(), StigmeRigDB(db.rig_dir).fail_task(*r))[1]) for r in schedule])
    traps = json.loads((db.rig_dir / "traps.json").read_text(encoding="utf-8"))["traps"]
    assert traps[0] == dismissed and [t["id"] for t in traps] == ["TRAP-004", "TRAP-005", "TRAP-006", "TRAP-007"]
    by_pat = {t["trigger_pattern"]: t for t in traps[1:]}
    dl, cs, sw = by_pat["sqlite checkpoint deadlock"], by_pat["schema migration checksum"], by_pat["task blocked"]
    assert (dl["occurrences"], len(dl["source_traces"]), len(set(dl["source_traces"])), dl["promotion_candidate"]) == (11, 10, 10, True)
    assert (cs["occurrences"], len(cs["source_traces"]), cs["promotion_candidate"]) == (3, 3, True)
    assert (sw["occurrences"], len(sw["source_traces"]), sw["promotion_candidate"]) == (2, 2, False)
    assert len(db.list_tasks(status="blocked")) == 16 and [p for p in db.rig_dir.rglob("*") if p.name.endswith(".tmp")] == [] and db.verify_audit_chain()[0] is True


def test_concurrent_create_task_layouts_ordering_board_and_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "ws" / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    for tid in ("TSK-TIE-A", "TSK-LAY-00", "TSK-LAY-03"):
        (db.tasks_dir / tid).mkdir(parents=True)
        (db.tasks_dir / tid / "context.md").write_text(f"# Custom {tid}\n", encoding="utf-8")
    for tid in ("TSK-LAY-01", "TSK-LAY-04"):
        (db.tasks_dir / tid).mkdir(parents=True)
    for ts, tid in (("2026-11-14T08:10:00Z", "TSK-TIE-B"), ("2026-11-14T08:01:00Z", "TSK-TIE-A")):
        monkeypatch.setattr("stigmerig.db.utc_now_iso", lambda val=ts: val)
        db.create_task(tid, f"Tie {tid}", priority=5)
    monkeypatch.undo()
    prios, barrier = [40, 20, -10, 30, 15, 50, 25, 35], threading.Barrier(8, timeout=5.0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        [f.result() for f in [pool.submit(lambda i=i: (barrier.wait(), StigmeRigDB(db.rig_dir).create_task(f"TSK-LAY-{i:02d}", f"Lay {i:02d}", priority=prios[i]))[1]) for i in range(8)]]
    expected_ids = ["TSK-LAY-02", "TSK-TIE-A", "TSK-TIE-B", "TSK-LAY-04", "TSK-LAY-01", "TSK-LAY-06", "TSK-LAY-03", "TSK-LAY-07", "TSK-LAY-00", "TSK-LAY-05"]
    assert [t["id"] for t in db.list_tasks()] == expected_ids
    assert all(Path(db.get_task(t)["context_path"]).as_posix() == f".stigmerig/tasks/{t}/context.md" and (db.tasks_dir / t / "context.md").is_file() and not (db.tasks_dir / f"{t}.md").exists() for t in ("TSK-TIE-A", "TSK-LAY-00", "TSK-LAY-03", "TSK-LAY-01", "TSK-LAY-04"))
    assert all((db.tasks_dir / t / "context.md").read_text(encoding="utf-8") == f"# Custom {t}\n" for t in ("TSK-TIE-A", "TSK-LAY-00", "TSK-LAY-03"))
    assert all(Path(db.get_task(t)["context_path"]).as_posix() == f".stigmerig/tasks/{t}.md" and (db.tasks_dir / f"{t}.md").is_file() for t in ("TSK-TIE-B", "TSK-LAY-02", "TSK-LAY-05", "TSK-LAY-06", "TSK-LAY-07"))
    for board in (db.board_path.read_text(encoding="utf-8"), db.sync_board().read_text(encoding="utf-8")):
        assert "## To Do / Backlog (10)" in board and re.findall(r"\| `(TSK-[A-Z0-9-]+)` \|", board) == expected_ids
    ok, msg = db.verify_audit_chain()
    assert ok is True and "Verified 10 audit entries" in msg and [p for p in db.rig_dir.rglob("*") if p.name.endswith(".tmp")] == []

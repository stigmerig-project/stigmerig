"""Hermetic multi-threaded SQLite CAS lease contention, takeover, and split-brain stress tests."""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import threading
import pytest
from stigmerig.db import StigmeRigDB


def _build_worker_schedule(worker_idx: int, task_count: int = 20) -> list[tuple[str, bool]]:
    """Pure schedule builder assigning rotated task traversal and deterministic fail targets."""
    return [(f"TSK-MIX-{(i + worker_idx * 2) % task_count:02d}", ((i + worker_idx * 2) % task_count) % 4 == 0) for i in range(task_count)]


def _init_rig(tmp_path: Path) -> StigmeRigDB:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    return db


class _MidFlightStaleDB(StigmeRigDB):
    def __init__(self, rig_dir: Path, read_ready: threading.Event, release_gate: threading.Event):
        super().__init__(rig_dir=rig_dir)
        self._read_ready, self._release_gate = read_ready, release_gate

    def get_task(self, task_id: str) -> dict[str, object] | None:
        row = super().get_task(task_id)
        self._read_ready.set()
        assert self._release_gate.wait(timeout=5.0), "Timed out waiting for takeover gate release"
        return row


def test_twelve_thread_barrier_cas_claim_contention(tmp_path: Path) -> None:
    db = _init_rig(tmp_path)
    db.create_task("TSK-CAS-01", "Barrier CAS contention target")
    barrier = threading.Barrier(12, timeout=5.0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(lambda i=i: (barrier.wait(), f"Worker-{i:02d}", StigmeRigDB(db.rig_dir).claim_task("TSK-CAS-01", f"Worker-{i:02d}", 600))[1:]) for i in range(12)]
        outcomes = [f.result() for f in futs]
    assert sum(ok is True for _, ok in outcomes) == 1 and sum(ok is False for _, ok in outcomes) == 11
    winner = next(agent for agent, ok in outcomes if ok is True)
    task = db.get_task("TSK-CAS-01")
    assert task is not None and task["status"] == "working" and task["claimed_by"] == winner and task["version"] == 1
    with db.get_audit_conn() as conn:
        rows = conn.execute("SELECT agent FROM audit_log WHERE task_id = 'TSK-CAS-01' AND action = 'task_claimed'").fetchall()
    assert len(rows) == 1 and rows[0]["agent"] == winner and db.verify_audit_chain()[0] is True


def test_expired_lease_takeover_prevents_stale_split_brain(tmp_path: Path) -> None:
    db = _init_rig(tmp_path)
    db.create_task("TSK-EXP-01", "Expired lease takeover target")
    assert db.claim_task("TSK-EXP-01", agent="StaleAgent", ttl_seconds=60) is True
    with db.get_queue_conn() as conn:
        conn.execute("UPDATE tasks SET lease_until = '2020-01-01T00:00:00Z' WHERE id = 'TSK-EXP-01'")
        conn.commit()
    read_ready, release_gate = threading.Event(), threading.Event()
    race_bar, post_bar = threading.Barrier(6, timeout=5.0), threading.Barrier(4, timeout=5.0)

    def _stale_fail(target_db: StigmeRigDB, gate: threading.Barrier | None = None) -> str:
        if gate is not None:
            gate.wait()
        with pytest.raises(PermissionError) as exc_info:
            target_db.fail_task("TSK-EXP-01", agent="StaleAgent", reason="Stale split-brain fail")
        return str(exc_info.value)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        mid_fail = pool.submit(_stale_fail, _MidFlightStaleDB(db.rig_dir, read_ready, release_gate))
        assert read_ready.wait(timeout=5.0)
        c_futs = [pool.submit(lambda a=f"Takeover-{i}": (race_bar.wait(), a, StigmeRigDB(db.rig_dir).claim_task("TSK-EXP-01", a, 600))[1:]) for i in range(4)]
        sc_futs = [pool.submit(lambda: (race_bar.wait(), StigmeRigDB(db.rig_dir).complete_task("TSK-EXP-01", "StaleAgent", "VerifierX"))[1]) for _ in range(2)]
        claim_res, stale_comp = [f.result() for f in c_futs], [f.result() for f in sc_futs]
        release_gate.set()
        pf_futs = [pool.submit(_stale_fail, StigmeRigDB(db.rig_dir), post_bar) for _ in range(2)]
        pc_futs = [pool.submit(lambda: (post_bar.wait(), StigmeRigDB(db.rig_dir).complete_task("TSK-EXP-01", "StaleAgent", "VerifierX"))[1]) for _ in range(2)]
        fail_msgs, post_comp = [mid_fail.result(), *(f.result() for f in pf_futs)], [f.result() for f in pc_futs]
    assert sum(ok is True for _, ok in claim_res) == 1 and sum(ok is False for _, ok in claim_res) == 3
    winner = next(agent for agent, ok in claim_res if ok is True)
    assert all(ok is False for ok, _ in [*stale_comp, *post_comp])
    assert "Concurrent modification detected" in fail_msgs[0] and all("actively leased by" in m for m in fail_msgs[1:])
    state = db.get_task("TSK-EXP-01")
    assert state is not None and state["status"] == "working" and state["claimed_by"] == winner and state["version"] == 2
    assert not (db.rig_dir / "traps.json").exists() and db.verify_audit_chain()[0] is True


def test_ten_thread_mixed_lifecycle_stress(tmp_path: Path) -> None:
    db = _init_rig(tmp_path)
    for idx in range(20):
        db.create_task(f"TSK-MIX-{idx:02d}", f"Mixed stress task {idx:02d}")
    start_barrier = threading.Barrier(10, timeout=5.0)

    def _worker(worker_idx: int) -> None:
        agent, local_db = f"MixWorker-{worker_idx:02d}", StigmeRigDB(rig_dir=db.rig_dir)
        start_barrier.wait()
        for step, (tid, should_fail) in enumerate(_build_worker_schedule(worker_idx, 20)):
            if local_db.claim_task(tid, agent=agent, ttl_seconds=300):
                assert local_db.claim_task(tid, agent=agent, ttl_seconds=900) is True
                local_db.sync_board()
                if should_fail:
                    assert local_db.fail_task(tid, agent=agent, reason="Sqlite migration lock contention timeout") is True
                else:
                    ok, msg = local_db.complete_task(tid, agent=agent, verifier="IndependentVerifier")
                    assert ok is True, msg
            elif step % 5 == 0:
                local_db.sync_board()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        [fut.result() for fut in [pool.submit(_worker, w) for w in range(10)]]
    done, blocked = db.list_tasks(status="done"), db.list_tasks(status="blocked")
    assert len(done) == 15 and len(blocked) == 5 and all(t["version"] == 3 for t in [*done, *blocked])
    traps = json.loads((db.rig_dir / "traps.json").read_text(encoding="utf-8"))["traps"]
    assert len(traps) == 1 and traps[0]["occurrences"] == 5 and traps[0]["promotion_candidate"] is True
    board = db.board_path.read_text(encoding="utf-8")
    assert "## Completed (15)" in board and "## Blocked / Human Hold (5)" in board
    assert [p for p in db.rig_dir.rglob("*") if p.name.endswith(".tmp")] == []
    audit_ok, audit_msg = db.verify_audit_chain()
    assert audit_ok is True and "Verified 80 audit entries" in audit_msg

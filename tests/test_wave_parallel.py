"""Parallel wave execution, clean-branch pruning, and merge-conflict tests."""

import json
from pathlib import Path
import subprocess
import threading

import pytest
from stigmerig.cli import main
from stigmerig.db import StigmeRigDB
from stigmerig.telemetry import TelemetryExporter
from stigmerig.wave import WaveRunner, WorktreeManager


@pytest.fixture
def rig_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True)
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    db = StigmeRigDB(rig_dir=repo / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    wt_mgr = WorktreeManager(repo_root=repo)
    return repo, db, wt_mgr, WaveRunner(db=db, worktree_mgr=wt_mgr)


def test_parallel_barrier_and_clean_branch_cleanup(rig_repo):
    repo, db, wt_mgr, runner = rig_repo
    barrier = threading.Barrier(3, timeout=5.0)
    seen_dirs = set()
    for i in (1, 2, 3):
        db.create_task(task_id=f"TSK-P{i}", title=f"Parallel {i}", author="Arch")

    def make_act(idx: int):
        def _act(wt_dir: Path) -> bool:
            seen_dirs.add(wt_dir)
            barrier.wait()
            if idx < 3:
                (wt_dir / f"out_{idx}.txt").write_text(f"v{idx}\n", encoding="utf-8")
            return True
        return _act

    specs = [{"task_id": f"TSK-P{i}", "agent": f"A{i}", "verifier": f"V{i}", "action": make_act(i)} for i in (1, 2, 3)]
    res = runner.run_wave(specs, max_concurrency=3)
    assert len(seen_dirs) == 3 and all(ok for ok, _ in res.values()), res
    assert (repo / "out_1.txt").exists() and (repo / "out_2.txt").exists()
    branches = subprocess.run(["git", "branch", "--list", "task/*"], cwd=repo, capture_output=True, text=True, check=True)
    assert branches.stdout.strip() == "" and not (wt_mgr.worktrees_dir / "TSK-P3").exists()
    assert all(db.get_task(f"TSK-P{i}")["status"] == "done" for i in (1, 2, 3)) and db.verify_audit_chain()[0]


def test_parallel_merge_conflict_aborts_and_marks_second_blocked(rig_repo):
    repo, db, _, runner = rig_repo
    barrier = threading.Barrier(2, timeout=5.0)
    for i in (1, 2):
        db.create_task(task_id=f"TSK-C{i}", title=f"Conflict {i}", author="Arch")

    def make_conflict(idx: int):
        def _act(wt_dir: Path) -> bool:
            barrier.wait()
            (wt_dir / "shared.txt").write_text(f"conflict {idx}\n", encoding="utf-8")
            return True
        return _act

    specs = [{"task_id": f"TSK-C{i}", "agent": f"A{i}", "verifier": f"V{i}", "action": make_conflict(i)} for i in (1, 2)]
    res = runner.run_wave(specs, max_concurrency=2)
    assert sorted(ok for ok, _ in res.values()) == [False, True], res
    assert sorted(db.get_task(f"TSK-C{i}")["status"] for i in (1, 2)) == ["blocked", "done"]
    assert any("Merge conflict" in msg for ok, msg in res.values() if not ok)
    assert not (repo / ".git" / "MERGE_HEAD").exists() and db.verify_audit_chain()[0]


def _is_clean_worktree_state(porcelain: str, branches: str, entries: list[Path]) -> bool:
    return len([b for b in porcelain.strip().split("\n\n") if b.strip()]) == 1 and "prunable" not in porcelain and branches.strip() == "" and entries == []


def _check_clean_git_state(repo: Path, wt_mgr: WorktreeManager) -> bool:
    out = [subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout for a in (("worktree", "list", "--porcelain"), ("branch", "--list", "task/*"))]
    return _is_clean_worktree_state(out[0], out[1], list(wt_mgr.worktrees_dir.iterdir()))


def _cleanup_and_reset_tasks(db: StigmeRigDB, wt_mgr: WorktreeManager, *task_ids: str) -> None:
    for tid in task_ids: wt_mgr.remove_worktree(tid, delete_branch=True)
    with db.get_queue_conn() as conn:
        conn.executemany("UPDATE tasks SET status = 'todo', claimed_by = NULL, lease_until = NULL WHERE id = ?", [(t,) for t in task_ids])
        conn.commit()


def test_parallel_wave_partial_failure_forensic_cleanup_and_retry_merge(rig_repo):
    repo, db, wt_mgr, runner = rig_repo
    for tid in ("TSK-R1", "TSK-R2", "TSK-R3", "TSK-R4"): db.create_task(tid, f"Wave task {tid}", author="Arch")
    b1, b2 = threading.Barrier(3, timeout=10.0), threading.Barrier(3, timeout=10.0)
    def w1_crash(wt: Path) -> bool:
        b1.wait()
        with wt_mgr._lock:
            (wt / "stale.txt").write_text("bad\n", encoding="utf-8")
            for c in (["git", "add", "stale.txt"], ["git", "commit", "--no-gpg-sign", "-m", "stale"]): subprocess.run(c, cwd=wt, check=True, capture_output=True)
        (wt / "forensic.txt").write_text("crash\n", encoding="utf-8")
        raise RuntimeError("mid-wave crash")
    w1 = runner.run_wave([
        {"task_id": "TSK-R1", "agent": "A1", "verifier": "V1", "action": lambda wt: b1.wait() is not None and (wt / "r1.txt").write_text("r1\n", encoding="utf-8") > 0},
        {"task_id": "TSK-R2", "agent": "A2", "verifier": "V2", "action": w1_crash},
        {"task_id": "TSK-R3", "agent": "A3", "verifier": "V3", "action": lambda wt: (b1.wait(), (wt / "forensic3.txt").write_text("false\n", encoding="utf-8")) and False},
    ], max_concurrency=3)
    assert w1["TSK-R1"][0] and not w1["TSK-R2"][0] and "mid-wave crash" in w1["TSK-R2"][1] and not w1["TSK-R3"][0]
    assert (repo / "r1.txt").read_text(encoding="utf-8") == "r1\n" and not (repo / "stale.txt").exists() and not (wt_mgr.worktrees_dir / "TSK-R1").exists()
    assert (wt_mgr.worktrees_dir / "TSK-R2" / "forensic.txt").read_text(encoding="utf-8") == "crash\n" and (wt_mgr.worktrees_dir / "TSK-R3" / "forensic3.txt").read_text(encoding="utf-8") == "false\n"
    refs = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/task/"], cwd=repo, check=True, capture_output=True, text=True).stdout.splitlines()
    assert set(refs) == {"task/TSK-R2", "task/TSK-R3"} and all(db.get_task(t)["status"] == "blocked" for t in ("TSK-R2", "TSK-R3")) and not db.claim_task("TSK-R2", agent="A2R")
    _cleanup_and_reset_tasks(db, wt_mgr, "TSK-R2", "TSK-R3")
    assert _check_clean_git_state(repo, wt_mgr) and main(["--rig-dir", str(db.rig_dir), "task", "claim", "--id", "TSK-R2", "--agent", "A2R"]) == 0
    w2 = runner.run_wave([
        {"task_id": "TSK-R2", "agent": "A2R", "verifier": "V2", "action": lambda wt: b2.wait() is not None and (wt / "r1.txt").exists() and not (wt / "stale.txt").exists() and not (wt / "forensic.txt").exists() and (wt / "r2.txt").write_text("r2\n", encoding="utf-8") > 0},
        {"task_id": "TSK-R3", "agent": "A3R", "verifier": "V3", "action": lambda wt: b2.wait() is not None and (wt / "r1.txt").exists() and not (wt / "forensic3.txt").exists()},
        {"task_id": "TSK-R4", "agent": "A4", "verifier": "V4", "action": lambda wt: b2.wait() is not None and (wt / "r4.txt").write_text("r4\n", encoding="utf-8") > 0},
    ], max_concurrency=3)
    assert all(ok for ok, _ in w2.values()) and all(db.get_task(f"TSK-R{i}")["status"] == "done" for i in (1, 2, 3, 4)), w2
    assert (repo / "r2.txt").exists() and (repo / "r4.txt").exists() and not (repo / "stale.txt").exists() and _check_clean_git_state(repo, wt_mgr) and db.verify_audit_chain()[0]


def test_post_wave_retry_cli_and_telemetry_verification(rig_repo, tmp_path: Path, capsys):
    repo, db, wt_mgr, runner = rig_repo
    db.create_task("EPC-M", "Metrics Epic", author="Arch", archetype="epic")
    for i in range(1, 5): db.create_task(f"TSK-M{i}", f"Metric {i}", parent_id="EPC-M", author="Arch", archetype="unit_tester" if i == 4 else "diff_synthesizer")
    b1, b2 = threading.Barrier(4, timeout=10.0), threading.Barrier(3, timeout=10.0)
    w1_specs = [{"task_id": f"TSK-M{i}", "agent": f"A{i}", "verifier": f"V{i}", "action": lambda wt, k=i: ((wt / f"bad_{k}.txt").write_text("err\n", encoding="utf-8"), b1.wait(), (_ for _ in ()).throw(RuntimeError("schema lock contention")))[2]} for i in (1, 2, 3)]
    w1 = runner.run_wave([*w1_specs, {"task_id": "TSK-M4", "agent": "A4", "verifier": "V4", "action": lambda wt: b1.wait() is not None and (wt / "shared.txt").exists()}], max_concurrency=4)
    assert w1["TSK-M4"][0] and all(not w1[f"TSK-M{i}"][0] and (wt_mgr.worktrees_dir / f"TSK-M{i}" / f"bad_{i}.txt").exists() for i in (1, 2, 3))
    _cleanup_and_reset_tasks(db, wt_mgr, "TSK-M1", "TSK-M2", "TSK-M3")
    w2 = runner.run_wave([{"task_id": f"TSK-M{i}", "agent": f"B{i}", "verifier": f"V{i}", "action": lambda wt, k=i: b2.wait() is not None and not (wt / f"bad_{k}.txt").exists() and (wt / f"m_{k}.txt").write_text(f"ok{k}\n", encoding="utf-8") > 0} for i in (1, 2, 3)], max_concurrency=3)
    assert all(ok for ok, _ in w2.values()) and _check_clean_git_state(repo, wt_mgr)
    m_json, m_md = (exp := TelemetryExporter(db)).export_metrics_json(), exp.generate_metrics_markdown()
    gov, seq_file = m_json["governance_and_topology"], tmp_path / "seq.mmd"
    assert len(m_json["durations"]) == 5 and all((d["total_seconds"] is None) == (d["task_id"] == "EPC-M") and (d["total_seconds"] or 0) >= 0 for d in m_json["durations"])
    assert (gov["total_records"], gov["total_tasks"], gov["completed_tasks"], gov["verify_only_tasks"], gov["rework_tasks"], gov["rework_rate"]) == (5, 4, 4, 1, 3, 0.75)
    assert gov["blocked_reasons"]["by_reason"] == {"schema lock contention": 3} and len(gov["blocked_reasons"]["recurring_candidates"]) == 1
    assert "3 / 4 leaf tasks" in m_md and "### Recurring Blocker Traps" in m_md and "| `EPC-M` | 4 |" in m_md
    assert main(["--rig-dir", str(db.rig_dir), "board", "sync"]) == 0 and "## Completed (4)" in db.board_path.read_text(encoding="utf-8")
    assert main(["--rig-dir", str(db.rig_dir), "telemetry", "sequence", "--output", str(seq_file)]) == 0
    assert "Report blocked TSK-M1 (schema lock contention)" in seq_file.read_text(encoding="utf-8") and "B1->>V1: Submit TSK-M1 for review" in seq_file.read_text(encoding="utf-8")
    capsys.readouterr()
    assert main(["--rig-dir", str(db.rig_dir), "telemetry", "metrics", "--json"]) == 0 and json.loads(capsys.readouterr().out) == m_json
    assert main(["--rig-dir", str(db.rig_dir), "audit", "verify"]) == 0 and "Verified 19 audit entries" in capsys.readouterr().out and db.verify_audit_chain()[0]

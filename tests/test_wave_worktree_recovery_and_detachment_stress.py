"""Stress tests for WaveRunner concurrency clamping, worktree recovery, and swarm-slug fallbacks."""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil, subprocess, threading, pytest
from stigmerig.db import StigmeRigDB
from stigmerig.wave import WaveRunner, WorktreeManager


def parse_worktree_porcelain(raw: str) -> list[dict[str, str]]:
    return [{k: v for ln in chunk.splitlines() for k, _, v in (ln.partition(" "),)} for chunk in raw.strip().split("\n\n") if chunk.strip()]


def parse_commit_identities(raw: str) -> list[tuple[str, ...]]:
    return [tuple(ln.split("|", 4)) for ln in raw.strip().splitlines() if ln.strip()]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _clean_git_state(repo: Path, wt_mgr: WorktreeManager) -> bool:
    blocks = parse_worktree_porcelain(_git(repo, "worktree", "list", "--porcelain"))
    return len(blocks) == 1 and Path(blocks[0]["worktree"]).resolve() == repo.resolve() and "prunable" not in blocks[0] and _git(repo, "branch", "--list", "task/*") == "" and list(wt_mgr.worktrees_dir.iterdir()) == []


@pytest.fixture
def rig_repo(tmp_path: Path) -> tuple[Path, StigmeRigDB, WorktreeManager, WaveRunner]:
    (repo := tmp_path / "repo").mkdir()
    for cmd in (("init", "-b", "main"), ("config", "user.name", "Test"), ("config", "user.email", "t@local"), ("config", "commit.gpgsign", "false")): _git(repo, *cmd)
    (repo / "base.txt").write_text("v1\n", encoding="utf-8")
    for cmd in (("add", "base.txt"), ("commit", "-m", "init")): _git(repo, *cmd)
    db, wt_mgr = StigmeRigDB(rig_dir=repo / ".stigmerig"), WorktreeManager(repo_root=repo)
    db.init_rig(scaffold_icm=False)
    return repo, db, wt_mgr, WaveRunner(db=db, worktree_mgr=wt_mgr)


def test_run_wave_clamps_concurrency_above_five_and_prunes_worktrees(rig_repo) -> None:
    repo, db, wt_mgr, runner = rig_repo
    for idx in range(1, 8): db.create_task(f"TSK-C{idx}", f"Clamp task {idx}", author="Arch")
    lock, b1, b2, state = threading.Lock(), threading.Barrier(5, timeout=30.0), threading.Barrier(2, timeout=30.0), {"active": 0, "hwm": 0, "peaks": [0, 0]}

    def make_act(idx: int):
        def _act(wt: Path) -> bool:
            with lock:
                state["active"] += 1
                state["hwm"], state["peaks"][idx > 5] = max(state["hwm"], state["active"]), max(state["peaks"][idx > 5], state["active"])
            try:
                (b1 if idx <= 5 else b2).wait()
                assert idx <= 5 or all(db.get_task(f"TSK-C{j}")["status"] == "done" and (wt / f"c{j}.txt").exists() for j in range(1, 6))
                return (wt / f"c{idx}.txt").write_text(f"val-{idx}\n", encoding="utf-8") > 0
            finally:
                with lock: state["active"] -= 1
        return _act

    res = runner.run_wave([{"task_id": f"TSK-C{i}", "agent": f"ag/w{i}", "verifier": f"qa/v{i}", "action": make_act(i)} for i in range(1, 8)], max_concurrency=10)
    assert all(ok for ok, _ in res.values()) and state == {"active": 0, "hwm": 5, "peaks": [5, 2]}, (res, state)
    assert all(db.get_task(f"TSK-C{i}")["status"] == "done" and (repo / f"c{i}.txt").exists() for i in range(1, 8))
    assert _clean_git_state(repo, wt_mgr) and db.verify_audit_chain()[0]


def test_failed_worktree_detachment_idempotent_cleanup_and_precommitted_retry(rig_repo) -> None:
    repo, db, wt_mgr, runner = rig_repo
    for tid in ("TSK-DET", "TSK-R1", "TSK-R2"): db.create_task(tid, f"Recovery {tid}", author="Arch")
    _git(repo, "checkout", "--detach", "HEAD")
    assert not runner.run_task_in_worktree("TSK-DET", "ag/wd", lambda p: True, verifier="qa/vd")[0] and db.get_task("TSK-DET")["status"] == "blocked"
    _git(repo, "checkout", "main")
    f_bar, r_bar = threading.Barrier(2, timeout=30.0), threading.Barrier(2, timeout=30.0)
    fail_res = runner.run_wave([
        {"task_id": "TSK-R1", "agent": "ag/w1", "verifier": "qa/v1", "action": lambda wt: ((wt / "dirty1.txt").write_text("x\n", encoding="utf-8"), f_bar.wait()) and False},
        {"task_id": "TSK-R2", "agent": "ag/w2", "verifier": "qa/v2", "action": lambda wt: ((wt / "dirty2.txt").write_text("y\n", encoding="utf-8"), _git(wt, "checkout", "--detach", "HEAD"), f_bar.wait(), (_ for _ in ()).throw(RuntimeError("boom")))[3]},
    ], max_concurrency=2)
    assert not fail_res["TSK-R1"][0] and not fail_res["TSK-R2"][0] and all(db.get_task(t)["status"] == "blocked" for t in ("TSK-R1", "TSK-R2"))
    with pytest.raises(RuntimeError): wt_mgr.create_worktree("TSK-R2")
    shutil.rmtree(wt_mgr.worktrees_dir / "TSK-R1")
    mid = parse_worktree_porcelain(_git(repo, "worktree", "list", "--porcelain"))
    assert any("prunable" in b for b in mid) and any("detached" in b for b in mid)
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda t: wt_mgr.remove_worktree(t, delete_branch=True), ("TSK-R1", "TSK-R1", "TSK-R2", "TSK-R2", "TSK-NONE")))
    wt_pre = wt_mgr.create_worktree("TSK-R1")
    (wt_pre / "r1.txt").write_text("pre1\n", encoding="utf-8")
    for cmd in (("add", "r1.txt"), ("commit", "-m", "precommit r1")): _git(wt_pre, *cmd)
    wt_mgr.remove_worktree("TSK-R1", delete_branch=False)
    with db.get_queue_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'todo', claimed_by = NULL, lease_until = NULL WHERE id IN ('TSK-R1', 'TSK-R2')")
        conn.commit()
    retry_res = runner.run_wave([
        {"task_id": "TSK-R1", "agent": "ag/w1", "verifier": "qa/v1", "action": lambda wt: r_bar.wait() is not None and (wt / "r1.txt").read_text(encoding="utf-8") == "pre1\n" and not (wt / "dirty1.txt").exists()},
        {"task_id": "TSK-R2", "agent": "ag/w2", "verifier": "qa/v2", "action": lambda wt: ((wt / "r2.txt").write_text("pre2\n", encoding="utf-8"), _git(wt, "add", "r2.txt"), _git(wt, "commit", "-m", "precommit r2"), r_bar.wait()) is not None and not (wt / "dirty2.txt").exists()},
    ], max_concurrency=2)
    assert all(ok for ok, _ in retry_res.values()) and (repo / "r1.txt").read_text(encoding="utf-8") == "pre1\n" and (repo / "r2.txt").read_text(encoding="utf-8") == "pre2\n", retry_res
    assert _clean_git_state(repo, wt_mgr) and db.verify_audit_chain()[0]


def test_worktree_none_self_approval_verify_only_and_symbol_swarm_slug(rig_repo) -> None:
    repo, db, _, _ = rig_repo
    head0, wt_mgr = _git(repo, "rev-parse", "HEAD"), WorktreeManager(repo_root=repo, swarm_name="---!!!")
    wt_runner, nowt_runner = WaveRunner(db=db, worktree_mgr=wt_mgr), WaveRunner(db=db, worktree_mgr=None, swarm_name="---!!!")
    verify_archs = (("TSK-V1", "unit_tester"), ("TSK-V2", "prose_auditor"), ("TSK-V3", "topology_reader"))
    slug_archs = (("TSK-S1", "Security Code Reviewer"), ("TSK-S2", "Lead Gate Keeper"))
    nowt_defs = (("TSK-N1", "solo/one", "solo/one", "Policy Reviewer"), ("TSK-N2", "solo/two", None, "Queue Inspector"))
    for tid, arch in (*verify_archs, *slug_archs, *((t, a) for t, _, _, a in nowt_defs)): db.create_task(tid, f"Task {tid}", author="Arch", archetype=arch)
    assert not nowt_runner.run_task_in_worktree("TSK-N1", "solo/one", lambda p: True, verifier="solo/one")[0]
    bar, seen = threading.Barrier(7, timeout=30.0), {}
    wt_specs = [{"task_id": t, "agent": f"wt/{t.lower()}", "verifier": "qa/ver", "archetype": a, "action": lambda p, tid=t: (seen.setdefault(tid, p), bar.wait(), (p / "base.txt").exists())[2]} for t, a in verify_archs] + [
        {"task_id": t, "agent": f"wt/{t.lower()}", "verifier": "qa/ver", "archetype": a, "action": lambda p, tid=t: (seen.setdefault(tid, p), bar.wait(), (p / f"{tid.lower()}.txt").write_text("ok\n", encoding="utf-8")) is not None} for t, a in slug_archs
    ]
    nowt_specs = [{"task_id": t, "agent": ag, "verifier": vr, "archetype": ar, "allow_self_approval": True, "action": lambda p, tid=t: (seen.setdefault(tid, p), bar.wait()) is not None} for t, ag, vr, ar in nowt_defs]
    with ThreadPoolExecutor(max_workers=2) as pool:
        res = {k: v for f in (pool.submit(wt_runner.run_wave, wt_specs, 10), pool.submit(nowt_runner.run_wave, nowt_specs, 10)) for k, v in f.result().items()}
    assert all(ok and db.get_task(t)["status"] == "done" for t, (ok, _) in res.items()), res
    assert all(seen[t] == repo for t, _, _, _ in nowt_defs) and all(seen[t] != repo for t, _ in (*verify_archs, *slug_archs))
    ids = parse_commit_identities(_git(repo, "log", f"{head0}..HEAD", "--format=%an|%ae|%cn|%ce|%s"))
    assert len(ids) == 4 and ("---!!! [Security Code Reviewer]", "swarm+security_code_reviewer@stigmerig.local", "---!!! [orchestrator]", "swarm+orchestrator@stigmerig.local", "feat(TSK-S1): Task TSK-S1") in ids
    assert ("---!!! [Lead Gate Keeper]", "swarm+lead_gate_keeper@stigmerig.local", "---!!! [orchestrator]", "swarm+orchestrator@stigmerig.local", "feat(TSK-S2): Task TSK-S2") in ids
    assert sum(1 for r in ids if r[:4] == ("---!!! [orchestrator]", "swarm+orchestrator@stigmerig.local", "---!!! [orchestrator]", "swarm+orchestrator@stigmerig.local")) == 2
    assert _clean_git_state(repo, wt_mgr) and db.verify_audit_chain() == (True, "Verified 21 audit entries with intact hash chain")

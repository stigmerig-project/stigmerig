"""Hermetic tests for worktree lease recovery, git lock synchronization, and wave bounds."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
import pytest
import stigmerig.wave as wave_mod
from stigmerig.db import StigmeRigDB
from stigmerig.wave import WaveRunner, WorktreeManager


@pytest.fixture
def rig_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.name", "Test"], ["git", "config", "user.email", "test@local"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "base.txt").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--no-gpg-sign", "-m", "init"], cwd=repo, check=True, capture_output=True)
    db = StigmeRigDB(rig_dir=repo / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    wt_mgr = WorktreeManager(repo_root=repo)
    return repo, db, wt_mgr, WaveRunner(db=db, worktree_mgr=wt_mgr)


def test_worktree_setup_failure_recovers_claimed_lease(rig_repo, tmp_path: Path, monkeypatch):
    repo, db, wt_mgr, runner = rig_repo
    db.create_task(task_id="TSK-WT-1", title="Git worktree failure", author="Arch")
    subprocess.run(["git", "worktree", "add", "-b", "task/TSK-WT-1", str(tmp_path / "occ")], cwd=repo, check=True, capture_output=True)
    with pytest.raises(subprocess.CalledProcessError) as cpe:
        wt_mgr.create_worktree("TSK-WT-1")
    assert "task/TSK-WT-1" in str(cpe.value) and "already" in str(cpe.value)
    called = []
    ok, msg = runner.run_task_in_worktree("TSK-WT-1", "AgentA", lambda p: called.append(p) or True, verifier="AgentB")
    t1 = db.get_task("TSK-WT-1")
    assert ok is False and "Task error:" in msg and "task/TSK-WT-1" in msg and not called and t1["status"] == "blocked" and t1["lease_until"] is None

    db.create_task(task_id="TSK-WT-2", title="OS worktree failure", author="Arch")
    monkeypatch.setattr(wt_mgr, "create_worktree", lambda _tid: (_ for _ in ()).throw(OSError("worktree disk full")))
    res = runner.run_wave([{"task_id": "TSK-WT-2", "agent": "AgentA", "verifier": "AgentB", "action": lambda p: True}], max_concurrency=1)
    t2 = db.get_task("TSK-WT-2")
    assert res["TSK-WT-2"][0] is False and "worktree disk full" in res["TSK-WT-2"][1]
    assert t2["status"] == "blocked" and t2["lease_until"] is None and db.verify_audit_chain()[0]


def test_git_staging_and_commit_synchronized_while_actions_concurrent(rig_repo, monkeypatch):
    _, db, wt_mgr, runner = rig_repo
    inner_lock, tls, real_run, git_states = wt_mgr._lock, threading.local(), subprocess.run, []

    class _TrackedLock:
        def __enter__(self):
            inner_lock.acquire()
            tls.depth = getattr(tls, "depth", 0) + 1
            return self

        def __exit__(self, *exc):
            tls.depth -= 1
            inner_lock.release()

    wt_mgr._lock = _TrackedLock()
    monkeypatch.setattr(wave_mod.subprocess, "run", lambda cmd, *a, **kw: (git_states.append((cmd[1], getattr(tls, "depth", 0) > 0)) if cmd[:1] == ["git"] else None, real_run(cmd, *a, **kw))[1])
    barrier = threading.Barrier(2, timeout=5.0)
    for i in (1, 2):
        db.create_task(task_id=f"TSK-S{i}", title=f"Sync {i}", author="Arch")

    def make_act(idx: int):
        def _act(wt_dir: Path) -> bool:
            assert getattr(tls, "depth", 0) == 0
            barrier.wait()
            (wt_dir / f"sync_{idx}.txt").write_text(f"data {idx}\n", encoding="utf-8")
            return True
        return _act

    res = runner.run_wave([{"task_id": f"TSK-S{i}", "agent": f"A{i}", "verifier": f"V{i}", "action": make_act(i)} for i in (1, 2)], max_concurrency=2)
    assert all(ok for ok, _ in res.values()) and {"add", "diff", "commit", "merge"}.issubset({s for s, _ in git_states}) and all(h for _, h in git_states)


def test_run_wave_validates_max_concurrency(tmp_path: Path):
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    db.create_task(task_id="TSK-MC", title="Concurrency check", author="Arch")
    runner = WaveRunner(db=db)
    spec = [{"task_id": "TSK-MC", "agent": "A1", "verifier": "V1", "action": lambda p: True}]
    for bad in (True, False, 0, -1, -5):
        with pytest.raises(ValueError, match="max_concurrency"):
            runner.run_wave(spec, max_concurrency=bad)
    assert db.get_task("TSK-MC")["status"] == "todo"


def test_locked_readonly_remove_and_prunable_worktree_recovery(rig_repo, monkeypatch):
    repo, _, wt_mgr, _ = rig_repo
    wt = wt_mgr.create_worktree("TSK-LCK")
    subprocess.run(["git", "worktree", "lock", str(wt)], cwd=repo, check=True, capture_output=True)
    ro_dir = wt / "ro_sub"
    ro_dir.mkdir()
    ro_file = ro_dir / "ro.txt"
    ro_file.write_text("locked-ro\n", encoding="utf-8")
    os.chmod(ro_file, stat.S_IREAD)
    os.chmod(ro_dir, 0o555 if os.name != "nt" else stat.S_IREAD)
    cmds, rmtree_calls, real_run, real_rm = [], [], subprocess.run, shutil.rmtree
    monkeypatch.setattr(wave_mod.subprocess, "run", lambda c, *a, **kw: (cmds.append(list(c)), real_run(c, *a, **kw))[1])
    monkeypatch.setattr(wave_mod.shutil, "rmtree", lambda p, *a, **kw: (rmtree_calls.append(callable(kw.get("onerror"))), real_rm(p, *a, **kw))[1])
    wt_mgr.remove_worktree("TSK-LCK", delete_branch=True)
    porcelain = real_run(["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True).stdout
    branches = real_run(["git", "branch", "--list", "task/TSK-LCK"], cwd=repo, check=True, capture_output=True, text=True).stdout
    assert ["git", "worktree", "remove", "--force", "--force", str(wt)] in cmds and rmtree_calls == [True]
    assert not wt.exists() and "TSK-LCK" not in porcelain and branches.strip() == ""

    wt_prn = wt_mgr.create_worktree("TSK-PRN")
    (wt_prn / "kept.txt").write_text("saved\n", encoding="utf-8")
    real_run(["git", "add", "kept.txt"], cwd=wt_prn, check=True, capture_output=True)
    real_run(["git", "commit", "--no-gpg-sign", "-m", "pre"], cwd=wt_prn, check=True, capture_output=True)
    real_rm(wt_prn)
    assert "prunable" in real_run(["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True).stdout
    wt_rec = wt_mgr.create_worktree("TSK-PRN")
    assert wt_rec == wt_prn and (wt_rec / "kept.txt").read_text(encoding="utf-8") == "saved\n"
    assert "prunable" not in real_run(["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True).stdout
    wt_mgr.remove_worktree("TSK-PRN", delete_branch=True)


def test_gpgsign_safe_commits_merges_and_cpe_stderr_stdout_details(rig_repo, monkeypatch):
    repo, db, _, runner = rig_repo
    for k, v in (("commit.gpgsign", "true"), ("tag.gpgsign", "true"), ("gpg.program", "nonexistent-gpg-bin")):
        subprocess.run(["git", "config", k, v], cwd=repo, check=True, capture_output=True)
    assert subprocess.run(["git", "commit", "--allow-empty", "-m", "unsigned"], cwd=repo, capture_output=True).returncode != 0
    for tid in ("TSK-GPG", "TSK-CPE-ERR", "TSK-CPE-OUT"):
        db.create_task(task_id=tid, title=f"Task {tid}", author="Arch")
    cmds, real_run = [], subprocess.run
    monkeypatch.setattr(wave_mod.subprocess, "run", lambda c, *a, **kw: (cmds.append(list(c)), real_run(c, *a, **kw))[1])
    ok_g, msg_g = runner.run_task_in_worktree("TSK-GPG", "AgentA", lambda wt: (wt / "gpg.txt").write_text("ok\n", encoding="utf-8") > 0, verifier="AgentB")
    assert ok_g is True and (repo / "gpg.txt").read_text(encoding="utf-8") == "ok\n", msg_g
    assert any(c[:2] == ["git", "commit"] and "--no-gpg-sign" in c for c in cmds)
    assert any(c[:2] == ["git", "merge"] and "--no-gpg-sign" in c for c in cmds)

    no_wt = WaveRunner(db=db)
    ok_e, msg_e = no_wt.run_task_in_worktree("TSK-CPE-ERR", "AgentA", lambda _p: (_ for _ in ()).throw(subprocess.CalledProcessError(128, ["git", "commit"], output="out\n", stderr="  fatal: stderr detail  \n")), verifier="AgentB")
    ok_o, msg_o = no_wt.run_task_in_worktree("TSK-CPE-OUT", "AgentA", lambda _p: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["git", "commit"], output="  hook stdout detail  \n", stderr="   \n")), verifier="AgentB")
    assert not ok_e and msg_e.endswith("fatal: stderr detail") and not ok_o and msg_o.endswith("hook stdout detail")
    assert db.verify_audit_chain()[0]

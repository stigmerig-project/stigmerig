"""Hermetic tests for worktree git-root isolation, branch ref validation, and pre-merge guards."""

from __future__ import annotations
import os
from pathlib import Path
import subprocess
import pytest
import stigmerig.wave as wave_mod
from stigmerig.db import StigmeRigDB
from stigmerig.wave import WaveRunner, WorktreeManager


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _make_dir_link(src: Path, dst: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))
    else:
        dst.symlink_to(src, target_is_directory=True)


@pytest.fixture
def rig_repo(tmp_path: Path) -> tuple[Path, StigmeRigDB, WorktreeManager, WaveRunner]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (("init", "-b", "main"), ("config", "user.name", "Test"), ("config", "user.email", "t@local"), ("config", "commit.gpgsign", "false")):
        _git(repo, *cmd)
    (repo / "base.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "init")
    db = StigmeRigDB(rig_dir=repo / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    wt_mgr = WorktreeManager(repo_root=repo)
    return repo, db, wt_mgr, WaveRunner(db=db, worktree_mgr=wt_mgr)


def test_worktree_reparse_and_plain_dir_isolation(rig_repo, tmp_path: Path, monkeypatch) -> None:
    repo, db, wt_mgr, runner = rig_repo
    ext = tmp_path / "ext"
    ext.mkdir()
    junc_root = tmp_path / "junc_wt"
    _make_dir_link(ext, junc_root)
    with pytest.raises(ValueError):
        WorktreeManager(repo_root=repo, worktrees_dir=junc_root)._resolve_worktree_target("TSK-1")
    wt_mgr.worktrees_dir.mkdir(parents=True, exist_ok=True)
    _make_dir_link(ext, wt_mgr.worktrees_dir / "TSK-JUNC")
    with pytest.raises(ValueError):
        wt_mgr.create_worktree("TSK-JUNC")
    real_is_sym = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name in {"SYM_WT", "TSK-SYM"} or real_is_sym(self))
    with pytest.raises(ValueError):
        WorktreeManager(repo_root=repo, worktrees_dir=tmp_path / "SYM_WT")._resolve_worktree_target("TSK-1")
    with pytest.raises(ValueError):
        wt_mgr._resolve_worktree_target("TSK-SYM")
    monkeypatch.undo()

    (wt_mgr.worktrees_dir / "TSK-PLAIN").mkdir()
    (wt_mgr.worktrees_dir / "TSK-FILE").write_text("x\n", encoding="utf-8")
    (repo / "canary.txt").write_text("untracked_main\n", encoding="utf-8")
    head0 = _git(repo, "rev-parse", "HEAD")
    for bad_id in ("TSK-PLAIN", "TSK-FILE"):
        with pytest.raises(RuntimeError):
            wt_mgr.create_worktree(bad_id)
    db.create_task("TSK-PLAIN", "Plain dir trap", author="Arch")
    ok, _ = runner.run_task_in_worktree("TSK-PLAIN", "A1", lambda p: True, verifier="V1")
    assert not ok and db.get_task("TSK-PLAIN")["status"] == "blocked" and _git(repo, "rev-parse", "HEAD") == head0 and "canary.txt" not in _git(repo, "ls-files")
    (repo / "canary.txt").unlink()
    wt_ok = wt_mgr.create_worktree("TSK-OK")
    assert wt_mgr.create_worktree("TSK-OK") == wt_ok


def test_merge_worktree_branch_validation_and_conditional_abort(rig_repo, monkeypatch) -> None:
    repo, _, wt_mgr, _ = rig_repo
    wt = wt_mgr.create_worktree("TSK-MRG")
    (wt / "mrg.txt").write_text("branch_data\n", encoding="utf-8")
    _git(wt, "add", "mrg.txt")
    _git(wt, "commit", "-m", "mrg")
    for bad_target in ("HEAD", "head", "", "   ", "-b", "--orphan", " -f", "missing-branch"):
        assert wt_mgr.merge_worktree("TSK-MRG", target_branch=bad_target)[0] is False
    assert wt_mgr.merge_worktree("TSK-NOBRANCH", target_branch="main") == (False, "Task branch task/TSK-NOBRANCH does not exist")
    _git(repo, "checkout", "--detach", "HEAD")
    assert wt_mgr.merge_worktree("TSK-MRG")[0] is False
    _git(repo, "checkout", "main")

    cmds, real_run = [], subprocess.run
    monkeypatch.setattr(wave_mod.subprocess, "run", lambda cmd, *a, **kw: (cmds.append(list(cmd)), real_run(cmd, *a, **kw))[1])
    (repo / "mrg.txt").write_text("untracked_collision\n", encoding="utf-8")
    assert wt_mgr.merge_worktree("TSK-MRG", target_branch="main")[0] is False and ["git", "merge", "--abort"] not in cmds
    _git(repo, "add", "mrg.txt")
    _git(repo, "commit", "-m", "main conflict")
    cmds.clear()
    ok, msg = wt_mgr.merge_worktree("TSK-MRG", target_branch="main")
    assert not ok and "Merge conflict" in msg and ["git", "merge", "--abort"] in cmds and not (repo / ".git" / "MERGE_HEAD").exists()


def test_waverunner_open_child_and_premerge_lease_guards(rig_repo) -> None:
    repo, db, wt_mgr, runner = rig_repo
    db.create_task("EPIC-1", "Parent", author="Arch")
    db.create_task("TSK-C1", "Child", parent_id="EPIC-1", author="Arch")
    ok_p, msg_p = runner.run_task_in_worktree("EPIC-1", "A1", lambda p: (_ for _ in ()).throw(AssertionError("ran")), verifier="V1")
    assert not ok_p and "open child tasks remain" in msg_p and db.get_task("EPIC-1")["status"] == "todo" and not (wt_mgr.worktrees_dir / "EPIC-1").exists()
    assert runner.run_task_in_worktree("TSK-C1", "A1", lambda p: True, verifier="V1")[0] is True
    assert runner.run_task_in_worktree("EPIC-1", "A1", lambda p: True, verifier="V1")[0] is True

    for tid in ("TSK-EXP", "TSK-TAKE"):
        db.create_task(tid, f"Task {tid}", author="Arch")
    head0 = _git(repo, "rev-parse", "HEAD")

    def expire_act(wt: Path) -> bool:
        (wt / "exp.txt").write_text("stale\n", encoding="utf-8")
        with db.get_queue_conn() as conn:
            conn.execute("UPDATE tasks SET lease_until = '2000-01-01T00:00:00Z' WHERE id = 'TSK-EXP'")
            conn.commit()
        return True

    def takeover_act(wt: Path) -> bool:
        (wt / "take.txt").write_text("hijacked\n", encoding="utf-8")
        _git(wt, "add", "take.txt")
        _git(wt, "commit", "-m", "stale precommit")
        with db.get_queue_conn() as conn:
            conn.execute("UPDATE tasks SET lease_until = '2000-01-01T00:00:00Z' WHERE id = 'TSK-TAKE'")
            conn.commit()
        return db.claim_task("TSK-TAKE", agent="AgentB")

    ok_e, msg_e = runner.run_task_in_worktree("TSK-EXP", "AgentA", expire_act, verifier="V1")
    ok_t, msg_t = runner.run_task_in_worktree("TSK-TAKE", "AgentA", takeover_act, verifier="V1")
    assert not ok_e and "lease expired" in msg_e and db.get_task("TSK-EXP")["status"] == "working"
    assert not ok_t and "active lease not held" in msg_t and (db.get_task("TSK-TAKE")["status"], db.get_task("TSK-TAKE")["claimed_by"]) == ("working", "AgentB")
    assert _git(repo, "rev-parse", "HEAD") == head0 and not (repo / "exp.txt").exists() and not (repo / "take.txt").exists() and db.verify_audit_chain()[0]


def test_merge_worktree_preexisting_repo_and_worktree_op_guards(rig_repo, monkeypatch) -> None:
    repo, _, wt_mgr, _ = rig_repo
    wt = wt_mgr.create_worktree("TSK-GUARD")
    (wt / "guard.txt").write_text("clean_work\n", encoding="utf-8")
    _git(wt, "add", "guard.txt")
    _git(wt, "commit", "-m", "guard work")

    _git(repo, "checkout", "-b", "user-wip")
    (repo / "base.txt").write_text("user_side\n", encoding="utf-8")
    _git(repo, "commit", "-am", "user wip")
    _git(repo, "checkout", "main")
    (repo / "base.txt").write_text("main_side\n", encoding="utf-8")
    _git(repo, "commit", "-am", "main wip")
    assert subprocess.run(["git", "merge", "user-wip"], cwd=repo, capture_output=True, text=True).returncode != 0

    cmds, real_run = [], subprocess.run
    monkeypatch.setattr(wave_mod.subprocess, "run", lambda cmd, *a, **kw: (cmds.append(list(cmd)), real_run(cmd, *a, **kw))[1])
    for tgt in ("main", None):
        ok, msg = wt_mgr.merge_worktree("TSK-GUARD", target_branch=tgt)
        assert not ok and "MERGE_HEAD" in msg and ["git", "merge", "--abort"] not in cmds and (wave_mod._git_dir(repo) / "MERGE_HEAD").exists() and wt.exists()
    real_run(["git", "merge", "--abort"], cwd=repo, check=True, capture_output=True)

    for gdir in (wave_mod._git_dir(repo), wave_mod._git_dir(wt)):
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
            cmds.clear()
            target_path = gdir / marker
            target_path.mkdir() if marker.startswith("rebase-") else target_path.write_text("deadbeef\n", encoding="utf-8")
            ok, msg = wt_mgr.merge_worktree("TSK-GUARD", target_branch="main")
            assert not ok and marker in msg and ["git", "merge", "--abort"] not in cmds and target_path.exists() and wt.exists()
            target_path.rmdir() if marker.startswith("rebase-") else target_path.unlink()

    _git(wt, "checkout", "--detach", "HEAD")
    assert wt_mgr.merge_worktree("TSK-GUARD", target_branch="main")[0] is False and wt.exists()
    _git(wt, "checkout", "-b", "rogue-branch")
    assert wt_mgr.merge_worktree("TSK-GUARD", target_branch="main")[0] is False and wt.exists()
    _git(wt, "checkout", "task/TSK-GUARD")
    assert wt_mgr.merge_worktree("TSK-GUARD", target_branch="main")[0] is True and (repo / "guard.txt").exists()


def test_waverunner_branch_drift_detached_head_and_worktree_op_recovery(rig_repo) -> None:
    repo, db, wt_mgr, runner = rig_repo
    _git(repo, "checkout", "-b", "cp-src")
    (repo / "base.txt").write_text("cp_conflict\n", encoding="utf-8")
    _git(repo, "commit", "-am", "cp src")
    _git(repo, "checkout", "main")
    (repo / "base.txt").write_text("main_conflict\n", encoding="utf-8")
    _git(repo, "commit", "-am", "main conflict")
    head0 = _git(repo, "rev-parse", "HEAD")
    saved: dict[str, str] = {}

    def det_commit(wt: Path) -> bool:
        _git(wt, "checkout", "--detach", "HEAD")
        (wt / "det.txt").write_text("saved\n", encoding="utf-8")
        _git(wt, "add", "det.txt")
        _git(wt, "commit", "-m", "detached work")
        saved["det_sha"] = _git(wt, "rev-parse", "HEAD")
        return True

    def det_dirty(wt: Path) -> bool:
        _git(wt, "checkout", "--detach", "HEAD")
        (wt / "dirty.txt").write_text("unstaged\n", encoding="utf-8")
        return True

    def switch_branch(wt: Path) -> bool:
        _git(wt, "checkout", "-b", "side/drift")
        (wt / "side.txt").write_text("branched\n", encoding="utf-8")
        _git(wt, "add", "side.txt")
        _git(wt, "commit", "-m", "side commit")
        (wt / "side_mod.txt").write_text("uncommitted\n", encoding="utf-8")
        return True

    def wt_cherry_pick(wt: Path) -> bool:
        assert subprocess.run(["git", "cherry-pick", "cp-src"], cwd=wt, capture_output=True, text=True).returncode != 0
        return True

    for tid, fn in (("TSK-DET-CMT", det_commit), ("TSK-DET-MOD", det_dirty), ("TSK-SW-BR", switch_branch), ("TSK-WT-CP", wt_cherry_pick)):
        db.create_task(tid, f"Task {tid}", author="Arch")
        ok, msg = runner.run_task_in_worktree(tid, "AgentA", fn, verifier="V1")
        assert not ok and db.get_task(tid)["status"] == "blocked" and (wt_mgr.worktrees_dir / tid).exists()
        if tid == "TSK-WT-CP":
            assert "CHERRY_PICK_HEAD" in msg and (wave_mod._git_dir(wt_mgr.worktrees_dir / tid) / "CHERRY_PICK_HEAD").exists()

    wt_det, wt_mod, wt_sw = (wt_mgr.worktrees_dir / t for t in ("TSK-DET-CMT", "TSK-DET-MOD", "TSK-SW-BR"))
    assert _git(repo, "rev-parse", "HEAD") == head0 and _git(wt_det, "rev-parse", "HEAD") == saved["det_sha"] and (wt_det / "det.txt").read_text(encoding="utf-8") == "saved\n"
    assert (wt_mod / "dirty.txt").read_text(encoding="utf-8") == "unstaged\n" and _git(wt_mod, "diff", "--cached", "--name-only") == ""
    assert _git(wt_sw, "rev-parse", "--abbrev-ref", "HEAD") == "side/drift" and (wt_sw / "side.txt").exists() and (wt_sw / "side_mod.txt").exists()
    _git(wt_det, "checkout", "-B", "task/TSK-DET-CMT", saved["det_sha"])
    assert wt_mgr.merge_worktree("TSK-DET-CMT", target_branch="main")[0] is True and (repo / "det.txt").read_text(encoding="utf-8") == "saved\n" and db.verify_audit_chain()[0]

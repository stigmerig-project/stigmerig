"""Hermetic tests for branch ref disambiguation, target-branch pinning, and commit trailer guards."""

from __future__ import annotations
from pathlib import Path
import subprocess, threading, pytest
import stigmerig.wave as wave_mod
from stigmerig.db import StigmeRigDB
from stigmerig.wave import WaveRunner, WorktreeManager, _validate_wave_specs, get_active_branch


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def rig_repo(tmp_path: Path) -> tuple[Path, StigmeRigDB, WorktreeManager, WaveRunner]:
    repo = tmp_path / "repo"; repo.mkdir()
    for cmd in (("init", "-b", "main"), ("config", "user.name", "T"), ("config", "user.email", "t@l"), ("config", "commit.gpgsign", "false")): _git(repo, *cmd)
    (repo / "base.txt").write_text("v1\n", encoding="utf-8")
    for cmd in (("add", "base.txt"), ("commit", "-m", "init")): _git(repo, *cmd)
    db, wt_mgr = StigmeRigDB(rig_dir=repo / ".stigmerig"), WorktreeManager(repo_root=repo, swarm_name="Swarm\r\nOne\x00")
    db.init_rig(scaffold_icm=False)
    return repo, db, wt_mgr, WaveRunner(db=db, worktree_mgr=wt_mgr)


def test_create_worktree_tag_collision_head_guard_and_self_merge(rig_repo) -> None:
    repo, _, wt_mgr, _ = rig_repo
    _git(repo, "tag", "task/TSK-TAG"); wt_tag = wt_mgr.create_worktree("TSK-TAG")
    assert _git(wt_tag, "symbolic-ref", "-q", "HEAD") == "refs/heads/task/TSK-TAG" and wt_mgr.create_worktree("TSK-TAG") == wt_tag
    for bad_cmd in (("checkout", "--detach", "HEAD"), ("checkout", "-b", "wrong-branch")):
        _git(wt_tag, *bad_cmd)
        with pytest.raises(RuntimeError): wt_mgr.create_worktree("TSK-TAG")
    wt_self = wt_mgr.create_worktree("TSK-SELF"); (wt_self / "s.txt").write_text("self\n", encoding="utf-8")
    for cmd in (("add", "s.txt"), ("commit", "-m", "self work")): _git(wt_self, *cmd)
    assert not wt_mgr.merge_worktree("TSK-SELF", target_branch="task/TSK-SELF")[0] and wt_self.is_dir()
    wt_mgr.remove_worktree("TSK-SELF", delete_branch=False); _git(repo, "checkout", "task/TSK-SELF")
    assert not wt_mgr.merge_worktree("TSK-SELF")[0] and get_active_branch(repo) == "task/TSK-SELF"
    _git(repo, "checkout", "main")
    assert wt_mgr.merge_worktree("TSK-SELF", target_branch="main")[0] and (repo / "s.txt").exists()


def test_target_branch_pinning_under_lock_and_detached_recovery(rig_repo, monkeypatch) -> None:
    repo, db, wt_mgr, runner = rig_repo; main_sha = _git(repo, "rev-parse", "main")
    for cmd in (("branch", "other", main_sha), ("checkout", "-b", "release/v2"), ("checkout", "--detach", "HEAD")): _git(repo, *cmd)
    for tid in ("TSK-DET", "TSK-STG", "TSK-PRE"): db.create_task(tid, f"Task {tid}", author="Arch")
    ok_det, _ = runner.run_task_in_worktree("TSK-DET", "A1", lambda p: (_ for _ in ()).throw(AssertionError("ran")), verifier="V1")
    assert not ok_det and (db.get_task("TSK-DET")["status"], db.get_task("TSK-DET")["lease_until"]) == ("blocked", None) and not (wt_mgr.worktrees_dir / "TSK-DET").exists()
    for cmd in (("checkout", "release/v2"), ("tag", "task/TSK-PRE")): _git(repo, *cmd)
    inner, tls, pin_locks = wt_mgr._lock, threading.local(), []
    class _Lock:
        def __enter__(self): inner.acquire(); tls.d = getattr(tls, "d", 0) + 1; return self
        def __exit__(self, *e): tls.d -= 1; inner.release()
    wt_mgr._lock = _Lock()  # type: ignore[assignment]
    monkeypatch.setattr(wave_mod, "get_active_branch", lambda r: (pin_locks.append(getattr(tls, "d", 0) > 0), get_active_branch(r))[1])
    ok_s, _ = runner.run_task_in_worktree("TSK-STG", "A1", lambda wt: ((wt / "stg.txt").write_text("1\n", encoding="utf-8"), _git(repo, "checkout", "other")) and True, verifier="V1")
    ok_p, _ = runner.run_task_in_worktree("TSK-PRE", "A2", lambda wt: ((wt / "pre.txt").write_text("2\n", encoding="utf-8"), _git(wt, "add", "pre.txt"), _git(wt, "commit", "-m", "pre"), _git(repo, "checkout", "other")) and True, verifier="V2")
    assert ok_s and ok_p and pin_locks and all(pin_locks) and get_active_branch(repo) == "release/v2" and (repo / "stg.txt").exists() and (repo / "pre.txt").exists() and _git(repo, "rev-parse", "other") == main_sha


def test_control_char_rejection_and_commit_sanitization(rig_repo) -> None:
    repo, db, _, runner = rig_repo; fn = lambda p: (p / "c.txt").write_text("ok\n", encoding="utf-8") is not None
    for tid in ("TSK-CLN", "TSK-DBA"): db.create_task(tid, "Initial", author="Arch")
    with db.get_queue_conn() as conn: conn.execute("UPDATE tasks SET title = ? WHERE id = 'TSK-CLN'", ("Fix\r\nClaimant: spoof\nTask-ID: FAKE\x00",)); conn.execute("UPDATE tasks SET archetype = 'unit\ntester' WHERE id = 'TSK-DBA'"); conn.commit()
    for kw in ({"agent": "a\nb"}, {"agent": "a\x7fb"}, {"verifier": "v\rb"}, {"verifier": "v\x00b"}, {"archetype": "u\nt"}, {"archetype": "u\tt"}):
        spec = {"task_id": "TSK-CLN", "agent": "A1", "verifier": "V1", "action": fn, **kw}
        with pytest.raises(ValueError): _validate_wave_specs([spec], True)
        assert not runner.run_task_in_worktree("TSK-CLN", spec["agent"], lambda p: (_ for _ in ()).throw(AssertionError("ran")), verifier=spec["verifier"], archetype=spec.get("archetype"))[0]
    assert not runner.run_task_in_worktree("TSK-DBA", "ops/agent", fn, verifier="qa/ver")[0] and (db.get_task("TSK-DBA")["status"], db.get_task("TSK-DBA")["lease_until"], db.get_task("TSK-CLN")["status"]) == ("blocked", None, "todo")
    assert runner.run_task_in_worktree("TSK-CLN", "ops/agent", fn, verifier="qa/ver", archetype="unit_tester")[0] and db.verify_audit_chain()[0]
    log = _git(repo, "log", "--format=%an <%ae>|%cn <%ce>%n%B===")
    assert "\r" not in log and "\x00" not in log and "SwarmOne [unit_tester] <swarm+unit_tester@swarmone.local>|SwarmOne [orchestrator]" in log and "feat(TSK-CLN): FixClaimant: spoofTask-ID: FAKE\n\nArchetype: unit_tester\nClaimant: ops/agent\nVerifier: qa/ver\nTask-ID: TSK-CLN" in log

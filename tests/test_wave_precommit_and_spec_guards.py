"""Hermetic tests for pre-committed worktree merges, .worktrees exclusions, and wave spec guards."""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
import pytest
import stigmerig.wave as wave_mod
from stigmerig.db import StigmeRigDB
from stigmerig.wave import WaveRunner, WorktreeManager, _invalid_meta, _validate_wave_specs


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def rig_repo(tmp_path: Path) -> tuple[Path, StigmeRigDB, WorktreeManager, WaveRunner]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (("init", "-b", "main"), ("config", "user.name", "Test"), ("config", "user.email", "t@local"), ("config", "commit.gpgsign", "false")):
        _git(repo, *cmd)
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "init")
    db = StigmeRigDB(rig_dir=repo / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    wt_mgr = WorktreeManager(repo_root=repo)
    return repo, db, wt_mgr, WaveRunner(db=db, worktree_mgr=wt_mgr)


def test_precommitted_branch_merge_verify_prune_and_conflict(rig_repo) -> None:
    repo, db, wt_mgr, runner = rig_repo
    for tid in ("tsk-pre1", "TSK-VER", "TSK-CONF"):
        db.create_task(task_id=tid, title=f"Task {tid}", author="Arch")

    def precommit_ok(wt: Path) -> bool:
        (wt / ".worktrees" / "sub").mkdir(parents=True)
        (wt / ".worktrees" / "sub" / "ignored.txt").write_text("nested\n", encoding="utf-8")
        (wt / "shared.txt").write_text("from_precommit\n", encoding="utf-8")
        (wt / "pre.txt").write_text("committed_by_agent\n", encoding="utf-8")
        _git(wt, "add", "shared.txt", "pre.txt")
        _git(wt, "commit", "-m", "agent direct commit")
        return True

    ok1, _ = runner.run_task_in_worktree("tsk-pre1", "A1", precommit_ok, verifier="V1")
    assert ok1 and (repo / "pre.txt").read_text(encoding="utf-8") == "committed_by_agent\n"
    assert "agent direct commit" in _git(repo, "log", "--oneline") and ".worktrees/sub/ignored.txt" not in _git(repo, "ls-files")
    head_after = _git(repo, "rev-parse", "HEAD")
    ok2, _ = runner.run_task_in_worktree("TSK-VER", "A2", lambda wt: ((wt / ".worktrees").mkdir(exist_ok=True), (wt / ".worktrees" / "x").write_text("x", encoding="utf-8")) and True, verifier="V2")
    assert ok2 and _git(repo, "rev-parse", "HEAD") == head_after and _git(repo, "branch", "--list", "task/*") == ""
    assert not (wt_mgr.worktrees_dir / "TSK-PRE1").exists() and not (wt_mgr.worktrees_dir / "TSK-VER").exists()

    _git(repo, "branch", "task/TSK-CONF", "HEAD~1")
    ok3, msg3 = runner.run_task_in_worktree("TSK-CONF", "A3", lambda wt: ((wt / "shared.txt").write_text("conflicting\n", encoding="utf-8"), _git(wt, "add", "shared.txt"), _git(wt, "commit", "-m", "conflict")) and True, verifier="V3")
    t_conf = db.get_task("TSK-CONF")
    assert not ok3 and "Merge conflict" in msg3 and t_conf["status"] == "blocked" and t_conf["lease_until"] is None
    assert not (repo / ".git" / "MERGE_HEAD").exists() and (repo / "shared.txt").read_text(encoding="utf-8") == "from_precommit\n" and db.verify_audit_chain()[0]


def test_merge_worktree_excludes_worktrees_and_run_wave_guards(rig_repo, monkeypatch) -> None:
    repo, db, wt_mgr, runner = rig_repo
    wt = wt_mgr.create_worktree("TSK-MWT")
    (wt / "clean_commit.txt").write_text("ok\n", encoding="utf-8")
    _git(wt, "add", "clean_commit.txt")
    _git(wt, "commit", "-m", "mwt commit")
    (wt / ".worktrees" / "nested").mkdir(parents=True)
    (wt / ".worktrees" / "nested" / "tmp.txt").write_text("ignore\n", encoding="utf-8")
    (wt / "dirty.txt").write_text("block\n", encoding="utf-8")
    assert wt_mgr.merge_worktree("TSK-MWT") == (False, f"Worktree at {wt} has uncommitted changes")
    (wt / "dirty.txt").unlink()
    assert wt_mgr.merge_worktree("TSK-MWT")[0] is True and (repo / "clean_commit.txt").exists()

    for tid in ("TSK-1", "tsk-1"):
        db.create_task(task_id=tid, title=f"T {tid}", author="Arch")
    spawned, calls = [], []
    real_pool = wave_mod.ThreadPoolExecutor
    monkeypatch.setattr(wave_mod, "ThreadPoolExecutor", lambda *a, **kw: spawned.append(1) or real_pool(*a, **kw))
    fn = lambda p: calls.append(p) or True
    valid = {"task_id": "TSK-1", "agent": "A1", "verifier": "V1", "action": fn}
    for bad in (None, "bad", {"task_id": "TSK-1"}, [None], ["TSK-1"], [{"agent": "A1", "action": fn}], [{"task_id": "", "agent": "A1", "action": fn}], [{"task_id": "   ", "agent": "A1", "action": fn}], [{"task_id": 123, "agent": "A1", "action": fn}], [{"task_id": "TSK-1", "action": fn}], [{"task_id": "TSK-1", "agent": "  ", "action": fn}], [{"task_id": "TSK-1", "agent": "A1"}], [{"task_id": "TSK-1", "agent": "A1", "action": "x"}], [valid, dict(valid)], [valid, {"task_id": "tsk-1", "agent": "A2", "verifier": "V2", "action": fn}]):
        with pytest.raises(ValueError):
            runner.run_wave(bad, max_concurrency=1)  # type: ignore[arg-type]
    assert spawned == [] and calls == [] and all(db.get_task(t)["status"] == "todo" for t in ("TSK-1", "tsk-1"))
    no_wt = WaveRunner(db=db, worktree_mgr=None)
    with pytest.raises(ValueError):
        no_wt.run_wave([valid, dict(valid)], max_concurrency=1)
    res = no_wt.run_wave([{"task_id": "TSK-1", "agent": "A1", "verifier": "V1", "action": lambda p: True}, {"task_id": "tsk-1", "agent": "A2", "verifier": "V2", "action": lambda p: True}], max_concurrency=2)
    assert res["TSK-1"][0] and res["tsk-1"][0]


def test_cross_instance_repo_and_rig_lock_sharing_with_override_compat(rig_repo, tmp_path: Path) -> None:
    repo, db, wt_mgr, runner = rig_repo
    (repo / "subdir").mkdir()
    linked_wt = wt_mgr.create_worktree("TSK-LINKED")
    wt_same = WorktreeManager(repo_root=repo)
    wt_sub = WorktreeManager(repo_root=repo / "subdir", worktrees_dir=tmp_path / "wt_sub")
    wt_from_wt = WorktreeManager(repo_root=linked_wt, worktrees_dir=tmp_path / "wt_linked")

    other_repo = tmp_path / "other_repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-b", "main")
    wt_other = WorktreeManager(repo_root=other_repo)

    assert wt_mgr._lock is wt_same._lock is wt_sub._lock is wt_from_wt._lock
    assert wt_other._lock is not wt_mgr._lock
    wt_mgr.remove_worktree("TSK-LINKED", delete_branch=True)

    db2 = StigmeRigDB(rig_dir=repo / ".stigmerig")
    runner2 = WaveRunner(db=db2, worktree_mgr=wt_same)
    assert runner._db_lock is db._lock is db2._lock is runner2._db_lock

    entered: list[int] = []
    inner_lock = wt_mgr._lock

    class _CustomLock:
        def __enter__(self) -> _CustomLock:
            inner_lock.acquire()
            entered.append(1)
            return self

        def __exit__(self, *exc: object) -> None:
            inner_lock.release()

    custom = _CustomLock()
    wt_mgr._lock = custom  # type: ignore[assignment]
    assert wt_mgr._lock is custom and wt_same._lock is inner_lock and WorktreeManager(repo_root=repo)._lock is inner_lock
    wt_ovr = wt_mgr.create_worktree("TSK-OVR")
    assert wt_ovr.is_dir() and entered
    wt_mgr.remove_worktree("TSK-OVR", delete_branch=True)
    wt_mgr._lock = inner_lock

    for tid in ("TSK-SHR-1", "TSK-SHR-2"):
        db.create_task(tid, f"Shared lock {tid}", author="Arch")
    bar = threading.Barrier(2, timeout=10.0)
    r_a = WaveRunner(db=StigmeRigDB(rig_dir=repo / ".stigmerig"), worktree_mgr=WorktreeManager(repo_root=repo))
    r_b = WaveRunner(db=StigmeRigDB(rig_dir=repo / ".stigmerig"), worktree_mgr=WorktreeManager(repo_root=repo))
    assert r_a.worktree_mgr._lock is r_b.worktree_mgr._lock and r_a._db_lock is r_b._db_lock
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_a = pool.submit(r_a.run_wave, [{"task_id": "TSK-SHR-1", "agent": "ag/a", "verifier": "qa/v", "action": lambda p: (bar.wait(), (p / "s1.txt").write_text("1\n", encoding="utf-8")) is not None}], 1)
        f_b = pool.submit(r_b.run_wave, [{"task_id": "TSK-SHR-2", "agent": "ag/b", "verifier": "qa/v", "action": lambda p: (bar.wait(), (p / "s2.txt").write_text("2\n", encoding="utf-8")) is not None}], 1)
        out_a, out_b = f_a.result(), f_b.result()
    assert out_a["TSK-SHR-1"][0] and out_b["TSK-SHR-2"][0]
    assert (repo / "s1.txt").exists() and (repo / "s2.txt").exists() and db.verify_audit_chain()[0]


def test_c1_unicode_nonprintable_bool_and_malformed_task_id_guards(rig_repo, tmp_path: Path, monkeypatch) -> None:
    repo, db, wt_mgr, runner = rig_repo
    db.create_task("TSK-G1", "Guard target", author="Arch")
    db.create_task("TSK-DB-ARCH", "DB archetype guard", author="Arch")
    head0 = _git(repo, "rev-parse", "HEAD")
    ran: list[Path] = []
    act = lambda p: ran.append(p) or True

    for ch in ("\x85", "\x9b", "\u2028", "\u202e"):
        for field, val in (("agent", f"ag{ch}1"), ("verifier", f"ver{ch}1"), ("archetype", f"arch{ch}1")):
            spec = {"task_id": "TSK-G1", "agent": "ag1", "verifier": "ver1", "archetype": "diff_synthesizer", "action": act, field: val}
            assert _invalid_meta(spec["agent"], spec["verifier"], spec["archetype"]) is True
            with pytest.raises(ValueError):
                _validate_wave_specs([spec], True)
            with pytest.raises(ValueError):
                runner.run_wave([spec], max_concurrency=1)
            ok, msg = runner.run_task_in_worktree(
                "TSK-G1",
                spec["agent"],
                act,
                verifier=spec["verifier"],
                archetype=spec["archetype"],
            )
            assert not ok and "Invalid or control-character metadata" in msg

    # Whitespace-only verifier stays on the anti-self-approval error path rather than _invalid_meta
    for ws_ver in ("", "   "):
        assert _invalid_meta("ag1", ws_ver, "diff_synthesizer") is False
        ws_spec = {"task_id": "TSK-G1", "agent": "ag1", "verifier": ws_ver, "action": act}
        assert len(_validate_wave_specs([ws_spec], True)) == 1
        ok_ws, msg_ws = runner.run_task_in_worktree("TSK-G1", "ag1", act, verifier=ws_ver, allow_self_approval=False)
        assert not ok_ws and "Independent verifier required unless allow_self_approval is True" in msg_ws
        res_ws = runner.run_wave([ws_spec], max_concurrency=1)
        assert res_ws["TSK-G1"] == (False, "Independent verifier required unless allow_self_approval is True")

    # Non-boolean allow_self_approval rejected in both _validate_wave_specs and run_task_in_worktree
    for bad_bool in ("false", "true", "1", 1, 0, None, ["x"], {}):
        bad_spec = {"task_id": "TSK-G1", "agent": "ag1", "verifier": "ag1", "allow_self_approval": bad_bool, "action": act}
        with pytest.raises(ValueError, match="allow_self_approval"):
            _validate_wave_specs([bad_spec], True)
        with pytest.raises(ValueError, match="allow_self_approval"):
            runner.run_wave([bad_spec], max_concurrency=1)

    def _forbid_sqlite():
        raise AssertionError("SQLite must not be touched on upfront validation failure")

    monkeypatch.setattr(db, "get_queue_conn", _forbid_sqlite)
    monkeypatch.setattr(db, "get_audit_conn", _forbid_sqlite)

    for bad_bool in ("false", "true", 1, 0, None, []):
        ok_b, msg_b = runner.run_task_in_worktree(
            "TSK-G1", "ag1", act, verifier="ag1", allow_self_approval=bad_bool  # type: ignore[arg-type]
        )
        assert not ok_b and "allow_self_approval" in msg_b

    for bad_id in ("", "   ", "../escape", "TSK/1", " TSK-G1", "TSK-G1 ", "-TSK", ".TSK", "A" * 65, "TSK\x85", None, 123):
        ok_id, msg_id = runner.run_task_in_worktree(
            bad_id, "ag1", act, verifier="ver1"  # type: ignore[arg-type]
        )
        assert not ok_id and "Invalid task_id" in msg_id

    uninit_db = StigmeRigDB(rig_dir=tmp_path / "uninit_rig")
    uninit_runner = WaveRunner(db=uninit_db, worktree_mgr=wt_mgr)
    assert uninit_runner.run_task_in_worktree("../bad", "ag1", act, verifier="ver1")[0] is False
    assert not uninit_db.queue_db_path.exists() and not uninit_db.audit_db_path.exists()

    monkeypatch.undo()
    assert ran == [] and db.get_task("TSK-G1")["status"] == "todo" and not (wt_mgr.worktrees_dir / "TSK-G1").exists()

    # Non-printable DB archetype (including trailing Unicode line separator) rejected before worktree/commit/merge
    with db.get_queue_conn() as conn:
        conn.execute("UPDATE tasks SET archetype = ? WHERE id = 'TSK-DB-ARCH'", ("unit_tester\u2028",))
        conn.commit()
    ok_dba, _ = runner.run_task_in_worktree("TSK-DB-ARCH", "ag1", act, verifier="ver1")
    assert not ok_dba and ran == [] and db.get_task("TSK-DB-ARCH")["status"] == "blocked"
    assert not (wt_mgr.worktrees_dir / "TSK-DB-ARCH").exists() and _git(repo, "rev-parse", "HEAD") == head0
    assert db.verify_audit_chain()[0]

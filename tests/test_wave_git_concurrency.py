"""Concurrent multi-batch WaveRunner, WorktreeManager, and lease contention stress tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
from typing import Any, Callable

import pytest
from stigmerig.db import StigmeRigDB
from stigmerig.wave import WaveRunner, WorktreeManager

EXCLUDE_ARGS = (":(exclude).stigmerig", ":(exclude).stigmerig/**", ":(exclude).worktrees", ":(exclude).worktrees/**", ":(exclude)*.db-wal", ":(exclude)*.db-shm")


def build_wave_specs(actions: list[Callable[[Path], bool]], prefix: str = "TSK-W") -> list[dict[str, Any]]:
    return [{"task_id": f"{prefix}{i}", "agent": f"A{i}", "verifier": f"V{i}", "action": act} for i, act in enumerate(actions, start=1)]


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


def test_multibatch_mixed_outcomes_and_cross_batch_conflict(rig_repo: tuple[Path, StigmeRigDB, WorktreeManager, WaveRunner]) -> None:
    repo, db, wt_mgr, runner = rig_repo
    for idx in range(1, 7):
        db.create_task(task_id=f"TSK-W{idx}", title=f"Wave task {idx}", author="Arch")
    _git(repo, "branch", "task/TSK-W5")
    (repo / "main.db-wal").write_text("wal\n", encoding="utf-8")
    b1, b2 = threading.Barrier(3, timeout=5.0), threading.Barrier(3, timeout=5.0)

    def make_act(idx: int) -> Callable[[Path], bool]:
        def _act(wt: Path) -> bool:
            (b1 if idx <= 3 else b2).wait()
            if idx == 1:
                (wt / "shared.txt").write_text("batch1\n", encoding="utf-8")
                (wt / ".stigmerig").mkdir(exist_ok=True)
                for rel, val in (("feat_1.txt", "one\n"), (".stigmerig/scratch.json", "x\n"), ("wt.db-wal", "x\n"), ("wt.db-shm", "x\n")):
                    (wt / rel).write_text(val, encoding="utf-8")
                return True
            if idx == 2:
                return (wt / "shared.txt").read_text(encoding="utf-8") == "base\n"
            if idx == 3:
                (wt / "aborted.txt").write_text("discard\n", encoding="utf-8")
                return False
            if idx == 4:
                (wt / "uncommitted_crash.txt").write_text("partial work\n", encoding="utf-8")
                raise RuntimeError("worker crash in batch2")
            if idx == 5:
                assert (wt / "shared.txt").read_text(encoding="utf-8") == "base\n"
                (wt / "shared.txt").write_text("batch2_conflict\n", encoding="utf-8")
                return True
            assert (wt / "feat_1.txt").read_text(encoding="utf-8") == "one\n"
            (wt / "feat_6.txt").write_text("six\n", encoding="utf-8")
            return True
        return _act

    res = runner.run_wave(build_wave_specs([make_act(i) for i in range(1, 7)]), max_concurrency=3)
    expected_ok = {"TSK-W1": True, "TSK-W2": True, "TSK-W3": False, "TSK-W4": False, "TSK-W5": False, "TSK-W6": True}
    assert {k: v[0] for k, v in res.items()} == expected_ok and "Merge conflict" in res["TSK-W5"][1]
    assert {tid: db.get_task(tid)["status"] for tid in expected_ok} == {tid: ("done" if ok else "blocked") for tid, ok in expected_ok.items()}
    assert all(db.get_task(tid)["lease_until"] is None for tid in expected_ok)
    assert not (repo / ".git" / "MERGE_HEAD").exists() and (repo / "shared.txt").read_text(encoding="utf-8") == "batch1\n"
    assert set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()) == {"shared.txt", "feat_1.txt", "feat_6.txt"}
    assert _git(repo, "status", "--porcelain", "--", ".", *EXCLUDE_ARGS) == "" and (repo / "main.db-wal").exists()
    branches = {b.strip().lstrip("*+ ") for b in _git(repo, "branch", "--list", "task/*").splitlines() if b.strip()}
    assert branches.isdisjoint({"task/TSK-W1", "task/TSK-W2", "task/TSK-W6"}) and not any((wt_mgr.worktrees_dir / f"TSK-W{i}").exists() for i in (1, 2, 6))
    assert (wt_mgr.worktrees_dir / "TSK-W4" / "uncommitted_crash.txt").read_text(encoding="utf-8") == "partial work\n"
    traps = json.loads((db.rig_dir / "traps.json").read_text(encoding="utf-8"))["traps"]
    assert {tr["source_traces"][0] for tr in traps} == {"Task TSK-W3 (A3)", "Task TSK-W4 (A4)", "Task TSK-W5 (A5)"}
    assert db.verify_audit_chain() == (True, "Verified 18 audit entries with intact hash chain")


def test_preclaimed_task_rejected_without_disturbing_lease_or_siblings(rig_repo: tuple[Path, StigmeRigDB, WorktreeManager, WaveRunner]) -> None:
    repo, db, wt_mgr, runner = rig_repo
    for idx in (1, 2, 3):
        db.create_task(task_id=f"TSK-L{idx}", title=f"Lease task {idx}", author="Arch")
    assert db.claim_task("TSK-L2", agent="Incumbent", ttl_seconds=1800) is True
    before, sib_barrier, rogue_calls = db.get_task("TSK-L2"), threading.Barrier(2, timeout=5.0), []

    def make_sib(idx: int) -> Callable[[Path], bool]:
        def _act(wt: Path) -> bool:
            sib_barrier.wait()
            (wt / f"sib_{idx}.txt").write_text(f"s{idx}\n", encoding="utf-8")
            return True
        return _act

    specs = build_wave_specs([make_sib(1), lambda wt: rogue_calls.append(wt) or True, make_sib(3)], prefix="TSK-L")
    specs[1]["agent"] = "Intruder"
    res = runner.run_wave(specs, max_concurrency=3)
    after = db.get_task("TSK-L2")
    assert res["TSK-L2"] == (False, "Could not claim task TSK-L2 for Intruder") and rogue_calls == []
    assert (after["status"], after["claimed_by"], after["lease_until"], after["version"]) == ("working", "Incumbent", before["lease_until"], before["version"])
    assert not (wt_mgr.worktrees_dir / "TSK-L2").exists() and _git(repo, "branch", "--list", "task/*") == ""
    assert res["TSK-L1"][0] and res["TSK-L3"][0] and db.get_task("TSK-L1")["status"] == "done" and db.get_task("TSK-L3")["status"] == "done"
    assert (repo / "sib_1.txt").read_text(encoding="utf-8") == "s1\n" and (repo / "sib_3.txt").read_text(encoding="utf-8") == "s3\n"
    assert not (db.rig_dir / "traps.json").exists() and db.complete_task("TSK-L2", agent="Incumbent", verifier="V2")[0] and db.verify_audit_chain()[0]

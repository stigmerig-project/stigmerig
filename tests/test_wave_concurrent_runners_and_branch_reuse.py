"""Concurrent multi-WaveRunner contention, non-main target branch, and failed-worktree isolation tests."""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
from typing import Any, Callable
import pytest
from stigmerig.db import StigmeRigDB
from stigmerig.wave import WaveRunner, WorktreeManager, get_active_branch

EXCLUDE_ARGS = (":(exclude).stigmerig", ":(exclude).stigmerig/**", ":(exclude).worktrees", ":(exclude).worktrees/**", ":(exclude)*.db-wal", ":(exclude)*.db-shm")
TRAILER_KEYS = frozenset({"Archetype", "Claimant", "Verifier", "Task-ID"})


def parse_commit_trailers(log_blob: str) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = {}
    for raw_msg in log_blob.split("\x00"):
        fields = {k.strip(): v.strip() for ln in raw_msg.strip().splitlines() if ": " in ln for k, v in (ln.split(": ", 1),) if k.strip() in TRAILER_KEYS}
        if "Task-ID" in fields:
            parsed[fields["Task-ID"]] = fields
    return parsed


def build_runner_specs(prefix: str, verifier: str, task_ids: tuple[str, ...], archetypes: dict[str, str], make_act: Callable[[str, str], Callable[[Path], bool]]) -> list[dict[str, Any]]:
    return [{"task_id": t, "agent": f"{prefix}/{t.lower()}", "verifier": verifier, "archetype": archetypes[t], "action": make_act(t, f"{prefix}/{t.lower()}")} for t in task_ids]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def release_rig(tmp_path: Path) -> tuple[Path, str, StigmeRigDB, WorktreeManager]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (("init", "-b", "main"), ("config", "user.name", "Test"), ("config", "user.email", "t@local"), ("config", "commit.gpgsign", "false")):
        _git(repo, *cmd)
    (repo / "base.txt").write_text("main-v0\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "init main")
    main_sha = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "-b", "release/v1")
    (repo / "release.txt").write_text("rel-v1\n", encoding="utf-8")
    _git(repo, "add", "release.txt")
    _git(repo, "commit", "-m", "init release/v1")
    db = StigmeRigDB(rig_dir=repo / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    return repo, main_sha, db, WorktreeManager(repo_root=repo, swarm_name="ReleaseSwarm")


def test_dual_runners_race_on_release_branch_preserving_failed_worktree(release_rig: tuple[Path, str, StigmeRigDB, WorktreeManager]) -> None:
    repo, main_sha, db, wt_mgr = release_rig
    wt_m0 = wt_mgr.create_worktree("TSK-M0")
    (wt_m0 / "m0.txt").write_text("m0\n", encoding="utf-8")
    _git(wt_m0, "add", "m0.txt")
    _git(wt_m0, "commit", "-m", "direct branch work")
    _git(repo, "checkout", "main")
    assert wt_mgr.merge_worktree("TSK-M0", target_branch="release/v1") == (True, "Successfully merged task/TSK-M0 into release/v1")
    assert get_active_branch(repo) == "release/v1" and _git(repo, "rev-parse", "main") == main_sha
    runner_a, runner_b = WaveRunner(db=db, worktree_mgr=wt_mgr), WaveRunner(db=db, worktree_mgr=wt_mgr)
    db.create_task("TSK-DIAG", "Failing diagnostic task", author="Arch")
    ok_d, _ = runner_a.run_task_in_worktree("TSK-DIAG", "alpha/diag", lambda wt: ((wt / "diag.log").write_text("boom\n", encoding="utf-8"), (wt / "base.txt").write_text("dirty\n", encoding="utf-8")) and False, verifier="ver-a")
    wt_diag = wt_mgr.worktrees_dir / "TSK-DIAG"
    assert not ok_d and db.get_task("TSK-DIAG")["status"] == "blocked" and wt_diag.is_dir()
    archetypes = {"TSK-S1": "unit_tester", "TSK-S2": "policy_gatekeeper", "TSK-S3": "diff_synthesizer", "TSK-A1": "diff_synthesizer", "TSK-A2": "unit_tester", "TSK-B1": "policy_gatekeeper", "TSK-B2": "unit_tester"}
    for tid, arch in archetypes.items():
        db.create_task(tid, f"Wave {tid}", author="Arch", archetype=arch)
    executions, exec_lock, start_bar, b1_bar = {}, threading.Lock(), threading.Barrier(2, timeout=5.0), threading.Barrier(4, timeout=5.0)

    def make_act(tid: str, agent: str) -> Callable[[Path], bool]:
        def _act(wt: Path) -> bool:
            with exec_lock:
                executions.setdefault(tid, []).append(agent)
            if tid in {"TSK-S1", "TSK-S2", "TSK-A1", "TSK-B1"}:
                b1_bar.wait()
            assert (wt / "base.txt").read_text(encoding="utf-8") == "main-v0\n" and not (wt / "diag.log").exists()
            (wt / f"{tid.lower()}.txt").write_text(f"{tid}:{agent}\n", encoding="utf-8")
            return True
        return _act

    specs_a = build_runner_specs("alpha", "ver-alpha", ("TSK-S1", "TSK-S2", "TSK-A1", "TSK-S3", "TSK-A2"), archetypes, make_act)
    specs_b = build_runner_specs("beta", "ver-beta", ("TSK-S1", "TSK-S2", "TSK-B1", "TSK-S3", "TSK-B2"), archetypes, make_act)
    with ThreadPoolExecutor(max_workers=2) as pool:
        res_a, res_b = [f.result() for f in (pool.submit(lambda: (start_bar.wait(), runner_a.run_wave(specs_a, max_concurrency=3))[1]), pool.submit(lambda: (start_bar.wait(), runner_b.run_wave(specs_b, max_concurrency=3))[1]))]

    assert all(res_a[t][0] for t in ("TSK-A1", "TSK-A2")) and all(res_b[t][0] for t in ("TSK-B1", "TSK-B2"))
    assert all((res_a[t][0], res_b[t][0]) in {(True, False), (False, True)} and "Could not claim task" in (res_b[t][1] if res_a[t][0] else res_a[t][1]) for t in ("TSK-S1", "TSK-S2", "TSK-S3"))
    assert set(executions) == set(archetypes) and all(len(v) == 1 for v in executions.values())
    assert get_active_branch(repo) == "release/v1" and _git(repo, "rev-parse", "main") == main_sha and (repo / "m0.txt").read_text(encoding="utf-8") == "m0\n"
    assert all((db.get_task(t)["status"], db.get_task(t)["version"], db.get_task(t)["claimed_by"]) == ("done", 2, executions[t][0]) for t in archetypes)
    assert all((repo / f"{t.lower()}.txt").read_text(encoding="utf-8") == f"{t}:{executions[t][0]}\n" for t in archetypes)
    assert (wt_diag / "diag.log").read_text(encoding="utf-8") == "boom\n" and (wt_diag / "base.txt").read_text(encoding="utf-8") == "dirty\n"
    assert (repo / "base.txt").read_text(encoding="utf-8") == "main-v0\n" and "diag.log" not in _git(repo, "ls-tree", "-r", "--name-only", "release/v1")
    assert _git(repo, "status", "--porcelain", "--", ".", *EXCLUDE_ARGS) == "" and _git(repo, "diff", "--cached", "--name-only") == ""
    assert [p.name for p in wt_mgr.worktrees_dir.iterdir()] == ["TSK-DIAG"] and _git(repo, "branch", "--list", "task/*").strip().lstrip("*+ ") == "task/TSK-DIAG"
    trailers = parse_commit_trailers(_git(repo, "log", f"{main_sha}..release/v1", "--no-merges", "--format=%B%x00"))
    assert all(trailers[t] == {"Archetype": archetypes[t], "Claimant": executions[t][0], "Verifier": ("ver-alpha" if executions[t][0].startswith("alpha/") else "ver-beta"), "Task-ID": t} for t in archetypes)
    assert db.verify_audit_chain() == (True, "Verified 24 audit entries with intact hash chain")


def test_seat_component_self_approval_preclaim_rejection_and_handoff(release_rig: tuple[Path, str, StigmeRigDB, WorktreeManager]) -> None:
    repo, main_sha, db, wt_mgr = release_rig
    for tid, arch in (("TSK-SELF", "policy_gatekeeper"), ("TSK-HAND", "unit_tester")):
        db.create_task(tid, f"Task {tid}", author="Arch", archetype=arch)
    runner_a, runner_b, bad_calls, bar = WaveRunner(db=db, worktree_mgr=wt_mgr), WaveRunner(db=db, worktree_mgr=wt_mgr), [], threading.Barrier(2, timeout=5.0)
    specs_a = [{"task_id": tid, "agent": "dev/seat", "verifier": "qa:seat", "action": lambda wt: bad_calls.append(wt) or True} for tid in ("TSK-SELF", "TSK-HAND")]
    specs_b = [{"task_id": "TSK-HAND", "agent": "ops/worker", "verifier": "qa:lead", "archetype": "unit_tester", "action": lambda wt: (wt / "hand.txt").write_text("ok\n", encoding="utf-8") is not None}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        res_a, res_b = [f.result() for f in (pool.submit(lambda: (bar.wait(), runner_a.run_wave(specs_a, max_concurrency=2))[1]), pool.submit(lambda: (bar.wait(), runner_b.run_wave(specs_b, max_concurrency=1))[1]))]
    t_self = db.get_task("TSK-SELF")
    assert all(res_a[t] == (False, "Independent verifier required unless allow_self_approval is True") for t in ("TSK-SELF", "TSK-HAND")) and bad_calls == []
    assert (t_self["status"], t_self["claimed_by"], t_self["lease_until"], t_self["version"]) == ("todo", None, None, 0) and not (wt_mgr.worktrees_dir / "TSK-SELF").exists()
    assert res_b["TSK-HAND"][0] and get_active_branch(repo) == "release/v1" and (repo / "hand.txt").read_text(encoding="utf-8") == "ok\n" and not (db.rig_dir / "traps.json").exists()
    trailers = parse_commit_trailers(_git(repo, "log", f"{main_sha}..release/v1", "--no-merges", "--format=%B%x00"))
    assert trailers["TSK-HAND"] == {"Archetype": "unit_tester", "Claimant": "ops/worker", "Verifier": "qa:lead", "Task-ID": "TSK-HAND"}
    assert _git(repo, "branch", "--list", "task/*") == "" and db.verify_audit_chain() == (True, "Verified 4 audit entries with intact hash chain")

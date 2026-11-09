"""Unit tests for StigmeRig git worktree manager, wave runner, and telemetry."""

import subprocess
from pathlib import Path

import pytest
from stigmerig.cli import main
from stigmerig.db import StigmeRigDB
from stigmerig.telemetry import TelemetryExporter
from stigmerig.wave import WaveRunner, WorktreeManager


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Fixture initializing an empty git repository with an initial commit."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestAgent"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=str(repo_dir), check=True)

    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    return repo_dir


def test_worktree_lifecycle(git_repo: Path):
    """Verify creation, modification, and clean merging of a task worktree."""
    wt_mgr = WorktreeManager(repo_root=git_repo)
    wt_path = wt_mgr.create_worktree("TASK-101")
    assert wt_path.exists()

    # Modify file in worktree
    feature_file = wt_path / "feature.txt"
    feature_file.write_text("Disjoint feature content", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=str(wt_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=str(wt_path), check=True, capture_output=True)

    merged, msg = wt_mgr.merge_worktree("TASK-101", target_branch="main")
    assert merged is True
    assert "Successfully merged" in msg
    assert not wt_path.exists()

    # Verify file is now on main
    main_feature = git_repo / "feature.txt"
    assert main_feature.exists()
    assert main_feature.read_text(encoding="utf-8") == "Disjoint feature content"


def test_wave_runner_concurrency(git_repo: Path):
    """Verify 3-task wave runner executing tasks across worktrees."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    wt_mgr = WorktreeManager(repo_root=git_repo)
    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)

    # Seed 3 tasks
    for i in range(1, 4):
        db.create_task(
            task_id=f"TSK-W{i}",
            title=f"Wave task {i}",
            parent_id="EPC-TEST",
            author="Architect",
        )

    def make_action(task_num: int):
        def action_fn(worktree_dir: Path) -> bool:
            f = worktree_dir / f"output_{task_num}.txt"
            f.write_text(f"Task {task_num} output", encoding="utf-8")
            return True
        return action_fn

    task_specs = [
        {"task_id": "TSK-W1", "agent": "Agent1", "verifier": "Agent2", "action": make_action(1)},
        {"task_id": "TSK-W2", "agent": "Agent2", "verifier": "Agent3", "action": make_action(2)},
        {"task_id": "TSK-W3", "agent": "Agent3", "verifier": "Agent1", "action": make_action(3)},
    ]

    results = runner.run_wave(task_specs, max_concurrency=3)
    assert len(results) == 3
    for t_id, (ok, msg) in results.items():
        assert ok is True, f"Task {t_id} failed: {msg}"

    # Verify merged files exist in main repo
    for i in range(1, 4):
        assert (git_repo / f"output_{i}.txt").exists()

    # Verify tasks are marked done
    for i in range(1, 4):
        task = db.get_task(f"TSK-W{i}")
        assert task is not None
        assert task["status"] == "done"


def test_telemetry_export(git_repo: Path):
    """Verify Mermaid sequence diagram and duration metrics generation."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    # Perform task workflow
    db.create_task(task_id="TSK-TEL-1", title="Telemetry task", author="BossAgent")
    db.claim_task(task_id="TSK-TEL-1", agent="WorkerAgent")
    db.complete_task(task_id="TSK-TEL-1", agent="WorkerAgent", verifier="VerifierAgent")

    exporter = TelemetryExporter(db)
    seq = exporter.generate_mermaid_sequence(task_id="TSK-TEL-1")

    assert "sequenceDiagram" in seq
    assert "autonumber" in seq
    assert "actor WorkerAgent" in seq
    assert "actor VerifierAgent" in seq
    assert "Submit TSK-TEL-1 for review" in seq
    assert "Verified TSK-TEL-1" in seq

    metrics_md = exporter.generate_metrics_markdown()
    assert "Task Concurrency & Duration Metrics" in metrics_md
    assert "TSK-TEL-1" in metrics_md


def test_cli_telemetry(git_repo: Path, tmp_path: Path):
    """Verify CLI commands for telemetry export."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()
    db.create_task("TSK-CLI-TEL", "CLI Telemetry", author="Architect")

    out_file = tmp_path / "diagram.md"
    code = main(["--rig-dir", str(rig_dir), "telemetry", "sequence", "--output", str(out_file)])
    assert code == 0
    assert out_file.exists()
    assert "sequenceDiagram" in out_file.read_text(encoding="utf-8")


def test_swarm_name_parameterization(git_repo: Path, monkeypatch):
    """Verify STIGMERIG_SWARM_NAME parameterization and default StigmeRig Swarm identity."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    # 1. Verify default
    monkeypatch.delenv("STIGMERIG_SWARM_NAME", raising=False)
    wt_default = WorktreeManager(repo_root=git_repo)
    assert wt_default.swarm_name == "StigmeRig Swarm"

    # 2. Verify parameterized
    monkeypatch.setenv("STIGMERIG_SWARM_NAME", "Custom Swarm")
    wt_mgr = WorktreeManager(repo_root=git_repo)
    assert wt_mgr.swarm_name == "Custom Swarm"

    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)
    assert runner.swarm_name == "Custom Swarm"

    db.create_task(task_id="TSK-SWARM-1", title="Swarm identity task", author="Boss")
    def action_fn(worktree_dir: Path) -> bool:
        (worktree_dir / "custom.txt").write_text("Custom content", encoding="utf-8")
        return True

    ok, msg = runner.run_task_in_worktree(
        task_id="TSK-SWARM-1",
        agent="WorkerAgent",
        action_fn=action_fn,
        verifier="VerifierAgent",
        archetype="diff_synthesizer",
    )
    assert ok is True

    # Check commit log on main
    log = subprocess.run(
        ["git", "log", "-n", "5", "--pretty=format:%an <%ae> | %cn <%ce>"],
        cwd=str(git_repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert "Custom Swarm [diff_synthesizer] <swarm+diff_synthesizer@customswarm.local>" in log
    assert "Custom Swarm [orchestrator] <swarm+orchestrator@customswarm.local>" in log


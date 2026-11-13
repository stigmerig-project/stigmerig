"""Unit tests for StigmeRig git worktree manager, wave runner, and telemetry."""

import hashlib
import json
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


def test_wave_runner_seven_tasks_batching(git_repo: Path):
    """Verify run_wave with 7 task specs and max_concurrency=3 completes all 7 tasks."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    wt_mgr = WorktreeManager(repo_root=git_repo)
    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)

    for i in range(1, 8):
        db.create_task(
            task_id=f"TSK-WAVE7-{i}",
            title=f"Wave 7 task {i}",
            parent_id="EPC-TEST",
            author="Architect",
        )

    def make_action(task_num: int):
        def action_fn(worktree_dir: Path) -> bool:
            f = worktree_dir / f"output_wave7_{task_num}.txt"
            f.write_text(f"Task {task_num} output", encoding="utf-8")
            return True
        return action_fn

    task_specs = [
        {
            "task_id": f"TSK-WAVE7-{i}",
            "agent": f"Agent{i}",
            "verifier": f"Agent{(i % 7) + 1}",
            "action": make_action(i),
        }
        for i in range(1, 8)
    ]

    results = runner.run_wave(task_specs, max_concurrency=3)
    assert len(results) == 7
    for t_id, (ok, msg) in results.items():
        assert ok is True, f"Task {t_id} failed: {msg}"

    for i in range(1, 8):
        assert (git_repo / f"output_wave7_{i}.txt").exists()
        task = db.get_task(f"TSK-WAVE7-{i}")
        assert task is not None
        assert task["status"] == "done"


def test_verify_only_clean_tree_task(git_repo: Path):
    """Verify verify-only action_fn completing cleanly with status == 'done' and worktree cleaned up."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    wt_mgr = WorktreeManager(repo_root=git_repo)
    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)

    db.create_task(task_id="TSK-VONLY-1", title="Verify only task", author="Architect")

    def verify_action(worktree_dir: Path) -> bool:
        readme = worktree_dir / "README.md"
        assert readme.exists()
        return True

    wt_path = wt_mgr.worktrees_dir / "TSK-VONLY-1"

    ok, msg = runner.run_task_in_worktree(
        task_id="TSK-VONLY-1",
        agent="AuditorAgent",
        action_fn=verify_action,
        verifier="VerifierAgent",
    )

    assert ok is True, f"Verify-only task failed: {msg}"
    assert not wt_path.exists()
    task = db.get_task("TSK-VONLY-1")
    assert task is not None
    assert task["status"] == "done"


def test_independent_verifier_contract_enforced(git_repo: Path):
    """Verify run_wave / run_task_in_worktree rejecting missing verifier or verifier == agent."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    wt_mgr = WorktreeManager(repo_root=git_repo)
    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)

    # 1. Missing verifier (None)
    db.create_task(task_id="TSK-REJ-1", title="Missing verifier", author="Architect")
    ok, msg = runner.run_task_in_worktree(
        task_id="TSK-REJ-1",
        agent="WorkerAgent",
        action_fn=lambda p: True,
        verifier=None,
        allow_self_approval=False,
    )
    assert ok is False
    assert "Independent verifier required unless allow_self_approval is True" in msg
    task1 = db.get_task("TSK-REJ-1")
    assert task1["status"] == "todo"

    # 2. Empty string verifier
    db.create_task(task_id="TSK-REJ-2", title="Empty verifier", author="Architect")
    ok, msg = runner.run_task_in_worktree(
        task_id="TSK-REJ-2",
        agent="WorkerAgent",
        action_fn=lambda p: True,
        verifier="   ",
        allow_self_approval=False,
    )
    assert ok is False
    assert "Independent verifier required unless allow_self_approval is True" in msg

    # 3. Verifier equals agent (self-verification)
    db.create_task(task_id="TSK-REJ-3", title="Self verification", author="Architect")
    ok, msg = runner.run_task_in_worktree(
        task_id="TSK-REJ-3",
        agent="WorkerAgent",
        action_fn=lambda p: True,
        verifier="WorkerAgent",
        allow_self_approval=False,
    )
    assert ok is False
    assert "Independent verifier required unless allow_self_approval is True" in msg
    task3 = db.get_task("TSK-REJ-3")
    assert task3["status"] == "todo"

    # 4. run_wave rejects invalid verifier
    db.create_task(task_id="TSK-REJ-4", title="Wave self-approval reject", author="Architect")
    wave_results = runner.run_wave([
        {
            "task_id": "TSK-REJ-4",
            "agent": "WorkerAgent",
            "verifier": "WorkerAgent",
            "action": lambda p: True,
            "allow_self_approval": False,
        }
    ])
    assert wave_results["TSK-REJ-4"][0] is False
    assert "Independent verifier required" in wave_results["TSK-REJ-4"][1]

    # 5. Permitted when allow_self_approval=True
    db.create_task(task_id="TSK-OK-SELF", title="Self approval allowed", author="Architect")
    ok, msg = runner.run_task_in_worktree(
        task_id="TSK-OK-SELF",
        agent="SoloAgent",
        action_fn=lambda p: True,
        verifier="SoloAgent",
        allow_self_approval=True,
    )
    assert ok is True, f"Permitted self-approval failed: {msg}"
    task_ok = db.get_task("TSK-OK-SELF")
    assert task_ok["status"] == "done"


def test_telemetry_safe_mermaid_actor_aliasing(git_repo: Path):
    """Verify special characters like colons and hyphens in actor names are safely aliased in Mermaid."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    db.create_task(task_id="TSK-SPEC-1", title="Special actors", author="lead-seat:coder")
    db.claim_task(task_id="TSK-SPEC-1", agent="lead-seat:coder")
    db.complete_task(
        task_id="TSK-SPEC-1",
        agent="lead-seat:coder",
        verifier="claude-code",
        allow_self_approval=False,
    )

    exporter = TelemetryExporter(db)
    seq = exporter.generate_mermaid_sequence(task_id="TSK-SPEC-1")

    assert 'actor lead_seat_coder as "lead-seat:coder"' in seq
    assert 'actor claude_code as "claude-code"' in seq
    assert "lead_seat_coder->>claude_code: Submit TSK-SPEC-1 for review" in seq
    assert "claude_code->>System: Verified TSK-SPEC-1" in seq
    assert "lead-seat:coder->>" not in seq
    assert "claude-code->>" not in seq


def test_tier_a_uppercase_event_normalization(tmp_path: Path):
    """Verify Tier A uppercase task_events normalize cleanly while preserving raw_event_type."""
    rig_dir = tmp_path / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig(scaffold_icm=False)

    with db.get_queue_conn() as conn:
        conn.execute(
            """
            INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype)
            VALUES
                ('EPIC-LIN', 'Linear coordination epic', 'done', 100, '2026-01-01T10:00:00Z', '2026-01-01T10:15:00Z', NULL, 'diff_synthesizer'),
                ('TASK-LIN-1', 'First task', 'done', 100, '2026-01-01T10:00:00Z', '2026-01-01T10:05:00Z', 'EPIC-LIN', 'diff_synthesizer'),
                ('TASK-LIN-2', 'Second task', 'blocked', 100, '2026-01-01T10:06:00Z', '2026-01-01T10:10:00Z', 'EPIC-LIN', 'unit_tester')
            """
        )
        conn.executemany(
            """
            INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("TASK-LIN-1", "AGENT CREATED", "PlannerAgent", "{}", "2026-01-01T10:00:00Z"),
                ("TASK-LIN-1", "AGENT CLAIMED", "BuilderAgent", "{}", "2026-01-01T10:01:00Z"),
                ("TASK-LIN-1", "AGENT WORKING", "BuilderAgent", "{}", "2026-01-01T10:02:00Z"),
                ("TASK-LIN-1", "AGENT DONE", "BuilderAgent", '{"verifier": "AuditAgent"}', "2026-01-01T10:05:00Z"),
                ("TASK-LIN-2", "TASK_CREATED", "PlannerAgent", "{}", "2026-01-01T10:06:00Z"),
                ("TASK-LIN-2", "AGENT BLOCKED", "TesterAgent", '{"reason": "fixture missing"}', "2026-01-01T10:08:00Z"),
                ("TASK-LIN-2", "AGENT CANCELLED", "PlannerAgent", "{}", "2026-01-01T10:09:00Z"),
                ("TASK-LIN-2", "SELF_EXECUTION_OVERRIDE", "PlannerAgent", "{}", "2026-01-01T10:10:00Z"),
            ],
        )
        conn.commit()

    exporter = TelemetryExporter(db)
    events = exporter.get_events()
    mapped = [(e["task_id"], e["raw_event_type"], e["event_type"]) for e in events]
    assert ("TASK-LIN-1", "AGENT CREATED", "created") in mapped
    assert ("TASK-LIN-1", "AGENT CLAIMED", "claimed") in mapped
    assert ("TASK-LIN-1", "AGENT WORKING", "claimed") in mapped
    assert ("TASK-LIN-1", "AGENT DONE", "completed") in mapped
    assert ("TASK-LIN-2", "TASK_CREATED", "created") in mapped
    assert ("TASK-LIN-2", "AGENT BLOCKED", "blocked") in mapped
    assert ("TASK-LIN-2", "AGENT CANCELLED", "cancelled") in mapped
    assert ("TASK-LIN-2", "SELF_EXECUTION_OVERRIDE", "self_execution_override") in mapped

    durations = {d["task_id"]: d for d in exporter.compute_duration_metrics()}
    assert durations["TASK-LIN-1"]["wait_seconds"] == 60.0
    assert durations["TASK-LIN-1"]["exec_seconds"] == 240.0
    assert durations["TASK-LIN-1"]["total_seconds"] == 300.0

    seq = exporter.generate_mermaid_sequence(task_id="TASK-LIN-1")
    assert "Submit TASK-LIN-1 for review" in seq
    assert "Verified TASK-LIN-1" in seq


def test_topology_classification_and_dmv_alert(tmp_path: Path):
    """Verify linear vs concurrent wave shape detection and verifier ratio alert."""
    rig_dir = tmp_path / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig(scaffold_icm=False)

    with db.get_queue_conn() as conn:
        conn.executemany(
            """
            INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype)
            VALUES (?, ?, ?, 100, ?, ?, ?, ?)
            """,
            [
                ("EPIC-LIN", "Linear Epic", "done", "2026-01-02T09:00:00Z", "2026-01-02T09:30:00Z", None, "diff_synthesizer"),
                ("TASK-LIN-1", "Step 1", "done", "2026-01-02T09:00:00Z", "2026-01-02T09:10:00Z", "EPIC-LIN", "diff_synthesizer"),
                ("TASK-LIN-2", "Step 2", "done", "2026-01-02T09:10:00Z", "2026-01-02T09:20:00Z", "EPIC-LIN", "unit_tester"),
                ("TASK-LIN-3", "Step 3", "done", "2026-01-02T09:20:00Z", "2026-01-02T09:30:00Z", "EPIC-LIN", "policy_gatekeeper"),
                ("EPIC-PAR", "Parallel Epic", "done", "2026-01-02T10:00:00Z", "2026-01-02T10:20:00Z", None, "diff_synthesizer"),
                ("TASK-PAR-1", "Parallel 1", "done", "2026-01-02T10:00:00Z", "2026-01-02T10:15:00Z", "EPIC-PAR", "diff_synthesizer"),
                ("TASK-PAR-2", "Parallel 2", "done", "2026-01-02T10:01:00Z", "2026-01-02T10:14:00Z", "EPIC-PAR", "diff_synthesizer"),
                ("TASK-PAR-3", "Parallel 3", "done", "2026-01-02T10:02:00Z", "2026-01-02T10:16:00Z", "EPIC-PAR", "diff_synthesizer"),
                ("TASK-PAR-4", "Parallel 4", "done", "2026-01-02T10:03:00Z", "2026-01-02T10:12:00Z", "EPIC-PAR", "unit_tester"),
            ],
        )
        conn.execute(
            """
            INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
            VALUES ('TASK-LIN-1', 'SELF_EXECUTION_OVERRIDE', 'LeadAgent', '{}', '2026-01-02T09:05:00Z')
            """
        )
        conn.commit()

    exporter = TelemetryExporter(db)
    gov = exporter.compute_topology_and_dmv_metrics()

    assert gov["total_records"] == 9
    assert gov["total_tasks"] == 7
    assert gov["completed_tasks"] == 7
    assert gov["verify_only_tasks"] == 3
    assert gov["verify_only_ratio"] == round(3 / 7, 4)
    assert gov["dmv_alert"] is True
    assert gov["ceremony_ratio"] == round(4 / 7, 4)

    lin_shape = gov["epics"]["EPIC-LIN"]
    assert lin_shape["total_tasks"] == 3
    assert lin_shape["phase_depth"] == 3
    assert lin_shape["max_wave_width"] == 1
    assert lin_shape["topology_class"] == "linear"

    par_shape = gov["epics"]["EPIC-PAR"]
    assert par_shape["total_tasks"] == 4
    assert par_shape["phase_depth"] == 1
    assert par_shape["max_wave_width"] == 4
    assert par_shape["topology_class"] == "concurrent"

    md = exporter.generate_metrics_markdown()
    assert "# Task Concurrency & Duration Metrics" in md
    assert "## Governance, Topology & Blocker Summary" in md
    assert "`EPIC-LIN`" in md
    assert "`EPIC-PAR`" in md


def test_fail_task_trap_deduplication_and_cli_metrics_json(tmp_path: Path, capsys):
    """Verify fail_task deduplicates recurring blockers into traps.json and CLI exports --json."""
    rig_dir = tmp_path / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig(scaffold_icm=False)

    for idx in range(1, 4):
        db.create_task(
            task_id=f"TASK-PAR-{idx}",
            title=f"Worker unit {idx}",
            parent_id="EPIC-PAR",
            author="PlannerAgent",
        )

    blocker_msg = "Database migration lock timeout during schema upgrade"
    db.fail_task("TASK-PAR-1", agent="WorkerA", reason=blocker_msg)
    db.fail_task("TASK-PAR-2", agent="WorkerB", reason=blocker_msg)
    db.fail_task("TASK-PAR-3", agent="WorkerC", reason=blocker_msg)

    traps_path = rig_dir / "traps.json"
    assert traps_path.exists()
    traps_doc = json.loads(traps_path.read_text(encoding="utf-8"))
    assert traps_doc["version"] == "1.0.0"
    assert len(traps_doc["traps"]) == 1

    trap = traps_doc["traps"][0]
    assert trap["id"] == "TRAP-001"
    assert trap["occurrences"] == 3
    assert trap["promotion_candidate"] is True
    assert trap["status"] == "candidate"
    assert len(trap["source_traces"]) == 3

    capsys.readouterr()
    exit_code = main(["--rig-dir", str(rig_dir), "telemetry", "metrics", "--json"])
    assert exit_code == 0
    stdout_payload = json.loads(capsys.readouterr().out)
    assert "durations" in stdout_payload
    assert "governance_and_topology" in stdout_payload
    gov = stdout_payload["governance_and_topology"]
    assert gov["blocked_reasons"]["total_traps"] == 1
    assert len(gov["blocked_reasons"]["recurring_candidates"]) == 1
    assert gov["blocked_reasons"]["recurring_candidates"][0]["id"] == "TRAP-001"

    out_json = tmp_path / "metrics.json"
    exit_code_file = main(
        ["--rig-dir", str(rig_dir), "telemetry", "metrics", "--json", "--output", str(out_json)]
    )
    assert exit_code_file == 0
    file_payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert file_payload["governance_and_topology"]["blocked_reasons"]["total_traps"] == 1


def test_worktree_manager_task_id_validation_and_containment(git_repo: Path):
    """Verify WorktreeManager raises ValueError on invalid or escaping task IDs."""
    wt_mgr = WorktreeManager(repo_root=git_repo)
    invalid_ids = ["..", ".", "../escape", "foo/bar", ""]

    readme = git_repo / "README.md"
    assert readme.exists()

    for invalid in invalid_ids:
        with pytest.raises(ValueError):
            wt_mgr.create_worktree(invalid)
        with pytest.raises(ValueError):
            wt_mgr.remove_worktree(invalid)
        with pytest.raises(ValueError):
            wt_mgr.merge_worktree(invalid)

    # Verify repo_root and files remain completely intact
    assert git_repo.exists()
    assert readme.exists()
    assert readme.read_text(encoding="utf-8") == "# Test Repo\n"


def test_worktree_preserved_on_action_exception(git_repo: Path):
    """Verify worktree directory and uncommitted work are preserved when task raises."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    wt_mgr = WorktreeManager(repo_root=git_repo)
    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)

    task_id = "TSK-FAIL-1"
    db.create_task(task_id=task_id, title="Failing task", author="Boss")

    expected_worktree = wt_mgr.worktrees_dir / task_id

    def failing_action(worktree_dir: Path) -> bool:
        uncommitted = worktree_dir / "uncommitted_work.txt"
        uncommitted.write_text("precious worker code", encoding="utf-8")
        raise RuntimeError("boom")

    ok, msg = runner.run_task_in_worktree(
        task_id=task_id,
        agent="WorkerA",
        action_fn=failing_action,
        verifier="VerifierB",
    )

    assert ok is False
    assert "boom" in msg
    task = db.get_task(task_id)
    assert task is not None
    assert task["status"] == "blocked"
    assert expected_worktree.exists()
    uncommitted_file = expected_worktree / "uncommitted_work.txt"
    assert uncommitted_file.exists()
    assert uncommitted_file.read_text(encoding="utf-8") == "precious worker code"


def test_pre_claim_self_approval_rejection_normalized(git_repo: Path):
    """Verify normalized self-approval is rejected before claiming or creating worktree."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    wt_mgr = WorktreeManager(repo_root=git_repo)
    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)

    task_id = "TSK-SELF-NORM"
    db.create_task(task_id=task_id, title="Self approval normalized", author="Architect")

    worktree_path = wt_mgr.worktrees_dir / task_id

    ok, msg = runner.run_task_in_worktree(
        task_id=task_id,
        agent="Claude-Opus",
        action_fn=lambda p: True,
        verifier="claude-opus",
        allow_self_approval=False,
    )

    assert ok is False
    assert "Independent verifier required unless allow_self_approval is True" in msg
    task = db.get_task(task_id)
    assert task is not None
    assert task["status"] == "todo"
    assert not worktree_path.exists()


def test_scoped_git_staging_excludes_stigmerig_and_sqlite(git_repo: Path):
    """Verify scoped git staging excludes .stigmerig and sqlite wal/shm artifacts."""
    rig_dir = git_repo / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    wt_mgr = WorktreeManager(repo_root=git_repo)
    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)

    task_id = "TSK-STAGE-1"
    db.create_task(task_id=task_id, title="Staging exclusion test", author="Architect")

    def action_fn(worktree_dir: Path) -> bool:
        # Write valid file
        (worktree_dir / "valid_code.py").write_text("print('hello')", encoding="utf-8")
        # Write excluded artifacts
        stig_dir = worktree_dir / ".stigmerig"
        stig_dir.mkdir(parents=True, exist_ok=True)
        (stig_dir / "temp_queue.db").write_text("sqlite db data", encoding="utf-8")
        (worktree_dir / "audit.db-wal").write_text("wal data", encoding="utf-8")
        (worktree_dir / "audit.db-shm").write_text("shm data", encoding="utf-8")
        return True

    ok, msg = runner.run_task_in_worktree(
        task_id=task_id,
        agent="WorkerA",
        action_fn=action_fn,
        verifier="VerifierB",
    )

    assert ok is True, f"Task failed: {msg}"
    task = db.get_task(task_id)
    assert task["status"] == "done"

    # Verify committed files on main branch
    main_valid = git_repo / "valid_code.py"
    assert main_valid.exists()

    # Verify excluded files are not present in commit tree of main
    ls_files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=str(git_repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    assert "valid_code.py" in ls_files
    assert ".stigmerig/temp_queue.db" not in ls_files
    assert "audit.db-wal" not in ls_files
    assert "audit.db-shm" not in ls_files


def test_complete_task_and_cli_evidence_binding(tmp_path: Path):
    """Verify complete_task and CLI task done --evidence bind SHA-256 digests and reject invalid paths."""
    repo_root = tmp_path / "workspace"
    repo_root.mkdir(parents=True, exist_ok=True)
    rig_dir = repo_root / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    ev_file = repo_root / "proof.txt"
    ev_bytes = b"clean-room verification output\n"
    ev_file.write_bytes(ev_bytes)
    expected_sha = hashlib.sha256(ev_bytes).hexdigest()

    # 1. Direct DB complete_task(..., evidence_paths=...)
    db.create_task(task_id="TASK-EV-1", title="Direct evidence task", author="Architect")
    assert db.claim_task(task_id="TASK-EV-1", agent="WorkerA") is True
    ok, msg = db.complete_task(
        task_id="TASK-EV-1",
        agent="WorkerA",
        verifier="VerifierB",
        evidence_paths=[ev_file],
    )
    assert ok is True, msg

    with db.get_queue_conn() as conn:
        ev_row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND event_type = 'completed'",
            ("TASK-EV-1",),
        ).fetchone()
    assert ev_row is not None
    raw_event_payload = ev_row["payload"]
    event_payload = json.loads(raw_event_payload)
    assert event_payload["evidence"] == [
        {"path": "proof.txt", "sha256": expected_sha, "bytes": len(ev_bytes)}
    ]

    with db.get_audit_conn() as conn:
        audit_row = conn.execute(
            "SELECT payload_hash FROM audit_log WHERE task_id = ? AND action = 'task_completed'",
            ("TASK-EV-1",),
        ).fetchone()
    assert audit_row is not None
    assert audit_row["payload_hash"] == hashlib.sha256(raw_event_payload.encode("utf-8")).hexdigest()

    # 2. CLI task done --evidence
    db.create_task(task_id="TASK-EV-CLI", title="CLI evidence task", author="Architect")
    assert db.claim_task(task_id="TASK-EV-CLI", agent="WorkerA") is True
    code = main(
        [
            "--rig-dir",
            str(rig_dir),
            "task",
            "done",
            "--id",
            "TASK-EV-CLI",
            "--agent",
            "WorkerA",
            "--verifier",
            "VerifierB",
            "--evidence",
            "proof.txt",
        ]
    )
    assert code == 0

    with db.get_queue_conn() as conn:
        cli_ev_row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND event_type = 'completed'",
            ("TASK-EV-CLI",),
        ).fetchone()
    assert cli_ev_row is not None
    cli_raw_payload = cli_ev_row["payload"]
    assert json.loads(cli_raw_payload)["evidence"] == [
        {"path": "proof.txt", "sha256": expected_sha, "bytes": len(ev_bytes)}
    ]
    with db.get_audit_conn() as conn:
        cli_audit_row = conn.execute(
            "SELECT payload_hash FROM audit_log WHERE task_id = ? AND action = 'task_completed'",
            ("TASK-EV-CLI",),
        ).fetchone()
    assert cli_audit_row is not None
    assert cli_audit_row["payload_hash"] == hashlib.sha256(cli_raw_payload.encode("utf-8")).hexdigest()

    # 3. Negative cases must fail before mutating tasks.status or audit.db
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    (repo_root / "empty.txt").write_bytes(b"")
    (repo_root / ".env").write_text("TOKEN=redacted", encoding="utf-8")
    worktrees_dir = repo_root / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    (worktrees_dir / "scratch.txt").write_text("wt", encoding="utf-8")

    db.create_task(task_id="TASK-EV-NEG", title="Negative evidence task", author="Architect")
    assert db.claim_task(task_id="TASK-EV-NEG", agent="WorkerA") is True

    with db.get_audit_conn() as conn:
        baseline_audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    negative_cases = [
        ("missing.txt", FileNotFoundError),
        ("empty.txt", ValueError),
        ("../outside.txt", ValueError),
        (".env", ValueError),
        (".stigmerig/queue.db", ValueError),
        (".worktrees/scratch.txt", ValueError),
        ("queue.db", ValueError),
    ]
    for bad_path, expected_exc in negative_cases:
        with pytest.raises(expected_exc):
            db.complete_task(
                task_id="TASK-EV-NEG",
                agent="WorkerA",
                verifier="VerifierB",
                evidence_paths=[bad_path],
            )
        task_state = db.get_task("TASK-EV-NEG")
        assert task_state is not None
        assert task_state["status"] == "working"
        with db.get_audit_conn() as conn:
            current_audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        assert current_audit_count == baseline_audit_count


def test_telemetry_normative_rework_definition(tmp_path: Path):
    """Verify TelemetryExporter.compute_topology_and_dmv_metrics() across all 4 rework cases."""
    rig_dir = tmp_path / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()

    db.create_task(task_id="EPIC-RW-1", title="Rework Epic", author="Architect")
    for idx in range(1, 6):
        db.create_task(
            task_id=f"TASK-RW-{idx}",
            title=f"Rework case {idx}",
            parent_id="EPIC-RW-1",
            author="Architect",
        )

    with db.get_queue_conn() as conn:
        conn.executemany(
            """
            INSERT INTO task_events (task_id, event_type, agent, payload, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                # 1) Distinct-claimant reassignment -> counts as rework
                ("TASK-RW-1", "claimed", "worker_one", "Initial claim", "2026-11-12T09:00:00Z"),
                ("TASK-RW-1", "claimed", "worker_two", "Reassigned", "2026-11-12T09:10:00Z"),
                # 2) Same-actor repeat claim -> does NOT count as rework
                ("TASK-RW-2", "claimed", "worker_three", "Initial claim", "2026-11-12T09:15:00Z"),
                ("TASK-RW-2", "claimed", "Worker_Three", "Renewed claim", "2026-11-12T09:20:00Z"),
                # 3) Force-claim message prefix -> counts as rework
                ("TASK-RW-3", "claimed", "worker_four", "Force-claimed due to lease expiry", "2026-11-12T09:25:00Z"),
                # 4) Blocked event -> counts as rework
                ("TASK-RW-4", "blocked", "worker_five", "Dependency failure", "2026-11-12T09:30:00Z"),
                # Clean task -> NOT rework
                ("TASK-RW-5", "claimed", "worker_six", "Initial claim", "2026-11-12T09:35:00Z"),
                # Epic-level blocked event -> excluded from leaf rework metrics
                ("EPIC-RW-1", "blocked", "orchestrator", "Epic hold", "2026-11-12T09:40:00Z"),
            ],
        )
        conn.commit()

    exporter = TelemetryExporter(db)
    metrics = exporter.compute_topology_and_dmv_metrics()

    assert metrics["total_tasks"] == 5
    assert metrics["rework_tasks"] == 3
    assert metrics["rework_rate"] == round(3 / 5, 4)




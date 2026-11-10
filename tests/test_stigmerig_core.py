"""Hermetic unit tests for StigmeRig core state manager and CLI."""

import concurrent.futures
import datetime
import json
import sqlite3
from pathlib import Path

import pytest
from stigmerig.cli import main
from stigmerig.db import StigmeRigDB


@pytest.fixture
def temp_rig(tmp_path: Path) -> StigmeRigDB:
    """Fixture providing an initialized StigmeRig instance in an isolated tempdir."""
    rig_dir = tmp_path / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig()
    return db


def test_init_rig(temp_rig: StigmeRigDB):
    """Verify directories and SQLite tables are properly initialized."""
    assert temp_rig.rig_dir.exists()
    assert temp_rig.queue_db_path.exists()
    assert temp_rig.audit_db_path.exists()
    assert temp_rig.tasks_dir.exists()
    assert temp_rig.board_path.exists()

    with temp_rig.get_queue_conn() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "tasks" in tables
        assert "task_events" in tables

    with temp_rig.get_audit_conn() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "audit_log" in tables


def test_task_create_and_query(temp_rig: StigmeRigDB):
    """Verify task creation, file scaffolding, and query filtering."""
    task = temp_rig.create_task(
        task_id="TASK-001",
        title="Initial core feature",
        parent_id="EPIC-001",
        tags="core,test",
        author="ArchitectAlpha",
    )
    assert task["id"] == "TASK-001"
    assert task["status"] == "todo"
    assert task["version"] == 0

    task_file = temp_rig.tasks_dir / "TASK-001.md"
    assert task_file.exists()
    assert "Initial core feature" in task_file.read_text(encoding="utf-8")

    fetched = temp_rig.get_task("TASK-001")
    assert fetched is not None
    assert fetched["title"] == "Initial core feature"

    todo_tasks = temp_rig.list_tasks(status="todo")
    assert len(todo_tasks) == 1
    assert todo_tasks[0]["id"] == "TASK-001"

    filtered_parent = temp_rig.list_tasks(parent_id="EPIC-001")
    assert len(filtered_parent) == 1

    empty_filter = temp_rig.list_tasks(parent_id="EPC-NONEXISTENT")
    assert len(empty_filter) == 0


def test_cas_claim_lifecycle(temp_rig: StigmeRigDB):
    """Verify Compare-And-Swap claim atomicity, conflicts, and lease expiry."""
    temp_rig.create_task(task_id="TASK-002", title="CAS Test Task")

    # Agent 1 claims task
    claimed = temp_rig.claim_task("TASK-002", agent="AgentAlpha", ttl_seconds=3600)
    assert claimed is True

    task = temp_rig.get_task("TASK-002")
    assert task["status"] == "working"
    assert task["claimed_by"] == "AgentAlpha"
    assert task["version"] == 1

    # Agent 2 attempts to claim while Agent 1 holds active lease -> rejected
    conflict = temp_rig.claim_task("TASK-002", agent="AgentBeta", ttl_seconds=3600)
    assert conflict is False

    # Force expiration of lease in SQLite
    past_time = "2020-01-01T00:00:00Z"
    with temp_rig.get_queue_conn() as conn:
        conn.execute("UPDATE tasks SET lease_until = ? WHERE id = 'TASK-002'", (past_time,))
        conn.commit()

    # Now Agent 2 can steal the expired claim
    stolen = temp_rig.claim_task("TASK-002", agent="AgentBeta", ttl_seconds=3600)
    assert stolen is True

    updated = temp_rig.get_task("TASK-002")
    assert updated["claimed_by"] == "AgentBeta"
    assert updated["version"] == 2


def test_anti_self_approval_gate(temp_rig: StigmeRigDB):
    """Enforce that verifying agent cannot match the implementing agent."""
    temp_rig.create_task(task_id="TASK-003", title="Approval Gate Task")
    temp_rig.claim_task("TASK-003", agent="CoderAgent")

    # Attempt self-approval: verifier == implementer
    with pytest.raises(PermissionError) as exc_info:
        temp_rig.complete_task(
            task_id="TASK-003",
            agent="CoderAgent",
            verifier="CoderAgent",
            allow_self_approval=False,
        )
    assert "Self-approval forbidden" in str(exc_info.value)

    # Distinct verifier passes
    ok, success_msg = temp_rig.complete_task(
        task_id="TASK-003",
        agent="CoderAgent",
        verifier="VerifierAgent",
        allow_self_approval=False,
    )
    assert ok is True
    assert "successfully" in success_msg

    task = temp_rig.get_task("TASK-003")
    assert task["status"] == "done"


def test_worm_audit_chain_integrity(temp_rig: StigmeRigDB):
    """Verify SHA-256 hash-chaining and tamper detection."""
    temp_rig.log_audit("TASK-010", "task_created", "System", "unverified", "payload1")
    temp_rig.log_audit("TASK-010", "task_claimed", "Agent1", "in_progress", "payload2")
    temp_rig.log_audit("TASK-010", "task_completed", "Agent1", "verified", "payload3")

    ok, msg = temp_rig.verify_audit_chain()
    assert ok is True
    assert "Verified 3 audit entries" in msg

    # Tamper with an entry in SQLite directly (drop trigger to simulate out-of-band disk corruption)
    with temp_rig.get_audit_conn() as conn:
        conn.execute("DROP TRIGGER IF EXISTS prevent_audit_update")
        conn.execute("UPDATE audit_log SET payload_hash = 'badhash' WHERE id = 2")
        conn.commit()

    tampered_ok, tampered_msg = temp_rig.verify_audit_chain()
    assert tampered_ok is False
    assert "Hash mismatch at ID 2" in tampered_msg


def test_board_rendering(temp_rig: StigmeRigDB):
    """Verify Task_Board.md accurately reflects queue state."""
    temp_rig.create_task("TASK-020", "Todo Task")
    temp_rig.create_task("TASK-021", "Working Task")
    temp_rig.claim_task("TASK-021", "WorkerBob")

    board_content = temp_rig.board_path.read_text(encoding="utf-8")
    assert "TASK-020" in board_content
    assert "TASK-021" in board_content
    assert "WorkerBob" in board_content


def test_cli_execution(tmp_path: Path):
    """Verify complete end-to-end workflow through CLI main entrypoint."""
    rig_dir = tmp_path / "cli_rig"

    # Init
    code = main(["--rig-dir", str(rig_dir), "init"])
    assert code == 0

    # Create task
    code = main([
        "--rig-dir", str(rig_dir),
        "task", "create",
        "--id", "TSK-CLI-1",
        "--title", "CLI Smoke Task",
        "--author", "Tester",
    ])
    assert code == 0

    # Claim task
    code = main([
        "--rig-dir", str(rig_dir),
        "task", "claim",
        "--id", "TSK-CLI-1",
        "--agent", "CLIAgent",
    ])
    assert code == 0

    # Done task
    code = main([
        "--rig-dir", str(rig_dir),
        "task", "done",
        "--id", "TSK-CLI-1",
        "--agent", "CLIAgent",
        "--verifier", "IndependentVerifier",
    ])
    assert code == 0

    # Audit verify
    code = main([
        "--rig-dir", str(rig_dir),
        "audit", "verify",
    ])
    assert code == 0


def test_worm_triggers_prevent_delete_and_update(temp_rig: StigmeRigDB):
    """Verify triggers prevent direct DELETE and UPDATE on audit_log."""
    temp_rig.log_audit("TSK-TRIG-1", "task_created", "System", "unverified", "p1")

    with temp_rig.get_audit_conn() as conn:
        with pytest.raises(sqlite3.DatabaseError) as exc_delete:
            conn.execute("DELETE FROM audit_log WHERE id = 1")
        assert "prevent_audit_delete" in str(exc_delete.value).lower() or "append-only" in str(exc_delete.value).lower()

        with pytest.raises(sqlite3.DatabaseError) as exc_update:
            conn.execute("UPDATE audit_log SET action = 'tampered' WHERE id = 1")
        assert "prevent_audit_update" in str(exc_update.value).lower() or "append-only" in str(exc_update.value).lower()


def test_verify_hash_chain_trigger(temp_rig: StigmeRigDB):
    """Verify verify_hash_chain trigger blocks invalid previous_hash on INSERT."""
    with temp_rig.get_audit_conn() as conn:
        with pytest.raises(sqlite3.DatabaseError) as exc_gen:
            conn.execute(
                """
                INSERT INTO audit_log (
                    timestamp, task_id, action, agent,
                    verification_status, payload_hash, previous_hash, entry_hash
                ) VALUES ('2026-01-01T00:00:00Z', 'TSK-TRIG-2', 'test', 'agent', 'unverified', 'ph', 'bad_prev', 'eh')
                """
            )
        assert "hash chain broken" in str(exc_gen.value).lower()

        genesis = "0" * 64
        conn.execute(
            """
            INSERT INTO audit_log (
                timestamp, task_id, action, agent,
                verification_status, payload_hash, previous_hash, entry_hash
            ) VALUES ('2026-01-01T00:00:00Z', 'TSK-TRIG-2', 'test', 'agent', 'unverified', 'ph', ?, 'valid_entry_hash_1')
            """,
            (genesis,),
        )
        conn.commit()

        with pytest.raises(sqlite3.DatabaseError) as exc_second:
            conn.execute(
                """
                INSERT INTO audit_log (
                    timestamp, task_id, action, agent,
                    verification_status, payload_hash, previous_hash, entry_hash
                ) VALUES ('2026-01-01T00:01:00Z', 'TSK-TRIG-2', 'test2', 'agent', 'unverified', 'ph', 'wrong_prev', 'eh2')
                """
            )
        assert "hash chain broken" in str(exc_second.value).lower()


def test_icm_scaffolding_and_no_icm(tmp_path: Path):
    """Verify ICM scaffolding on init and --no-icm bypass."""
    rig1 = tmp_path / "rig1" / ".stigmerig"
    code = main(["--rig-dir", str(rig1), "init", "--no-icm"])
    assert code == 0
    assert not (tmp_path / "rig1" / "AGENTS.md").exists()
    assert not (tmp_path / "rig1" / "CONTEXT.md").exists()
    assert not (rig1 / "rig.json").exists()

    rig2 = tmp_path / "rig2" / ".stigmerig"
    code = main(["--rig-dir", str(rig2), "init"])
    assert code == 0
    agents_file = tmp_path / "rig2" / "AGENTS.md"
    context_file = tmp_path / "rig2" / "CONTEXT.md"
    rig_json = rig2 / "rig.json"

    assert agents_file.exists()
    assert context_file.exists()
    assert rig_json.exists()

    agents_file.write_text("Custom agents content", encoding="utf-8")
    code = main(["--rig-dir", str(rig2), "init"])
    assert code == 0
    assert agents_file.read_text(encoding="utf-8") == "Custom agents content"


def test_icm_sidecar_fallback(tmp_path: Path, capsys):
    """When AGENTS.md exists, init writes .stigmerig/AGENTS.stigmerig.md and prints hint."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    existing_agents = project_dir / "AGENTS.md"
    existing_agents.write_text("# Existing Project AGENTS\n", encoding="utf-8")

    rig_dir = project_dir / ".stigmerig"
    code = main(["--rig-dir", str(rig_dir), "init"])
    assert code == 0

    assert existing_agents.read_text(encoding="utf-8") == "# Existing Project AGENTS\n"
    sidecar = rig_dir / "AGENTS.stigmerig.md"
    assert sidecar.exists()
    assert "Autonomous Operation Invariants" in sidecar.read_text(encoding="utf-8")

    captured = capsys.readouterr()
    assert "AGENTS.md already exists" in captured.out
    assert "AGENTS.stigmerig.md" in captured.out


def test_seat_attribution_and_session_id(temp_rig: StigmeRigDB, monkeypatch):
    """Verify task_events stores derived seat and session_id."""
    temp_rig.create_task("TSK-SEAT-1", "Seat Attribution Task")
    temp_rig.claim_task("TSK-SEAT-1", "WorkerAlpha")

    monkeypatch.setenv("STIGMERIG_SEAT", "seat-custom-99")
    ok, msg = temp_rig.complete_task(
        task_id="TSK-SEAT-1",
        agent="WorkerAlpha",
        verifier="VerifierBeta",
        session_id="session-uuid-1234",
    )
    assert ok is True

    with temp_rig.get_queue_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = 'TSK-SEAT-1' AND event_type = 'completed'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["seat"] == "seat-custom-99"
        assert payload["session_id"] == "session-uuid-1234"
        assert payload["verifier"] == "VerifierBeta"

    monkeypatch.delenv("STIGMERIG_SEAT", raising=False)
    temp_rig.create_task("TSK-SEAT-2", "Delimiter Seat Task")
    temp_rig.claim_task("TSK-SEAT-2", "lead-seat:coder")
    ok2, _ = temp_rig.complete_task(
        task_id="TSK-SEAT-2",
        agent="lead-seat:coder",
        verifier="VerifierGamma",
        session_id="session-5678",
    )
    assert ok2 is True
    with temp_rig.get_queue_conn() as conn:
        row2 = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = 'TSK-SEAT-2' AND event_type = 'completed'"
        ).fetchone()
        payload2 = json.loads(row2["payload"])
        assert payload2["seat"] == "lead-seat"
        assert payload2["session_id"] == "session-5678"


def test_next_workflow_step(temp_rig: StigmeRigDB, tmp_path: Path, capsys):
    """Verify next_workflow_step adheres to workflow rails and task done CLI returns JSON."""
    temp_rig.create_task("TSK-NF-1", "Feature Task", parent_id="EPC-RAIL-1", archetype="diff_synthesizer")
    temp_rig.create_task("TSK-NF-2", "Test Task", parent_id="EPC-RAIL-1", archetype="unit_tester")
    temp_rig.create_task("TSK-NF-3", "Policy Task", parent_id="EPC-RAIL-1", archetype="policy_gatekeeper")

    temp_rig.claim_task("TSK-NF-1", "DevAgent")
    temp_rig.complete_task("TSK-NF-1", "DevAgent", verifier="VerifierAgent")

    step1 = temp_rig.get_next_workflow_step("TSK-NF-1")
    assert step1["task_id"] == "TSK-NF-2"
    assert step1["archetype"] == "unit_tester"

    temp_rig.claim_task("TSK-NF-2", "TestAgent")
    temp_rig.complete_task("TSK-NF-2", "TestAgent", verifier="VerifierAgent")

    step2 = temp_rig.get_next_workflow_step("TSK-NF-2")
    assert step2["task_id"] == "TSK-NF-3"
    assert step2["archetype"] == "policy_gatekeeper"

    rig_dir = tmp_path / "cli_rails"
    main(["--rig-dir", str(rig_dir), "init", "--no-icm"])
    main(["--rig-dir", str(rig_dir), "task", "create", "--id", "TSK-CLI-A", "--title", "A", "--parent", "EPC-CLI", "--archetype", "diff_synthesizer"])
    main(["--rig-dir", str(rig_dir), "task", "create", "--id", "TSK-CLI-B", "--title", "B", "--parent", "EPC-CLI", "--archetype", "unit_tester"])
    main(["--rig-dir", str(rig_dir), "task", "claim", "--id", "TSK-CLI-A", "--agent", "AgentA"])

    capsys.readouterr()
    code = main([
        "--rig-dir", str(rig_dir),
        "task", "done",
        "--id", "TSK-CLI-A",
        "--agent", "AgentA",
        "--verifier", "AgentV",
        "--session-id", "sess-cli-101",
    ])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is True
    assert data["next_workflow_step"]["task_id"] == "TSK-CLI-B"
    assert data["next_workflow_step"]["archetype"] == "unit_tester"


def test_complete_task_verifier_required_and_cli(temp_rig: StigmeRigDB, tmp_path: Path, capsys):
    """Verify complete_task raises PermissionError without independent verifier and CLI returns 1."""
    temp_rig.create_task("TSK-VREQ-1", "Verifier Required Task")
    temp_rig.claim_task("TSK-VREQ-1", "DevOne")

    with pytest.raises(PermissionError) as exc_info:
        temp_rig.complete_task(
            task_id="TSK-VREQ-1",
            agent="DevOne",
            verifier=None,
            allow_self_approval=False,
        )
    assert "verifier is required" in str(exc_info.value).lower()

    with pytest.raises(PermissionError) as exc_blank:
        temp_rig.complete_task(
            task_id="TSK-VREQ-1",
            agent="DevOne",
            verifier="   ",
            allow_self_approval=False,
        )
    assert "verifier is required" in str(exc_blank.value).lower()

    # Self approval bypass allowed when explicitly enabled
    ok, _ = temp_rig.complete_task(
        task_id="TSK-VREQ-1",
        agent="DevOne",
        verifier=None,
        allow_self_approval=True,
    )
    assert ok is True

    # CLI task done without --verifier returns 1 with verifier/self-approval message in stderr
    rig_dir = tmp_path / "cli_vreq_rig"
    main(["--rig-dir", str(rig_dir), "init", "--no-icm"])
    main(["--rig-dir", str(rig_dir), "task", "create", "--id", "TSK-CLI-V", "--title", "CLI Verifier Task"])
    main(["--rig-dir", str(rig_dir), "task", "claim", "--id", "TSK-CLI-V", "--agent", "DevCli"])

    capsys.readouterr()
    code = main([
        "--rig-dir", str(rig_dir),
        "task", "done",
        "--id", "TSK-CLI-V",
        "--agent", "DevCli",
    ])
    assert code == 1
    err = capsys.readouterr().err
    assert "verifier" in err.lower() or "self-approval" in err.lower()


def test_priority_ascending_ordering(temp_rig: StigmeRigDB):
    """Verify priority 10 sorts ahead of priority 100 in list_tasks and get_next_workflow_step."""
    temp_rig.create_task("TSK-P100", "Default priority task", priority=100)
    temp_rig.create_task("TSK-P10", "High priority task", priority=10)
    temp_rig.create_task("TSK-P50", "Medium priority task", priority=50)

    tasks = temp_rig.list_tasks(status="todo")
    ids = [t["id"] for t in tasks]
    assert ids == ["TSK-P10", "TSK-P50", "TSK-P100"]

    # Verify in get_next_workflow_step
    temp_rig.create_task("TSK-PARENT-1", "Parent Feature", parent_id="EPC-P", archetype="diff_synthesizer", priority=100)
    temp_rig.create_task("TSK-CHILD-DEF", "Child default", parent_id="EPC-P", archetype="unit_tester", priority=100)
    temp_rig.create_task("TSK-CHILD-HIGH", "Child urgent", parent_id="EPC-P", archetype="unit_tester", priority=10)

    temp_rig.claim_task("TSK-PARENT-1", "DevAgent")
    temp_rig.complete_task("TSK-PARENT-1", "DevAgent", verifier="VerifierAgent")

    step = temp_rig.get_next_workflow_step("TSK-PARENT-1")
    assert step["task_id"] == "TSK-CHILD-HIGH"
    assert step["priority"] == 10


def test_claim_task_reopen_and_terminal_states_and_fail_task(temp_rig: StigmeRigDB):
    """Verify lease renewal, rejection of terminal states on claim, and fail_task KeyError."""
    temp_rig.create_task("TSK-CLAIM-1", "Claim renewal task")

    # Initial claim
    ok = temp_rig.claim_task("TSK-CLAIM-1", agent="AgentA", ttl_seconds=300)
    assert ok is True
    t1 = temp_rig.get_task("TSK-CLAIM-1")
    assert t1["status"] == "working"
    assert t1["claimed_by"] == "AgentA"
    assert t1["version"] == 1

    # Active lease renewal by same agent
    ok_renew = temp_rig.claim_task("TSK-CLAIM-1", agent="AgentA", ttl_seconds=600)
    assert ok_renew is True
    t2 = temp_rig.get_task("TSK-CLAIM-1")
    assert t2["version"] == 2
    assert t2["claimed_by"] == "AgentA"

    # Complete task
    ok_comp, _ = temp_rig.complete_task("TSK-CLAIM-1", agent="AgentA", verifier="VerifierB")
    assert ok_comp is True
    assert temp_rig.get_task("TSK-CLAIM-1")["status"] == "done"

    # Cannot claim done task
    assert temp_rig.claim_task("TSK-CLAIM-1", agent="AgentA") is False
    assert temp_rig.claim_task("TSK-CLAIM-1", agent="AgentB") is False

    # Blocked task
    temp_rig.create_task("TSK-CLAIM-2", "Blocked task")
    temp_rig.fail_task("TSK-CLAIM-2", agent="AgentA", reason="Blocked on external dependency")
    assert temp_rig.get_task("TSK-CLAIM-2")["status"] == "blocked"

    # Cannot claim blocked task
    assert temp_rig.claim_task("TSK-CLAIM-2", agent="AgentA") is False
    assert temp_rig.claim_task("TSK-CLAIM-2", agent="AgentB") is False

    # fail_task raises KeyError on nonexistent task
    with pytest.raises(KeyError) as exc_info:
        temp_rig.fail_task("TSK-NONEXISTENT", agent="AgentA", reason="Task does not exist")
    assert "TSK-NONEXISTENT" in str(exc_info.value)


def test_multithreaded_log_audit_worm_integrity(temp_rig: StigmeRigDB):
    """Verify concurrent multi-threaded audit logging maintains unbroken hash chain."""
    num_threads = 6
    entries_per_thread = 5
    total_entries = num_threads * entries_per_thread

    def worker(worker_id: int):
        for j in range(entries_per_thread):
            temp_rig.log_audit(
                task_id=f"TSK-MT-{worker_id}",
                action=f"action_{j}",
                agent=f"Worker_{worker_id}",
                verification_status="verified",
                payload=f"thread_{worker_id}_entry_{j}",
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    ok, msg = temp_rig.verify_audit_chain()
    assert ok is True
    assert f"Verified {total_entries} audit entries" in msg


def test_cli_uninitialized_and_duplicate_error_handling(tmp_path: Path, capsys):
    """Verify CLI task list before init and duplicate task create return 1 without unhandled crash."""
    uninit_dir = tmp_path / "uninit_rig"

    # Task list before init
    code = main(["--rig-dir", str(uninit_dir), "task", "list"])
    assert code == 1
    err = capsys.readouterr().err
    assert "[ERROR]" in err

    # Initialize rig
    init_code = main(["--rig-dir", str(uninit_dir), "init", "--no-icm"])
    assert init_code == 0

    # Create original task
    c1 = main([
        "--rig-dir", str(uninit_dir),
        "task", "create",
        "--id", "TSK-DUP-1",
        "--title", "Original Task",
    ])
    assert c1 == 0

    # Duplicate task create -> exit code 1
    capsys.readouterr()
    c2 = main([
        "--rig-dir", str(uninit_dir),
        "task", "create",
        "--id", "TSK-DUP-1",
        "--title", "Duplicate Task",
    ])
    assert c2 == 1
    err2 = capsys.readouterr().err
    assert "[ERROR]" in err2

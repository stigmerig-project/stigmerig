"""Hermetic unit tests for CLI review/approve lifecycle, multiline diffstats, WaveRunner friction gates, and dual-tier telemetry."""

from __future__ import annotations

import contextlib
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from stigmerig import cli
from stigmerig.db import StigmeRigDB, VERIFY_ONLY_ARCHETYPES
from stigmerig.telemetry import TelemetryExporter
from stigmerig.wave import WaveRunner, WorktreeManager


class TestCliReviewApprovalAndBackpressure(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.rig_dir = self.root / ".stigmerig"
        code, out, err = self._run_cli("init", "--no-icm")
        self.assertEqual(code, 0, f"init failed: {err}")

    def tearDown(self) -> None:
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        full_argv = ["--rig-dir", str(self.rig_dir), *argv]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(full_argv)
        return code, out.getvalue(), err.getvalue()

    def test_task_review_and_approve_flow(self) -> None:
        code, out, err = self._run_cli(
            "task", "create",
            "--id", "TASK-001",
            "--title", "Implement review workflow",
            "--archetype", "diff_synthesizer",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self._run_cli("task", "claim", "--id", "TASK-001", "--agent", "builder-1")
        self.assertEqual(code, 0, err)
        db = StigmeRigDB(rig_dir=self.rig_dir)
        task = db.get_task("TASK-001")
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "working")
        self.assertEqual(task["claimed_by"], "builder-1")

        code, out, err = self._run_cli(
            "task", "review",
            "--id", "TASK-001",
            "--agent", "builder-1",
            "--summary", "Feature implemented with unit tests",
            "--verification-cmd", "python -m unittest",
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("task_id"), "TASK-001")

        task = db.get_task("TASK-001")
        self.assertEqual(task["status"], "review")
        self.assertIsNone(task["lease_until"])

        code, out, err = self._run_cli(
            "task", "approve",
            "--id", "TASK-001",
            "--verifier", "verifier-1",
            "--session-id", "sess-100",
        )
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("task_id"), "TASK-001")
        self.assertIn("next_workflow_step", res)

        task = db.get_task("TASK-001")
        self.assertEqual(task["status"], "done")
        self.assertIsNone(task["lease_until"])

    def test_task_review_and_fail_flow(self) -> None:
        code, out, err = self._run_cli(
            "task", "create",
            "--id", "TASK-002",
            "--title", "Buggy feature",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self._run_cli("task", "claim", "--id", "TASK-002", "--agent", "builder-1")
        self.assertEqual(code, 0, err)

        code, out, err = self._run_cli(
            "task", "review",
            "--id", "TASK-002",
            "--agent", "builder-1",
            "--summary", "Submitted with potential defects",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self._run_cli(
            "task", "fail",
            "--id", "TASK-002",
            "--agent", "verifier-1",
            "--reason", "Security vulnerability in boundary parser",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("marked blocked", out)

        db = StigmeRigDB(rig_dir=self.rig_dir)
        task = db.get_task("TASK-002")
        self.assertEqual(task["status"], "blocked")

    def test_review_bounce_self_approval_blocks(self) -> None:
        code, out, err = self._run_cli(
            "task", "create",
            "--id", "TASK-003",
            "--title", "Self approval block test",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self._run_cli("task", "claim", "--id", "TASK-003", "--agent", "builder-1")
        self.assertEqual(code, 0, err)

        code, out, err = self._run_cli(
            "task", "review",
            "--id", "TASK-003",
            "--agent", "builder-1",
            "--summary", "Awaiting independent check",
        )
        self.assertEqual(code, 0, err)

        # 1. Exact implementer cannot approve
        code, out, err = self._run_cli(
            "task", "approve",
            "--id", "TASK-003",
            "--verifier", "builder-1",
        )
        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", err)
        self.assertIn("Self-approval forbidden", err)

        # 2. Scoped variant cannot approve
        code, out, err = self._run_cli(
            "task", "approve",
            "--id", "TASK-003",
            "--verifier", "builder-1/reviewer",
        )
        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", err)

        # 3. Independent verifier succeeds
        code, out, err = self._run_cli(
            "task", "approve",
            "--id", "TASK-003",
            "--verifier", "verifier-2",
        )
        self.assertEqual(code, 0, err)
        db = StigmeRigDB(rig_dir=self.rig_dir)
        self.assertEqual(db.get_task("TASK-003")["status"], "done")

    def test_task_claim_backpressure_and_overrides(self) -> None:
        rig_json = self.rig_dir / "rig.json"
        rig_json.write_text(json.dumps({"version": "0.1.0", "max_pending_review": 2}, indent=2), encoding="utf-8")

        for idx in (1, 2):
            tid = f"TSK-REV-{idx}"
            self._run_cli("task", "create", "--id", tid, "--title", f"Review task {idx}")
            self._run_cli("task", "claim", "--id", tid, "--agent", f"builder-{idx}")
            self._run_cli("task", "review", "--id", tid, "--agent", f"builder-{idx}", "--summary", f"Ready {idx}")

        # Pending reviews is now 2 (at cap). Non-verifier todo claim must fail.
        code, out, err = self._run_cli(
            "task", "create",
            "--id", "TSK-BP-BLOCKED",
            "--title", "Blocked builder task",
            "--archetype", "diff_synthesizer",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self._run_cli(
            "task", "claim",
            "--id", "TSK-BP-BLOCKED",
            "--agent", "builder-3",
        )
        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", err)

        # Overriding with --force-backpressure succeeds
        code, out, err = self._run_cli(
            "task", "claim",
            "--id", "TSK-BP-BLOCKED",
            "--agent", "builder-3",
            "--force-backpressure",
        )
        self.assertEqual(code, 0, err)
        db = StigmeRigDB(rig_dir=self.rig_dir)
        self.assertEqual(db.get_task("TSK-BP-BLOCKED")["status"], "working")

        # Working lease renewal is exempt from backpressure
        code, out, err = self._run_cli(
            "task", "claim",
            "--id", "TSK-BP-BLOCKED",
            "--agent", "builder-3",
        )
        self.assertEqual(code, 0, err)

        # Verifier archetypes are exempt from backpressure
        for arch in sorted(VERIFY_ONLY_ARCHETYPES):
            v_tid = f"TSK-VER-{arch.replace('_', '-')}"
            code, out, err = self._run_cli("task", "create", "--id", v_tid, "--title", f"Verify {arch}", "--archetype", arch)
            self.assertEqual(code, 0, err)
            code, out, err = self._run_cli("task", "claim", "--id", v_tid, "--agent", "verifier-agent")
            self.assertEqual(code, 0, f"Expected verifier archetype {arch} to bypass claim backpressure: {err}")


class TestCliMultilineDiffstat(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.rig_dir = self.root / ".stigmerig"
        code, _, err = self._run_cli("init", "--no-icm")
        self.assertEqual(code, 0, err)

    def tearDown(self) -> None:
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        full_argv = ["--rig-dir", str(self.rig_dir), *argv]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(full_argv)
        return code, out.getvalue(), err.getvalue()

    def test_diffstat_multiline_lf_and_crlf_success(self) -> None:
        self._run_cli("task", "create", "--id", "TSK-DS1", "--title", "Multiline LF diffstat test")
        self._run_cli("task", "claim", "--id", "TSK-DS1", "--agent", "builder-1")

        diffstat_lf = "src/core.py | 12 +++++++++---\n src/util.py |  4 +---\n 2 files changed, 10 insertions(+), 6 deletions(-)"
        code, out, err = self._run_cli(
            "task", "review",
            "--id", "TSK-DS1",
            "--agent", "builder-1",
            "--summary", "Completed changes",
            "--diffstat", diffstat_lf,
        )
        self.assertEqual(code, 0, err)

        db = StigmeRigDB(rig_dir=self.rig_dir)
        with db.get_queue_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = 'TSK-DS1' AND event_type = 'review_submitted' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        payload = json.loads(row["payload"])
        self.assertEqual(payload.get("diffstat"), diffstat_lf.strip())

        # CRLF multiline diffstat
        self._run_cli("task", "create", "--id", "TSK-DS2", "--title", "Multiline CRLF diffstat test")
        self._run_cli("task", "claim", "--id", "TSK-DS2", "--agent", "builder-1")
        diffstat_crlf = "src/main.rs | 5 +----\r\n 1 file changed, 1 insertion(+), 4 deletions(-)"
        code, out, err = self._run_cli(
            "task", "review",
            "--id", "TSK-DS2",
            "--agent", "builder-1",
            "--summary", "Completed Rust changes",
            "--diffstat", diffstat_crlf,
        )
        self.assertEqual(code, 0, err)

        with db.get_queue_conn() as conn:
            row2 = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = 'TSK-DS2' AND event_type = 'review_submitted' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row2)
        payload2 = json.loads(row2["payload"])
        self.assertEqual(payload2.get("diffstat"), diffstat_crlf.strip())

    def test_diffstat_validation_guards(self) -> None:
        self._run_cli("task", "create", "--id", "TSK-DS3", "--title", "Validation guards test")
        self._run_cli("task", "claim", "--id", "TSK-DS3", "--agent", "builder-1")

        # Whitespace-only diffstat is rejected
        code, out, err = self._run_cli(
            "task", "review",
            "--id", "TSK-DS3",
            "--agent", "builder-1",
            "--summary", "Whitespace diffstat",
            "--diffstat", "   \n\t  ",
        )
        self.assertEqual(code, 1)
        self.assertIn("--diffstat must not be empty or whitespace", err)

        # Control characters like embedded NUL or bell byte are rejected
        code, out, err = self._run_cli(
            "task", "review",
            "--id", "TSK-DS3",
            "--agent", "builder-1",
            "--summary", "NUL byte diffstat",
            "--diffstat", "src/bad.py | 1 +\x00",
        )
        self.assertEqual(code, 1)
        self.assertIn("must not contain embedded NUL bytes", err)

        code, out, err = self._run_cli(
            "task", "review",
            "--id", "TSK-DS3",
            "--agent", "builder-1",
            "--summary", "Bell byte diffstat",
            "--diffstat", "src/bad.py | 1 +\x07",
        )
        self.assertEqual(code, 1)
        self.assertIn("must not contain control or non-printable", err)


class TestWaveRunnerFrictionAndHeadIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmp.name) / "repo"
        cls.repo.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init", "-b", "main"], cwd=str(cls.repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(cls.repo), check=True)
        subprocess.run(["git", "config", "user.email", "runner@local.test"], cwd=str(cls.repo), check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(cls.repo), check=True)

        base_file = cls.repo / "base.txt"
        base_file.write_text("initial base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=str(cls.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(cls.repo), check=True, capture_output=True)

        cls.db = StigmeRigDB(rig_dir=cls.repo / ".stigmerig", workspace_root=cls.repo)
        cls.db.init_rig(scaffold_icm=False)
        cls.wt_mgr = WorktreeManager(repo_root=cls.repo)
        cls.runner = WaveRunner(db=cls.db, worktree_mgr=cls.wt_mgr)

        rig_cfg = {
            "version": "0.1.0",
            "max_pending_review": 3,
            "high_friction_tags": ["security", "infra"],
            "high_friction_globs": ["critical/**", "auth/*.py"],
        }
        (cls.repo / ".stigmerig" / "rig.json").write_text(json.dumps(rig_cfg, indent=2), encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.repo.exists():
            for tid in ("TSK-W1", "TSK-W2", "TSK-W3"):
                try:
                    cls.wt_mgr.remove_worktree(tid, delete_branch=True)
                except Exception:
                    pass
            try:
                subprocess.run(["git", "worktree", "prune"], cwd=str(cls.repo), capture_output=True)
            except Exception:
                pass
        import gc
        gc.collect()
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        cmd_cwd = cwd if cwd is not None else self.repo
        proc = subprocess.run(["git", *args], cwd=str(cmd_cwd), check=True, capture_output=True, text=True)
        return proc.stdout.strip()

    def test_wave_runner_friction_pass_with_evidence(self) -> None:
        self.db.create_task("TSK-W1", "Security patch", tags="security")
        evidence = self.repo / "security_scan.log"
        evidence.write_text("vulnerability scan clean\n", encoding="utf-8")

        def action(exec_dir: Path) -> bool:
            (exec_dir / "safe_patch.txt").write_text("safe patch content\n", encoding="utf-8")
            return True

        ok, msg = self.runner.run_task_in_worktree(
            task_id="TSK-W1",
            agent="builder-1",
            action_fn=action,
            verifier="verifier-1",
            allow_self_approval=False,
            evidence_paths=[evidence],
        )
        self.assertTrue(ok, f"Expected friction pass with evidence: {msg}")

        task = self.db.get_task("TSK-W1")
        self.assertEqual(task["status"], "done")
        self.assertTrue((self.repo / "safe_patch.txt").is_file())
        audit_ok, audit_msg = self.db.verify_audit_chain()
        self.assertTrue(audit_ok, audit_msg)

    def test_wave_runner_friction_rejection_leaves_head_unchanged_and_retains_worktree(self) -> None:
        head_before = self._git("rev-parse", "HEAD")
        self.db.create_task("TSK-W2", "Security patch without evidence", tags="security")

        def action_with_commit(exec_dir: Path) -> bool:
            (exec_dir / "unverified_change.txt").write_text("unverified change\n", encoding="utf-8")
            self._git("add", "unverified_change.txt", cwd=exec_dir)
            self._git("commit", "-m", "In-action commit for security task", cwd=exec_dir)
            return True

        ok, msg = self.runner.run_task_in_worktree(
            task_id="TSK-W2",
            agent="builder-1",
            action_fn=action_with_commit,
            verifier="verifier-1",
            allow_self_approval=False,
            evidence_paths=None,
        )
        self.assertFalse(ok)
        self.assertIn("Intentional friction gate requires non-empty SHA-256 evidence", msg)

        # Target branch main HEAD must remain strictly unchanged
        head_after = self._git("rev-parse", "HEAD")
        self.assertEqual(head_before, head_after)
        self.assertFalse((self.repo / "unverified_change.txt").exists())

        # Worktree must be retained for inspection
        wt_path = self.wt_mgr.worktrees_dir / "TSK-W2"
        self.assertTrue(wt_path.is_dir())
        self.assertTrue((wt_path / "unverified_change.txt").is_file())

        # Task in DB must be marked blocked
        task = self.db.get_task("TSK-W2")
        self.assertEqual(task["status"], "blocked")

    def test_wave_runner_friction_glob_rejection_leaves_head_unchanged(self) -> None:
        head_before = self._git("rev-parse", "HEAD")
        self.db.create_task("TSK-W3", "Modify critical infra", tags=None)

        def action_touching_glob(exec_dir: Path) -> bool:
            crit_dir = exec_dir / "critical"
            crit_dir.mkdir(parents=True, exist_ok=True)
            (crit_dir / "keys.json").write_text('{"key": "val"}\n', encoding="utf-8")
            self._git("add", "critical/keys.json", cwd=exec_dir)
            self._git("commit", "-m", "Add critical keys inside action", cwd=exec_dir)
            return True

        # Attempting self-approval on high-friction glob path
        ok, msg = self.runner.run_task_in_worktree(
            task_id="TSK-W3",
            agent="builder-1",
            action_fn=action_touching_glob,
            allow_self_approval=True,
        )
        self.assertFalse(ok)
        self.assertIn("Self-approval forbidden by intentional friction gate", msg)

        # Target branch main HEAD must remain unchanged
        head_after = self._git("rev-parse", "HEAD")
        self.assertEqual(head_before, head_after)
        self.assertFalse((self.repo / "critical").exists())

        # Worktree must be retained
        wt_path = self.wt_mgr.worktrees_dir / "TSK-W3"
        self.assertTrue(wt_path.is_dir())
        self.assertTrue((wt_path / "critical" / "keys.json").is_file())

        task = self.db.get_task("TSK-W3")
        self.assertEqual(task["status"], "blocked")


class TestBottleneckTelemetryTierAAndB(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.rig_dir = self.root / ".stigmerig"

    def tearDown(self) -> None:
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def test_tier_b_telemetry_calculations(self) -> None:
        db = StigmeRigDB(rig_dir=self.rig_dir)
        db.init_rig(scaffold_icm=False)
        rig_json = self.rig_dir / "rig.json"
        rig_json.write_text(json.dumps({"version": "0.1.0", "max_pending_review": 1}, indent=2), encoding="utf-8")

        # TSK-B1: completed with review (created 10:00, claimed 10:02, review 10:10, completed 10:15)
        # TSK-B2: currently in review status (pending)
        # TSK-B3: completed self-approved without verifier
        t_base = "2026-03-01T10:00:00Z"
        tasks_data = [
            ("TSK-B1", "Feature 1", "done", "2026-03-01T10:00:00Z", "2026-03-01T10:15:00Z", "diff_synthesizer"),
            ("TSK-B2", "Feature 2", "review", "2026-03-01T10:00:00Z", "2026-03-01T10:05:00Z", "diff_synthesizer"),
            ("TSK-B3", "Feature 3", "done", "2026-03-01T10:00:00Z", "2026-03-01T10:04:00Z", "diff_synthesizer"),
        ]
        events_data = [
            ("TSK-B1", "created", "System", "{}", "2026-03-01T10:00:00Z"),
            ("TSK-B1", "claimed", "builder-1", "{}", "2026-03-01T10:02:00Z"),
            ("TSK-B1", "review_submitted", "builder-1", '{"summary": "ready"}', "2026-03-01T10:10:00Z"),
            ("TSK-B1", "completed", "builder-1", '{"verifier": "verifier-1", "implementer": "builder-1"}', "2026-03-01T10:15:00Z"),

            ("TSK-B2", "created", "System", "{}", "2026-03-01T10:00:00Z"),
            ("TSK-B2", "claimed", "builder-2", "{}", "2026-03-01T10:01:00Z"),
            ("TSK-B2", "review_submitted", "builder-2", '{"summary": "in review"}', "2026-03-01T10:05:00Z"),

            ("TSK-B3", "created", "System", "{}", "2026-03-01T10:00:00Z"),
            ("TSK-B3", "claimed", "builder-3", "{}", "2026-03-01T10:01:00Z"),
            ("TSK-B3", "completed", "builder-3", '{"verifier": "builder-3", "implementer": "builder-3"}', "2026-03-01T10:04:00Z"),
        ]

        with db.get_queue_conn() as conn:
            conn.executemany(
                "INSERT INTO tasks (id, title, status, created_at, updated_at, archetype, priority) VALUES (?, ?, ?, ?, ?, ?, 100)",
                tasks_data,
            )
            conn.executemany(
                "INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
                events_data,
            )
            conn.commit()

        exporter = TelemetryExporter(db)
        durations = {d["task_id"]: d for d in exporter.compute_duration_metrics()}

        # Check TSK-B1 durations: wait=120s, exec=780s, total=900s, review=300s
        d1 = durations["TSK-B1"]
        self.assertEqual(d1["wait_seconds"], 120.0)
        self.assertEqual(d1["exec_seconds"], 780.0)
        self.assertEqual(d1["total_seconds"], 900.0)
        self.assertEqual(d1["review_seconds"], 300.0)

        # Check TSK-B3 has no review_seconds
        d3 = durations["TSK-B3"]
        self.assertNotIn("review_seconds", d3)

        # Check governance and topology metrics
        metrics = exporter.compute_topology_and_dmv_metrics()
        self.assertEqual(metrics["pending_review_tasks"], 1)
        self.assertEqual(metrics["completed_tasks"], 2)
        # 1 unverified (TSK-B3) out of 2 completed = 0.5
        self.assertEqual(metrics["unverified_completion_rate"], 0.5)
        # review_wait_ratio = 300s review wait / 900s total duration = 0.3333
        self.assertEqual(metrics["review_wait_ratio"], 0.3333)
        # pending_review_tasks (1) >= max_pending_review (1) -> review_bottleneck_alert is True
        self.assertTrue(metrics["review_bottleneck_alert"])

    def test_tier_a_telemetry_calculations(self) -> None:
        self.rig_dir.mkdir(parents=True, exist_ok=True)
        rig_json = self.rig_dir / "rig.json"
        rig_json.write_text(json.dumps({"version": "0.1.0", "max_pending_review": 2}, indent=2), encoding="utf-8")

        db_path = self.rig_dir / "queue.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, created_at TEXT, updated_at TEXT, parent_id TEXT, archetype TEXT)"
            )
            conn.execute(
                "CREATE TABLE task_events (event_id INTEGER PRIMARY KEY, task_id TEXT, event_type TEXT, actor TEXT, message TEXT, created_at TEXT)"
            )
            conn.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, NULL, ?)",
                [
                    ("TSK-A1", "Task A1", "done", "2026-03-01T12:00:00Z", "2026-03-01T12:20:00Z", "diff_synthesizer"),
                    ("TSK-A2", "Task A2", "review", "2026-03-01T12:00:00Z", "2026-03-01T12:10:00Z", "diff_synthesizer"),
                ],
            )
            conn.executemany(
                "INSERT INTO task_events (event_id, task_id, event_type, actor, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, "TSK-A1", "AGENT CREATED", "System", "{}", "2026-03-01T12:00:00Z"),
                    (2, "TSK-A1", "AGENT CLAIMED", "builder-a", "{}", "2026-03-01T12:05:00Z"),
                    (3, "TSK-A1", "TASK_REVIEW", "builder-a", "{}", "2026-03-01T12:15:00Z"),
                    (4, "TSK-A1", "TASK_COMPLETED", "builder-a", '{"verifier": "verifier-b", "implementer": "builder-a"}', "2026-03-01T12:20:00Z"),

                    (5, "TSK-A2", "AGENT CREATED", "System", "{}", "2026-03-01T12:00:00Z"),
                    (6, "TSK-A2", "AGENT CLAIMED", "builder-b", "{}", "2026-03-01T12:05:00Z"),
                    (7, "TSK-A2", "AGENT REVIEW", "builder-b", "{}", "2026-03-01T12:10:00Z"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        db = StigmeRigDB(rig_dir=self.rig_dir)
        exporter = TelemetryExporter(db)
        durations = {d["task_id"]: d for d in exporter.compute_duration_metrics()}

        # TSK-A1: wait=300s, exec=900s, total=1200s, review=300s
        d1 = durations["TSK-A1"]
        self.assertEqual(d1["wait_seconds"], 300.0)
        self.assertEqual(d1["exec_seconds"], 900.0)
        self.assertEqual(d1["total_seconds"], 1200.0)
        self.assertEqual(d1["review_seconds"], 300.0)

        metrics = exporter.compute_topology_and_dmv_metrics()
        self.assertEqual(metrics["pending_review_tasks"], 1)
        self.assertEqual(metrics["completed_tasks"], 1)
        self.assertEqual(metrics["unverified_completion_rate"], 0.0)
        # review_wait_ratio = 300 / 1200 = 0.25
        self.assertEqual(metrics["review_wait_ratio"], 0.25)
        # pending_review_tasks (1) < cap (2) and ratio (0.25) <= 0.40 -> alert is False
        self.assertFalse(metrics["review_bottleneck_alert"])

    def test_review_bottleneck_alert_conditions(self) -> None:
        db = StigmeRigDB(rig_dir=self.rig_dir)
        db.init_rig(scaffold_icm=False)

        # Ratio > 0.40 triggers alert even if pending < cap
        (self.rig_dir / "rig.json").write_text(json.dumps({"version": "0.1.0", "max_pending_review": 5}, indent=2), encoding="utf-8")
        tasks_data = [
            ("TSK-RATIO", "High ratio task", "done", "2026-03-01T10:00:00Z", "2026-03-01T10:10:00Z", "diff_synthesizer"),
        ]
        # Total duration = 600s, review duration = 300s -> ratio = 0.50 (> 0.40)
        events_data = [
            ("TSK-RATIO", "created", "System", "{}", "2026-03-01T10:00:00Z"),
            ("TSK-RATIO", "claimed", "builder-1", "{}", "2026-03-01T10:01:00Z"),
            ("TSK-RATIO", "review_submitted", "builder-1", "{}", "2026-03-01T10:05:00Z"),
            ("TSK-RATIO", "completed", "builder-1", '{"verifier": "verifier-1"}', "2026-03-01T10:10:00Z"),
        ]
        with db.get_queue_conn() as conn:
            conn.executemany("INSERT INTO tasks (id, title, status, created_at, updated_at, archetype, priority) VALUES (?, ?, ?, ?, ?, ?, 100)", tasks_data)
            conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, ?, ?, ?)", events_data)
            conn.commit()

        exporter = TelemetryExporter(db)
        metrics = exporter.compute_topology_and_dmv_metrics()
        self.assertEqual(metrics["review_wait_ratio"], 0.5)
        self.assertTrue(metrics["review_bottleneck_alert"])

    def test_cli_telemetry_metrics_json_and_markdown(self) -> None:
        db = StigmeRigDB(rig_dir=self.rig_dir)
        db.init_rig(scaffold_icm=False)
        db.create_task("TSK-CLI1", "CLI metrics task")
        db.claim_task("TSK-CLI1", "builder-1")
        db.submit_for_review("TSK-CLI1", "builder-1", "Ready for review")

        out_json = io.StringIO()
        with contextlib.redirect_stdout(out_json):
            code = cli.main(["--rig-dir", str(self.rig_dir), "telemetry", "metrics", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out_json.getvalue())
        self.assertIn("durations", data)
        self.assertIn("governance_and_topology", data)
        gov = data["governance_and_topology"]
        self.assertEqual(gov["pending_review_tasks"], 1)
        self.assertIn("review_wait_ratio", gov)
        self.assertIn("review_bottleneck_alert", gov)
        self.assertIn("unverified_completion_rate", gov)

        out_md = io.StringIO()
        with contextlib.redirect_stdout(out_md):
            code_md = cli.main(["--rig-dir", str(self.rig_dir), "telemetry", "metrics"])
        self.assertEqual(code_md, 0)
        md_text = out_md.getvalue()
        self.assertIn("Pending Review Tasks (`pending_review_tasks`)", md_text)
        self.assertIn("Review Wait Ratio (`review_wait_ratio`)", md_text)
        self.assertIn("Unverified Completion Rate (`unverified_completion_rate`)", md_text)


class TestCliModelProvenanceAttribution(unittest.TestCase):
    """Hermetic tests for --model, STIGMERIG_MODEL fallback, unset omission, and validation."""

    def setUp(self) -> None:
        prev_model = os.environ.pop("STIGMERIG_MODEL", None)
        self.addCleanup(self._restore_model_env, prev_model)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.rig_dir = self.root / ".stigmerig"
        code, _, err = self._run_cli("init", "--no-icm")
        self.assertEqual(code, 0, f"init failed: {err}")

    @staticmethod
    def _restore_model_env(prev_model: str | None) -> None:
        os.environ.pop("STIGMERIG_MODEL", None)
        if prev_model is not None:
            os.environ["STIGMERIG_MODEL"] = prev_model

    def tearDown(self) -> None:
        gc.collect()
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        full_argv = ["--rig-dir", str(self.rig_dir), *argv]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(full_argv)
        return code, out.getvalue(), err.getvalue()

    def _create_and_claim(self, task_id: str, agent: str = "builder-1") -> None:
        code, _, err = self._run_cli("task", "create", "--id", task_id, "--title", f"Task {task_id}")
        self.assertEqual(code, 0, err)
        code, _, err = self._run_cli("task", "claim", "--id", task_id, "--agent", agent)
        self.assertEqual(code, 0, err)

    def _latest_event_and_audit_hash(
        self, task_id: str, event_type: str, audit_action: str
    ) -> tuple[dict, str, str]:
        db = StigmeRigDB(rig_dir=self.rig_dir)
        with db.get_queue_conn() as qconn:
            ev_row = qconn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND event_type = ? ORDER BY id DESC LIMIT 1",
                (task_id, event_type),
            ).fetchone()
        self.assertIsNotNone(ev_row, f"Missing task_events row for {task_id}/{event_type}")
        raw_payload = ev_row["payload"]
        with db.get_audit_conn() as aconn:
            aud_row = aconn.execute(
                "SELECT payload_hash FROM audit_log WHERE task_id = ? AND action = ? ORDER BY id DESC LIMIT 1",
                (task_id, audit_action),
            ).fetchone()
        self.assertIsNotNone(aud_row, f"Missing audit_log row for {task_id}/{audit_action}")
        return json.loads(raw_payload), raw_payload, aud_row["payload_hash"]

    def test_explicit_model_flag_recorded_on_review_approve_and_done(self) -> None:
        self._create_and_claim("TSK-MOD-1", agent="builder-1")

        code, _, err = self._run_cli(
            "task", "review",
            "--id", "TSK-MOD-1",
            "--agent", "builder-1",
            "--summary", "Review with explicit model",
            "--model", "gemini-4-carbon",
        )
        self.assertEqual(code, 0, err)
        rev_payload, rev_raw, rev_hash = self._latest_event_and_audit_hash(
            "TSK-MOD-1", "review_submitted", "task_review_submitted"
        )
        self.assertEqual(rev_payload.get("model_id"), "gemini-4-carbon")
        self.assertEqual(rev_hash, hashlib.sha256(rev_raw.encode("utf-8")).hexdigest())

        code, _, err = self._run_cli(
            "task", "approve",
            "--id", "TSK-MOD-1",
            "--verifier", "verifier-1",
            "--session-id", "sess-app-1",
            "--model", "claude-sonnet-4-6",
        )
        self.assertEqual(code, 0, err)
        app_payload, app_raw, app_hash = self._latest_event_and_audit_hash(
            "TSK-MOD-1", "completed", "task_completed"
        )
        self.assertEqual(app_payload.get("model_id"), "claude-sonnet-4-6")
        self.assertEqual(app_hash, hashlib.sha256(app_raw.encode("utf-8")).hexdigest())

        self._create_and_claim("TSK-MOD-2", agent="builder-2")
        code, _, err = self._run_cli(
            "task", "done",
            "--id", "TSK-MOD-2",
            "--agent", "builder-2",
            "--verifier", "verifier-2",
            "--session-id", "sess-done-1",
            "--model", "gemini-4-flash",
        )
        self.assertEqual(code, 0, err)
        done_payload, done_raw, done_hash = self._latest_event_and_audit_hash(
            "TSK-MOD-2", "completed", "task_completed"
        )
        self.assertEqual(done_payload.get("model_id"), "gemini-4-flash")
        self.assertEqual(done_hash, hashlib.sha256(done_raw.encode("utf-8")).hexdigest())

    def test_env_fallback_and_explicit_model_override(self) -> None:
        os.environ["STIGMERIG_MODEL"] = "claude-opus-4-6"

        self._create_and_claim("TSK-ENV-1", agent="builder-1")
        code, _, err = self._run_cli(
            "task", "review",
            "--id", "TSK-ENV-1",
            "--agent", "builder-1",
            "--summary", "Review via env fallback",
        )
        self.assertEqual(code, 0, err)
        rev_payload, rev_raw, rev_hash = self._latest_event_and_audit_hash(
            "TSK-ENV-1", "review_submitted", "task_review_submitted"
        )
        self.assertEqual(rev_payload.get("model_id"), "claude-opus-4-6")
        self.assertEqual(rev_hash, hashlib.sha256(rev_raw.encode("utf-8")).hexdigest())

        code, _, err = self._run_cli(
            "task", "approve",
            "--id", "TSK-ENV-1",
            "--verifier", "verifier-1",
            "--session-id", "sess-env-app",
        )
        self.assertEqual(code, 0, err)
        app_payload, app_raw, app_hash = self._latest_event_and_audit_hash(
            "TSK-ENV-1", "completed", "task_completed"
        )
        self.assertEqual(app_payload.get("model_id"), "claude-opus-4-6")
        self.assertEqual(app_hash, hashlib.sha256(app_raw.encode("utf-8")).hexdigest())

        self._create_and_claim("TSK-ENV-2", agent="builder-2")
        code, _, err = self._run_cli(
            "task", "done",
            "--id", "TSK-ENV-2",
            "--agent", "builder-2",
            "--verifier", "verifier-2",
            "--session-id", "sess-env-done",
        )
        self.assertEqual(code, 0, err)
        done_payload, done_raw, done_hash = self._latest_event_and_audit_hash(
            "TSK-ENV-2", "completed", "task_completed"
        )
        self.assertEqual(done_payload.get("model_id"), "claude-opus-4-6")
        self.assertEqual(done_hash, hashlib.sha256(done_raw.encode("utf-8")).hexdigest())

        # Explicit --model overrides STIGMERIG_MODEL across review, approve, and done
        self._create_and_claim("TSK-ENV-3", agent="builder-1")
        code, _, err = self._run_cli(
            "task", "review",
            "--id", "TSK-ENV-3",
            "--agent", "builder-1",
            "--summary", "Review overriding env",
            "--model", "gemini-4-pro",
        )
        self.assertEqual(code, 0, err)
        rev_ovr, _, _ = self._latest_event_and_audit_hash(
            "TSK-ENV-3", "review_submitted", "task_review_submitted"
        )
        self.assertEqual(rev_ovr.get("model_id"), "gemini-4-pro")

        code, _, err = self._run_cli(
            "task", "approve",
            "--id", "TSK-ENV-3",
            "--verifier", "verifier-1",
            "--model", "gpt-5-codex",
        )
        self.assertEqual(code, 0, err)
        app_ovr, _, _ = self._latest_event_and_audit_hash(
            "TSK-ENV-3", "completed", "task_completed"
        )
        self.assertEqual(app_ovr.get("model_id"), "gpt-5-codex")

        self._create_and_claim("TSK-ENV-4", agent="builder-2")
        code, _, err = self._run_cli(
            "task", "done",
            "--id", "TSK-ENV-4",
            "--agent", "builder-2",
            "--verifier", "verifier-2",
            "--model", "claude-haiku-4-5",
        )
        self.assertEqual(code, 0, err)
        done_ovr, _, _ = self._latest_event_and_audit_hash(
            "TSK-ENV-4", "completed", "task_completed"
        )
        self.assertEqual(done_ovr.get("model_id"), "claude-haiku-4-5")

    def test_unset_or_empty_model_omits_model_id_from_payload(self) -> None:
        for env_val, suffix in ((None, "UNSET"), ("", "EMPTY")):
            if env_val is None:
                os.environ.pop("STIGMERIG_MODEL", None)
            else:
                os.environ["STIGMERIG_MODEL"] = env_val

            rev_tid = f"TSK-OMIT-{suffix}-1"
            self._create_and_claim(rev_tid, agent="builder-1")
            code, _, err = self._run_cli(
                "task", "review",
                "--id", rev_tid,
                "--agent", "builder-1",
                "--summary", "Review without model",
            )
            self.assertEqual(code, 0, err)
            rev_payload, rev_raw, rev_hash = self._latest_event_and_audit_hash(
                rev_tid, "review_submitted", "task_review_submitted"
            )
            self.assertNotIn("model_id", rev_payload)
            self.assertEqual(rev_hash, hashlib.sha256(rev_raw.encode("utf-8")).hexdigest())

            code, _, err = self._run_cli(
                "task", "approve",
                "--id", rev_tid,
                "--verifier", "verifier-1",
            )
            self.assertEqual(code, 0, err)
            app_payload, app_raw, app_hash = self._latest_event_and_audit_hash(
                rev_tid, "completed", "task_completed"
            )
            self.assertNotIn("model_id", app_payload)
            self.assertEqual(app_hash, hashlib.sha256(app_raw.encode("utf-8")).hexdigest())

            done_tid = f"TSK-OMIT-{suffix}-2"
            self._create_and_claim(done_tid, agent="builder-2")
            code, _, err = self._run_cli(
                "task", "done",
                "--id", done_tid,
                "--agent", "builder-2",
                "--verifier", "verifier-2",
            )
            self.assertEqual(code, 0, err)
            done_payload, done_raw, done_hash = self._latest_event_and_audit_hash(
                done_tid, "completed", "task_completed"
            )
            self.assertNotIn("model_id", done_payload)
            self.assertEqual(done_hash, hashlib.sha256(done_raw.encode("utf-8")).hexdigest())

    def test_invalid_model_flag_and_env_rejected_with_exit_code_1(self) -> None:
        self._create_and_claim("TSK-BAD-1", agent="builder-1")
        for bad_flag in ("   ", "bad\nmodel", "bad\x00model"):
            code, _, err = self._run_cli(
                "task", "review",
                "--id", "TSK-BAD-1",
                "--agent", "builder-1",
                "--summary", "Invalid model flag",
                "--model", bad_flag,
            )
            self.assertEqual(code, 1)
            self.assertIn("[ERROR]", err)

            code, _, err = self._run_cli(
                "task", "done",
                "--id", "TSK-BAD-1",
                "--agent", "builder-1",
                "--verifier", "verifier-1",
                "--model", bad_flag,
            )
            self.assertEqual(code, 1)
            self.assertIn("[ERROR]", err)

        # Transition TSK-BAD-1 to review cleanly, then test invalid --model and invalid STIGMERIG_MODEL on approve
        code, _, err = self._run_cli(
            "task", "review",
            "--id", "TSK-BAD-1",
            "--agent", "builder-1",
            "--summary", "Ready for invalid approve tests",
        )
        self.assertEqual(code, 0, err)

        for bad_flag in ("   ", "bad\nmodel", "bad\x00model"):
            code, _, err = self._run_cli(
                "task", "approve",
                "--id", "TSK-BAD-1",
                "--verifier", "verifier-1",
                "--model", bad_flag,
            )
            self.assertEqual(code, 1)
            self.assertIn("[ERROR]", err)

        self._create_and_claim("TSK-BAD-2", agent="builder-2")
        for bad_env in ("   ", "bad\x1b[31m"):
            os.environ["STIGMERIG_MODEL"] = bad_env
            code, _, err = self._run_cli(
                "task", "review",
                "--id", "TSK-BAD-2",
                "--agent", "builder-2",
                "--summary", "Invalid env on review",
            )
            self.assertEqual(code, 1)
            self.assertIn("[ERROR]", err)

            code, _, err = self._run_cli(
                "task", "done",
                "--id", "TSK-BAD-2",
                "--agent", "builder-2",
                "--verifier", "verifier-2",
            )
            self.assertEqual(code, 1)
            self.assertIn("[ERROR]", err)

            code, _, err = self._run_cli(
                "task", "approve",
                "--id", "TSK-BAD-1",
                "--verifier", "verifier-1",
            )
            self.assertEqual(code, 1)
            self.assertIn("[ERROR]", err)


if __name__ == "__main__":
    unittest.main()

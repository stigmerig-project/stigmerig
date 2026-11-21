"""Hermetic unit tests for StigmeRig review state, backpressure, and intentional friction gates."""

from __future__ import annotations

import contextlib
import ctypes
import gc
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import stigmerig.cli as cli
from stigmerig.db import (
    DEFAULT_RIG_JSON_TEMPLATE,
    HIGH_FRICTION_PR_RUBRIC,
    StigmeRigDB,
    VERIFY_ONLY_ARCHETYPES,
    _match_segment_glob,
)
from stigmerig.wave import (
    WaveRunner,
    WorktreeManager,
    _detect_test_assertion_deletions,
    _is_test_path,
)


class TestReviewBackpressureAndFriction(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.rig_dir = self.root / ".stigmerig"
        self.db = StigmeRigDB(rig_dir=self.rig_dir, workspace_root=self.root)
        self.db.init_rig(scaffold_icm=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_evidence(self, name: str = "proof.txt", content: str = "verified ok\n") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_rig_json(self, payload: dict) -> None:
        (self.rig_dir / "rig.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def test_default_rig_json_and_non_overwrite(self) -> None:
        rig_json = self.rig_dir / "rig.json"
        self.assertTrue(rig_json.is_file())
        data = json.loads(rig_json.read_text(encoding="utf-8"))
        self.assertEqual(data["max_pending_review"], 3)
        self.assertEqual(
            data["high_friction_tags"],
            ["security", "migration", "infra", "release", "destructive"],
        )
        self.assertEqual(rig_json.read_text(encoding="utf-8"), DEFAULT_RIG_JSON_TEMPLATE)

        custom = {"version": "0.1.0", "icm_enabled": True, "max_pending_review": 1}
        self._write_rig_json(custom)
        self.db.init_rig(scaffold_icm=True)
        self.assertEqual(json.loads(rig_json.read_text(encoding="utf-8")), custom)

    def test_legacy_rig_without_new_keys_disables_gates(self) -> None:
        self._write_rig_json({"version": "0.1.0", "icm_enabled": True})
        for idx in range(1, 5):
            tid = f"TASK-{idx:03d}"
            self.db.create_task(tid, f"Legacy task {idx}", tags="security")
            self.assertTrue(self.db.claim_task(tid, "builder-a"))
            if idx < 4:
                ok, _ = self.db.submit_for_review(tid, "builder-a", f"done {idx}")
                self.assertTrue(ok)

        # 4th task was claimed even with 3 in review (no max_pending_review).
        # Self-approval without evidence also succeeds on security tag when high_friction_tags is absent.
        ok, _ = self.db.complete_task("TASK-004", "builder-a", allow_self_approval=True)
        self.assertTrue(ok)

    def test_submit_for_review_and_board_rendering(self) -> None:
        evidence = self._make_evidence("checks.log", "all tests passed\n")
        self.db.create_task("TASK-010", "Implement feature")
        self.assertNotIn("Pending Review / Verification", self.db.board_path.read_text(encoding="utf-8"))

        self.assertTrue(self.db.claim_task("TASK-010", "builder-1"))
        ok, msg = self.db.submit_for_review(
            "TASK-010",
            "builder-1",
            summary="Implemented feature X",
            verification_cmd="python -m unittest",
            diffstat=" 1 file changed, 10 insertions(+)",
            evidence_paths=[evidence],
            session_id="sess-1",
        )
        self.assertTrue(ok, msg)

        task = self.db.get_task("TASK-010")
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "review")
        self.assertIsNone(task["lease_until"])
        self.assertEqual(task["version"], 2)

        # claim_task must not claim tasks in review status
        self.assertFalse(self.db.claim_task("TASK-010", "verifier-1"))

        board = self.db.board_path.read_text(encoding="utf-8")
        self.assertIn("Pending Review / Verification (1)", board)
        self.assertIn("TASK-010", board)

        with self.db.get_queue_conn() as conn:
            ev = conn.execute(
                "SELECT event_type, agent, payload FROM task_events WHERE task_id = 'TASK-010' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(ev["event_type"], "review_submitted")
        self.assertEqual(ev["agent"], "builder-1")
        payload = json.loads(ev["payload"])
        self.assertEqual(payload["summary"], "Implemented feature X")
        self.assertEqual(payload["verification_cmd"], "python -m unittest")
        self.assertEqual(len(payload["evidence"]), 1)
        self.assertEqual(len(payload["evidence"][0]["sha256"]), 64)

        audit_ok, audit_msg = self.db.verify_audit_chain()
        self.assertTrue(audit_ok, audit_msg)

    def test_atomic_backpressure_and_exemptions(self) -> None:
        self._write_rig_json({"version": "0.1.0", "max_pending_review": 2})

        self.db.create_task("TASK-101", "Working task before cap")
        self.assertTrue(self.db.claim_task("TASK-101", "builder-keep"))

        for tid in ("TASK-102", "TASK-103"):
            self.db.create_task(tid, f"Review {tid}")
            self.assertTrue(self.db.claim_task(tid, "builder-1"))
            ok, _ = self.db.submit_for_review(tid, "builder-1", "ready")
            self.assertTrue(ok)

        # Non-verifier todo claim is blocked at cap (2)
        self.db.create_task("TASK-104", "Blocked builder todo", archetype="diff_synthesizer")
        self.assertFalse(self.db.claim_task("TASK-104", "builder-2"))

        # Working renewal is exempt from backpressure
        self.assertTrue(self.db.claim_task("TASK-101", "builder-keep"))

        # Verifier archetypes are exempt from backpressure
        for idx, arch in enumerate(sorted(VERIFY_ONLY_ARCHETYPES), start=1):
            v_tid = f"TSK-V{idx}"
            self.db.create_task(v_tid, f"Verify {arch}", archetype=arch)
            self.assertTrue(self.db.claim_task(v_tid, f"verifier-{idx}"), f"Expected {arch} to bypass backpressure")

        # ignore_backpressure=True bypasses backpressure
        self.assertTrue(self.db.claim_task("TASK-104", "builder-2", ignore_backpressure=True))

        # Approving one review task drops pending count below cap and unblocks todo claims
        ok, _ = self.db.approve_review("TASK-102", "reviewer-1")
        self.assertTrue(ok)
        self.db.create_task("TASK-105", "Unblocked builder todo")
        self.assertTrue(self.db.claim_task("TASK-105", "builder-2"))

    def test_approve_review_guards_and_blocked_reset(self) -> None:
        self.db.create_task("TASK-200", "Parent task")
        self.db.create_task("TASK-201", "Child task", parent_id="TASK-200")
        self.assertTrue(self.db.claim_task("TASK-200", "builder-a"))
        ok, _ = self.db.submit_for_review("TASK-200", "builder-a", "parent ready")
        self.assertTrue(ok)

        # Open child guard
        ok, msg = self.db.approve_review("TASK-200", "verifier-z")
        self.assertFalse(ok)
        self.assertIn("open child tasks remain", msg)

        # Close child
        self.assertTrue(self.db.claim_task("TASK-201", "builder-b"))
        ok, _ = self.db.complete_task("TASK-201", "builder-b", verifier="verifier-z")
        self.assertTrue(ok)

        # Self-approval against review_submitted implementer is rejected
        with self.assertRaises(PermissionError):
            self.db.approve_review("TASK-200", "builder-a")
        with self.assertRaises(PermissionError):
            self.db.approve_review("TASK-200", "builder-a/reviewer")

        # Independent verifier succeeds
        ok, msg = self.db.approve_review("TASK-200", "verifier-z", session_id="rev-1")
        self.assertTrue(ok, msg)
        self.assertEqual(self.db.get_task("TASK-200")["status"], "done")
        self.assertNotIn("Pending Review / Verification", self.db.board_path.read_text(encoding="utf-8"))

        # Pre-review implementer history across handoff & blocked reset
        self.db.create_task("TASK-202", "Multi-implementer task")
        self.assertTrue(self.db.claim_task("TASK-202", "early-dev"))
        self.db.fail_task("TASK-202", "early-dev", "needs redesign")
        # Re-open to todo after blocked
        with self.db.get_queue_conn() as conn:
            conn.execute("UPDATE tasks SET status = 'todo', claimed_by = NULL WHERE id = 'TASK-202'")
            conn.commit()
        self.assertTrue(self.db.claim_task("TASK-202", "dev-1"))
        # Simulate handoff before review (e.g. expired lease / reclaim by dev-2)
        with self.db.get_queue_conn() as conn:
            conn.execute("UPDATE tasks SET lease_until = '2000-01-01T00:00:00Z' WHERE id = 'TASK-202'")
            conn.commit()
        self.assertTrue(self.db.claim_task("TASK-202", "dev-2"))
        ok, _ = self.db.submit_for_review("TASK-202", "dev-2", "implemented")
        self.assertTrue(ok)

        # dev-1 (post-blocked implementer) must be rejected
        with self.assertRaises(PermissionError):
            self.db.approve_review("TASK-202", "dev-1")
        # dev-2 must be rejected
        with self.assertRaises(PermissionError):
            self.db.approve_review("TASK-202", "dev-2")
        # early-dev (before latest blocked reset) is allowed to verify post-blocked work
        ok, _ = self.db.approve_review("TASK-202", "early-dev")
        self.assertTrue(ok)

    def test_intentional_friction_tags_and_segment_globs(self) -> None:
        self.assertTrue(_match_segment_glob(".github/workflows/ci.yml", ".github/workflows/*.yml"))
        self.assertFalse(_match_segment_glob(".github/workflows/sub/ci.yml", ".github/workflows/*.yml"))
        self.assertTrue(_match_segment_glob(".github/workflows/sub/ci.yml", ".github/workflows/**/*.yml"))
        self.assertTrue(_match_segment_glob("infra/main.tf", "*.tf"))
        self.assertTrue(_match_segment_glob("auth/login.py", "auth/**"))
        self.assertFalse(_match_segment_glob("other/auth/login.py", "auth/**"))

        self._write_rig_json({
            "version": "0.1.0",
            "max_pending_review": 3,
            "high_friction_tags": ["security"],
            "high_friction_globs": [".github/workflows/*.yml", "auth/**"],
        })
        evidence = self._make_evidence("scan.json", '{"ok": true}\n')

        # 1. Tag friction in complete_task: forbids allow_self_approval and requires evidence
        self.db.create_task("TASK-301", "Security fix", tags="security")
        self.assertTrue(self.db.claim_task("TASK-301", "builder-1"))
        with self.assertRaises(PermissionError):
            self.db.complete_task("TASK-301", "builder-1", allow_self_approval=True, evidence_paths=[evidence])
        with self.assertRaises(ValueError):
            self.db.complete_task("TASK-301", "builder-1", verifier="verifier-1", evidence_paths=None)
        ok, _ = self.db.complete_task("TASK-301", "builder-1", verifier="verifier-1", evidence_paths=[evidence])
        self.assertTrue(ok)

        # 2. Glob friction in complete_task via touched_paths
        self.db.create_task("TASK-302", "CI workflow tweak")
        self.assertTrue(self.db.claim_task("TASK-302", "builder-1"))
        # Non-matching nested path does not trigger friction
        with self.assertRaises(PermissionError):
            self.db.complete_task(
                "TASK-302",
                "builder-1",
                allow_self_approval=True,
                touched_paths=[".github/workflows/ci.yml"],
                evidence_paths=[evidence],
            )
        with self.assertRaises(ValueError):
            self.db.complete_task(
                "TASK-302",
                "builder-1",
                verifier="verifier-1",
                touched_paths=["auth/session.py"],
            )
        ok, _ = self.db.complete_task(
            "TASK-302",
            "builder-1",
            verifier="verifier-1",
            touched_paths=["auth/session.py"],
            evidence_paths=[evidence],
        )
        self.assertTrue(ok)

        # 3. approve_review inherits evidence from submit_for_review on friction tasks
        self.db.create_task("TASK-303", "Auth review without initial evidence", tags="security")
        self.assertTrue(self.db.claim_task("TASK-303", "builder-1"))
        ok, _ = self.db.submit_for_review("TASK-303", "builder-1", "no evidence yet")
        self.assertTrue(ok)
        with self.assertRaises(ValueError):
            self.db.approve_review("TASK-303", "verifier-1")
        ok, _ = self.db.approve_review("TASK-303", "verifier-1", evidence_paths=[evidence])
        self.assertTrue(ok)

        self.db.create_task("TASK-304", "Auth review with inherited evidence", tags="security")
        self.assertTrue(self.db.claim_task("TASK-304", "builder-1"))
        ok, _ = self.db.submit_for_review("TASK-304", "builder-1", "with evidence", evidence_paths=[evidence])
        self.assertTrue(ok)
        ok, _ = self.db.approve_review("TASK-304", "verifier-1")
        self.assertTrue(ok)

    def test_short_or_alias_ancestor_evidence_path_accepted_and_dotdot_escape_blocked(self) -> None:
        ws = Path(self._tmp.name) / "long_runner_workspace_root_12345"
        ws.mkdir()
        (ws / "checks.log").write_text("checks passed\n", encoding="utf-8")
        (ws / "sub").mkdir()
        (ws / "sub" / "nested.log").write_text("nested passed\n", encoding="utf-8")
        (Path(self._tmp.name) / "outside.log").write_text("outside\n", encoding="utf-8")

        db = StigmeRigDB(rig_dir=ws / ".stigmerig", workspace_root=ws)
        db.init_rig(scaffold_icm=True)

        if os.name == "nt":
            buf = ctypes.create_unicode_buffer(512)
            n = ctypes.windll.kernel32.GetShortPathNameW(str(ws), buf, 512)
            short = buf.value if n and buf.value else ""
            alias_ws = Path(short) if short else Path(str(ws).swapcase())
            if short and short != str(ws):
                self.assertNotEqual(alias_ws, ws.resolve())
        else:
            alias_ws = ws

        db.create_task("TASK-083", "Short path review")
        self.assertTrue(db.claim_task("TASK-083", "builder-1"))
        ok, msg = db.submit_for_review(
            "TASK-083",
            "builder-1",
            "ok",
            evidence_paths=[alias_ws / "checks.log", alias_ws / "sub" / ".." / "sub" / "nested.log"],
        )
        self.assertTrue(ok, msg)

        self.assertEqual(
            cli.main(["--rig-dir", str(ws / ".stigmerig"), "task", "create", "--id", "TASK-084", "--title", "Short path CLI"]),
            0,
        )
        self.assertEqual(
            cli.main(["--rig-dir", str(ws / ".stigmerig"), "task", "claim", "--id", "TASK-084", "--agent", "builder-1"]),
            0,
        )
        self.assertEqual(
            cli.main([
                "--rig-dir",
                str(ws / ".stigmerig"),
                "task",
                "done",
                "--id",
                "TASK-084",
                "--agent",
                "builder-1",
                "--verifier",
                "verifier-2",
                "--evidence",
                str(alias_ws / "checks.log"),
            ]),
            0,
        )

        with self.assertRaises(ValueError):
            db._validate_and_hash_evidence([alias_ws / ".." / "outside.log"])
        with self.assertRaises(ValueError):
            db._validate_and_hash_evidence([alias_ws / ".." / ws.name / "checks.log"])

        args = cli.build_parser().parse_args([
            "--rig-dir",
            str(ws / ".stigmerig"),
            "task",
            "done",
            "--id",
            "TASK-084",
            "--agent",
            "builder-1",
            "--verifier",
            "verifier-2",
            "--evidence",
            str(alias_ws / ".." / ws.name / "checks.log"),
        ])
        with self.assertRaises(ValueError):
            cli._validate_cli_args(args)

    def test_high_friction_rubric_and_cli_json(self) -> None:
        self._write_rig_json({
            "version": "0.1.0",
            "max_pending_review": 3,
            "high_friction_tags": ["security", "migration"],
            "high_friction_globs": ["migrations/**"],
        })
        self.db.create_task("TASK-901", "Harden auth tokens", tags="security")
        self.db.create_task("TASK-902", "Update docs wording", tags="docs")
        self.db.create_task("TASK-903", "Apply schema v2", tags="migration")

        hf_task = self.db.get_task("TASK-901")
        normal_task = self.db.get_task("TASK-902")
        self.assertIsNotNone(hf_task)
        self.assertIsNotNone(normal_task)

        self.assertEqual(len(HIGH_FRICTION_PR_RUBRIC), 4)
        self.assertEqual(self.db.get_high_friction_rubric(hf_task), HIGH_FRICTION_PR_RUBRIC)
        self.assertEqual(self.db.get_high_friction_rubric(normal_task), ())
        self.assertEqual(
            self.db.get_high_friction_rubric(normal_task, touched_paths=["migrations/001.sql"]),
            HIGH_FRICTION_PR_RUBRIC,
        )
        self.assertEqual(self.db.get_high_friction_rubric("invalid"), ())  # type: ignore[arg-type]

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--rig-dir", str(self.rig_dir), "task", "get", "--id", "TASK-901"])
        self.assertEqual(rc, 0)
        got_hf = json.loads(buf.getvalue())
        self.assertEqual(got_hf["high_friction_rubric"], list(HIGH_FRICTION_PR_RUBRIC))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--rig-dir", str(self.rig_dir), "task", "get", "--id", "TASK-902"])
        self.assertEqual(rc, 0)
        got_norm = json.loads(buf.getvalue())
        self.assertNotIn("high_friction_rubric", got_norm)

        # Plain-text claim output remains unchanged without --json
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--rig-dir", str(self.rig_dir), "task", "claim", "--id", "TASK-901", "--agent", "builder-a"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "Task TASK-901 claimed by builder-a")

        # --json omits rubric on ordinary tasks and includes it on high-friction tasks
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--rig-dir", str(self.rig_dir), "task", "claim", "--id", "TASK-902", "--agent", "builder-a", "--json"])
        self.assertEqual(rc, 0)
        claim_norm = json.loads(buf.getvalue())
        self.assertEqual(claim_norm, {"ok": True, "task_id": "TASK-902", "claimed_by": "builder-a"})

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--rig-dir", str(self.rig_dir), "task", "claim", "--id", "TASK-903", "--agent", "builder-a", "--json"])
        self.assertEqual(rc, 0)
        claim_hf = json.loads(buf.getvalue())
        self.assertEqual(claim_hf["ok"], True)
        self.assertEqual(claim_hf["task_id"], "TASK-903")
        self.assertEqual(claim_hf["claimed_by"], "builder-a")
        self.assertEqual(claim_hf["high_friction_rubric"], list(HIGH_FRICTION_PR_RUBRIC))

        # Legacy rig without friction keys returns empty rubric
        self._write_rig_json({"version": "0.1.0", "icm_enabled": True})
        self.assertEqual(self.db.get_high_friction_rubric(hf_task), ())

    def test_detect_test_assertion_deletions_and_wave_warning(self) -> None:
        self.assertTrue(_is_test_path("tests/test_auth.py"))
        self.assertTrue(_is_test_path("pkg/__tests__/auth.spec.ts"))
        self.assertTrue(_is_test_path("unit_test.go"))
        self.assertFalse(_is_test_path("src/auth.py"))

        sample_diff = "\n".join([
            "diff --git a/tests/test_sample.py b/tests/test_sample.py",
            "--- a/tests/test_sample.py",
            "+++ b/tests/test_sample.py",
            "@@ -10,3 +10,2 @@",
            "-    # assert commented out should be ignored",
            "-    assert result == 42",
            "diff --git a/tests/test_clean.py b/tests/test_clean.py",
            "--- a/tests/test_clean.py",
            "+++ b/tests/test_clean.py",
            "@@ -1,1 +1,1 @@",
            "-# old comment only",
            "diff --git a/src/app.py b/src/app.py",
            "--- a/src/app.py",
            "+++ b/src/app.py",
            "@@ -5,1 +5,1 @@",
            "-assert invariant_ok()",
        ])
        self.assertEqual(_detect_test_assertion_deletions(sample_diff), ["tests/test_sample.py"])

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.email", "runner@local.test"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(repo), check=True)
            (repo / "tests").mkdir()
            (repo / "tests" / "test_auth.py").write_text(
                "def test_ok():\n    assert 1 == 1\n    assert 2 == 2\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "tests/test_auth.py"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init tests"], cwd=str(repo), check=True, capture_output=True)

            db = StigmeRigDB(rig_dir=repo / ".stigmerig", workspace_root=repo)
            db.init_rig(scaffold_icm=False)
            (repo / ".stigmerig" / "rig.json").write_text(
                json.dumps({
                    "version": "0.1.0",
                    "max_pending_review": 3,
                    "high_friction_tags": ["security", "infra"],
                }, indent=2),
                encoding="utf-8",
            )
            wt_mgr = WorktreeManager(repo_root=repo)
            runner = WaveRunner(db=db, worktree_mgr=wt_mgr)
            evidence = repo / "proof.log"
            evidence.write_text("targeted check ok\n", encoding="utf-8")

            try:
                db.create_task("TASK-904", "Edit code with deleted test assertion", tags="security")

                def non_tester_edit(exec_dir: Path) -> bool:
                    (exec_dir / "tests" / "test_auth.py").write_text(
                        "def test_ok():\n    assert 2 == 2\n",
                        encoding="utf-8",
                    )
                    return True

                err_buf = io.StringIO()
                with contextlib.redirect_stderr(err_buf):
                    ok, msg = runner.run_task_in_worktree(
                        task_id="TASK-904",
                        agent="builder-1",
                        action_fn=non_tester_edit,
                        verifier="verifier-1",
                        archetype="diff_synthesizer",
                        allow_self_approval=False,
                        evidence_paths=[evidence],
                    )
                self.assertTrue(ok, msg)
                self.assertIn("tests/test_auth.py", err_buf.getvalue())

                db.create_task("TASK-905", "Unit tester edits test assertions intentionally", tags="security")

                def unit_tester_edit(exec_dir: Path) -> bool:
                    (exec_dir / "tests" / "test_auth.py").write_text(
                        "def test_ok():\n    return None\n",
                        encoding="utf-8",
                    )
                    return True

                err_buf2 = io.StringIO()
                with contextlib.redirect_stderr(err_buf2):
                    ok2, msg2 = runner.run_task_in_worktree(
                        task_id="TASK-905",
                        agent="tester-1",
                        action_fn=unit_tester_edit,
                        verifier="verifier-1",
                        archetype="unit_tester",
                        allow_self_approval=False,
                        evidence_paths=[evidence],
                    )
                self.assertTrue(ok2, msg2)
                self.assertNotIn("Warning: high-friction task 'TASK-905'", err_buf2.getvalue())

                with db.get_audit_conn() as aconn:
                    rows = aconn.execute(
                        "SELECT task_id, action, verification_status FROM audit_log WHERE action = 'scope_hygiene_warn'"
                    ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["task_id"], "TASK-904")
                self.assertEqual(rows[0]["verification_status"], "warn")
                audit_ok, audit_msg = db.verify_audit_chain()
                self.assertTrue(audit_ok, audit_msg)
            finally:
                for tid in ("TASK-904", "TASK-905"):
                    try:
                        wt_mgr.remove_worktree(tid, delete_branch=True)
                    except Exception:
                        pass
                subprocess.run(["git", "worktree", "prune"], cwd=str(repo), capture_output=True)
                gc.collect()


if __name__ == "__main__":
    unittest.main()

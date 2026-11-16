"""Hermetic polyglot verification suite for StigmeRig traps CLI and lock serialization."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

import stigmerig.cli as cli
from stigmerig.db import StigmeRigDB
from stigmerig.traps import (
    collect_scannable_files,
    is_target_applicable,
    seed_starter_traps,
    validate_traps_cli_args,
    verify_trap_fixtures,
)


def _create_dir_link(link_path: Path, target_dir: Path) -> bool:
    """Create a directory symlink or Windows NTFS junction; return False if unsupported."""
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        link_path.symlink_to(target_dir, target_is_directory=True)
        return True
    except OSError:
        try:
            import _winapi

            _winapi.CreateJunction(str(target_dir), str(link_path))
            return True
        except (ImportError, OSError):
            return False


_MODULE_TMP: tempfile.TemporaryDirectory[str] | None = None


def setUpModule() -> None:
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()


def tearDownModule() -> None:
    global _MODULE_TMP
    if _MODULE_TMP is not None:
        _MODULE_TMP.cleanup()
        _MODULE_TMP = None


class _HermeticTempTestCase(unittest.TestCase):
    """Base test case managing a hermetic TemporaryDirectory and isolated CLI execution."""

    def setUp(self) -> None:
        global _MODULE_TMP
        if _MODULE_TMP is None:
            self._own_tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._own_tmp.cleanup)
            base_dir = Path(self._own_tmp.name)
        else:
            base_dir = Path(_MODULE_TMP.name)
        self.tmp_dir = base_dir / f"{type(self).__name__}_{self._testMethodName}"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def run_cli(
        self,
        argv: list[str],
        cwd: Path | None = None,
    ) -> tuple[int, str, str]:
        """Invoke stigmerig.cli.main in-process with isolated cwd, env, and stdio buffers."""
        prev_cwd = Path.cwd()
        prev_env_root = os.environ.pop("STIGMERIG_ROOT", None)
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            if cwd is not None:
                os.chdir(cwd)
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                rc = cli.main(argv)
            return rc, out_buf.getvalue(), err_buf.getvalue()
        finally:
            os.chdir(prev_cwd)
            if prev_env_root is not None:
                os.environ["STIGMERIG_ROOT"] = prev_env_root
            else:
                os.environ.pop("STIGMERIG_ROOT", None)


class TestPolyglotDiscoveryAndPruning(_HermeticTempTestCase):
    """Verify polyglot source discovery and build/VCS/rig directory pruning."""

    def test_polyglot_extensions_discovered_and_build_dirs_pruned(self) -> None:
        repo = self.tmp_dir / "polyglot_repo"
        expected_sources = {
            "cmd/daemon/main.go": "package main\nfunc main() {}\n",
            "crates/core/lib.rs": "pub fn ready() -> bool { true }\n",
            "db/migrations/001_schema.sql": "CREATE TABLE runs (id INTEGER PRIMARY KEY);\n",
            "infra/main.tf": 'variable "environment" { type = string }\n',
            "web/src/App.tsx": 'export const App = () => "<main />";\n',
            "web/src/client.ts": "export const apiVersion = 2;\n",
        }
        for rel_path, content in expected_sources.items():
            full_path = repo / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

        pruned_files = (
            "node_modules/pkg/index.ts",
            "web/node_modules/ui/Button.tsx",
            "target/debug/build_gen.rs",
            "crates/core/target/release/out.rs",
            "vendor/example.com/mod/dep.go",
            "dist/bundle.ts",
            ".git/hooks/pre_commit.py",
            ".stigmerig/internal_helper.py",
        )
        for rel_pruned in pruned_files:
            pruned_path = repo / rel_pruned
            pruned_path.parent.mkdir(parents=True, exist_ok=True)
            pruned_path.write_text("// ignored build or metadata artifact\n", encoding="utf-8")

        (repo / "traps.json").write_text('{"version": "1.0.0", "traps": []}\n', encoding="utf-8")
        (repo / "Trap_Invariants.json").write_text('{"version": "1.0.0", "traps": []}\n', encoding="utf-8")
        (repo / "web" / "src" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        discovered = collect_scannable_files(repo, repo_root=repo)
        discovered_rel = [p.resolve().relative_to(repo.resolve()).as_posix() for p in discovered]
        self.assertEqual(discovered_rel, sorted(expected_sources.keys()))

        for rel_keep in expected_sources:
            self.assertTrue(
                is_target_applicable(repo / rel_keep, ["**"], repo_root=repo),
                f"Expected {rel_keep} to be scannable",
            )
        for rel_skip in pruned_files:
            self.assertFalse(
                is_target_applicable(repo / rel_skip, ["**"], repo_root=repo),
                f"Expected {rel_skip} to be pruned",
            )


class TestStandaloneUninitializedCliScanAndVerify(_HermeticTempTestCase):
    """Verify standalone CLI seed, verify, and scan without stigmerig init (no queue.db or audit.db)."""

    def test_standalone_cli_seed_verify_and_clean_polyglot_scan_without_init(self) -> None:
        repo = self.tmp_dir / "uninitialized_repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "service.ts").write_text(
            "export async function fetchCount(): Promise<number> {\n  return 42;\n}\n",
            encoding="utf-8",
        )
        (repo / "src" / "Panel.tsx").write_text(
            'export function Panel(): string {\n  return "<section />";\n}\n',
            encoding="utf-8",
        )
        (repo / "src" / "lib.rs").write_text(
            "pub fn write_frame(res: Result<(), String>) -> Result<(), String> {\n    res\n}\n",
            encoding="utf-8",
        )
        (repo / "src" / "main.go").write_text(
            'package main\nimport "fmt"\nfunc check(err error) error {\n\tif err != nil {\n\t\treturn fmt.Errorf("io: %w", err)\n\t}\n\treturn nil\n}\n',
            encoding="utf-8",
        )
        (repo / "src" / "worker.py").write_text(
            "def parse_port(raw: str) -> int:\n    try:\n        return int(raw)\n    except ValueError:\n        return 8080\n",
            encoding="utf-8",
        )
        (repo / "src" / "schema.sql").write_text(
            "SELECT id FROM accounts WHERE active = 1;\n",
            encoding="utf-8",
        )
        (repo / "src" / "infra.tf").write_text(
            'resource "null_resource" "noop" {}\n',
            encoding="utf-8",
        )

        # Offending snippets inside pruned build directories must be ignored during CLI scan.
        (repo / "node_modules" / "dep").mkdir(parents=True)
        (repo / "node_modules" / "dep" / "bad.ts").write_text(
            "// " + "@ts-ignore\nconst bad = 1;\n",
            encoding="utf-8",
        )
        (repo / "target" / "debug").mkdir(parents=True)
        (repo / "target" / "debug" / "bad.rs").write_text(
            "#!" + "[allow(warnings)]\npub fn skip() {}\n",
            encoding="utf-8",
        )

        # 1. Scan before traps.json exists succeeds cleanly with 0 active traps.
        rc_pre, out_pre, err_pre = self.run_cli(["traps", "scan", ".", "--json"], cwd=repo)
        self.assertEqual(rc_pre, 0, err_pre)
        pre_report = json.loads(out_pre)
        self.assertTrue(pre_report["ok"])
        self.assertEqual(pre_report["active_traps"], 0)
        self.assertEqual(pre_report["scanned_files"], 7)

        # 2. Seed all starter packs without running `stigmerig init`.
        rc_seed, out_seed, err_seed = self.run_cli(["traps", "seed", "--pack", "all", "--json"], cwd=repo)
        self.assertEqual(rc_seed, 0, err_seed)
        seed_report = json.loads(out_seed)
        self.assertTrue(seed_report["ok"])
        self.assertEqual(seed_report["seeded_count"], 8)

        # 3. Verify fixtures via CLI without queue.db or audit.db.
        rc_ver, out_ver, err_ver = self.run_cli(["traps", "verify", "--json"], cwd=repo)
        self.assertEqual(rc_ver, 0, err_ver)
        ver_report = json.loads(out_ver)
        self.assertTrue(ver_report["ok"])
        self.assertEqual(ver_report["verified_traps"], 8)
        self.assertEqual(ver_report["errors"], [])

        # 4. Scan clean polyglot repository and verify zero violations.
        rc_scan_txt, out_scan_txt, err_scan_txt = self.run_cli(["traps", "scan", "."], cwd=repo)
        self.assertEqual(rc_scan_txt, 0, err_scan_txt)
        self.assertIn("[PASS] Scanned 7 file(s) against 8 active trap(s): zero violations.", out_scan_txt)

        # Confirm zero SQLite databases were created.
        self.assertTrue((repo / ".stigmerig" / "traps.json").is_file())
        self.assertFalse((repo / ".stigmerig" / "queue.db").exists())
        self.assertFalse((repo / ".stigmerig" / "audit.db").exists())

    def test_standalone_cli_scan_flags_offending_polyglot_files_with_prescriptive_remediations(self) -> None:
        repo = self.tmp_dir / "offending_repo"
        src = repo / "src"
        src.mkdir(parents=True)
        reg_path = repo / "standalone_traps.json"

        rc_seed, _, err_seed = self.run_cli(
            ["traps", "seed", "--pack", "all", "--registry", str(reg_path)],
            cwd=repo,
        )
        self.assertEqual(rc_seed, 0, err_seed)

        (src / "bad.ts").write_text(
            "// " + "@ts-ignore\nconst x: number = raw;\npromise.catch" + "(() => {});\n",
            encoding="utf-8",
        )
        (src / "bad.rs").write_text(
            "#!" + "[allow(warnings)]\npub fn run() {\n    if let " + "Err(_e) = flush_io() {}\n}\n",
            encoding="utf-8",
        )
        (src / "bad.go").write_text(
            "package main\nfunc execStep() {\n\tif " + "err != nil {\n\t}\n}\n",
            encoding="utf-8",
        )
        (src / "bad.py").write_text(
            "def call_op():\n    try:\n        work()  # "
            + "type: ignore\n    ex"
            + "cept Exception:\n        pass\n",
            encoding="utf-8",
        )

        rc_txt, out_txt, err_txt = self.run_cli(
            ["traps", "scan", str(src), "--registry", str(reg_path)],
            cwd=repo,
        )
        self.assertEqual(rc_txt, 1)
        self.assertIn("[VIOLATION]", out_txt)
        self.assertIn("Remediation:", out_txt)
        self.assertIn("@typescript-eslint", out_txt)
        self.assertIn("clippy", out_txt)
        self.assertIn("errcheck", out_txt)
        self.assertIn("ruff S110", out_txt)
        self.assertIn("[ERROR] Trap scan found", err_txt)

        rc_json, out_json, _ = self.run_cli(
            ["traps", "scan", str(src), "--registry", str(reg_path), "--json"],
            cwd=repo,
        )
        self.assertEqual(rc_json, 1)
        report = json.loads(out_json)
        self.assertFalse(report["ok"])
        self.assertEqual(report["scanned_files"], 4)
        hit_ids = {v["trap_id"] for v in report["violations"]}
        self.assertEqual(
            hit_ids,
            {
                "TRAP-TS-001",
                "TRAP-TS-002",
                "TRAP-RS-001",
                "TRAP-RS-002",
                "TRAP-GO-001",
                "TRAP-PY-001",
                "TRAP-PY-002",
            },
        )
        for violation in report["violations"]:
            self.assertGreaterEqual(len(violation["remediation"]), 20)

        self.assertFalse((repo / ".stigmerig" / "queue.db").exists())
        self.assertFalse((repo / ".stigmerig" / "audit.db").exists())


class TestStarterPacksAndMultilineCliFixtures(_HermeticTempTestCase):
    """Verify all 5 starter packs, multiline CLI fixtures, stub rejections, and trace-preserving promotion."""

    def test_all_five_starter_packs_and_combined_seed_pass_verify(self) -> None:
        expected_counts = {
            "generic": 1,
            "python": 2,
            "typescript": 2,
            "rust": 2,
            "go": 1,
        }
        for pack_name, expected_len in expected_counts.items():
            reg_path = self.tmp_dir / f"pack_{pack_name}.json"
            seeded = seed_starter_traps(reg_path, pack=pack_name, status="promoted")
            self.assertEqual(len(seeded), expected_len)

            ok_fn, errors_fn, count_fn = verify_trap_fixtures(
                reg_path,
                include_candidates=True,
                require_prescriptive_on_promoted=True,
            )
            self.assertTrue(ok_fn, f"Pack {pack_name} failed verify_trap_fixtures: {errors_fn}")
            self.assertEqual(count_fn, expected_len)

        rig_dir = self.tmp_dir / ".stigmerig"
        all_reg = self.tmp_dir / "pack_all.json"
        rc_seed, out_seed, err_seed = self.run_cli(
            [
                "--rig-dir",
                str(rig_dir),
                "traps",
                "seed",
                "--pack",
                "all",
                "--registry",
                str(all_reg),
                "--json",
            ],
        )
        self.assertEqual(rc_seed, 0, err_seed)
        self.assertEqual(json.loads(out_seed)["seeded_count"], 8)

        ok_all, errors_all, count_all = verify_trap_fixtures(
            all_reg,
            include_candidates=True,
            require_prescriptive_on_promoted=True,
        )
        self.assertTrue(ok_all, errors_all)
        self.assertEqual(count_all, 8)

        rc_ver, _, err_ver = self.run_cli(
            [
                "--rig-dir",
                str(rig_dir),
                "traps",
                "verify",
                "--registry",
                str(all_reg),
                "--include-candidates",
            ],
        )
        self.assertEqual(rc_ver, 0, err_ver)

    def test_multiline_cli_register_and_promote_and_stub_remediation_rejections(self) -> None:
        rig_dir = self.tmp_dir / ".stigmerig"
        reg_path = self.tmp_dir / "multiline_traps.json"
        pos_ml = "def sync_data():\n    conn = open_raw_socket(\n        host\n    )\n"
        neg_ml = "def sync_data():\n    conn = open_raw_socket(\n        host,\n        timeout=5.0,\n    )\n"
        pattern_ml = r"^([ \t]*)conn\s*=\s*open_raw_socket\(\s*\r?\n\1[ \t]+host\s*\r?\n\1\)"

        # Register candidate with embedded \n multiline fixtures via CLI (cli.py:L42 allow_ws).
        rc_reg, out_reg, err_reg = self.run_cli(
            [
                "--rig-dir",
                str(rig_dir),
                "traps",
                "register",
                "--registry",
                str(reg_path),
                "--id",
                "TRAP-ML-001",
                "--domain",
                "network-safety",
                "--title",
                "Multiline Socket Call Without Timeout",
                "--pattern",
                pattern_ml,
                "--positive-fixture",
                pos_ml,
                "--negative-fixture",
                neg_ml,
                "--match-mode",
                "multiline",
                "--status",
                "candidate",
                "--json",
            ],
        )
        self.assertEqual(rc_reg, 0, err_reg)
        reg_entry = json.loads(out_reg)
        self.assertEqual(reg_entry["trap_id"], "TRAP-ML-001")
        self.assertIn("\n", reg_entry["fixtures"]["positive"])
        self.assertIn("\n", reg_entry["fixtures"]["negative"])

        # Reject < 20-char, "N/A", and "ATOM-01" stub remediations on promote and register(--status promoted).
        for bad_rem in ("Too short fix.", "N/A"):
            rc_bad_prom, _, err_bad_prom = self.run_cli(
                [
                    "--rig-dir",
                    str(rig_dir),
                    "traps",
                    "promote",
                    "--registry",
                    str(reg_path),
                    "--id",
                    "TRAP-ML-001",
                    "--remediation",
                    bad_rem,
                ],
            )
            self.assertEqual(rc_bad_prom, 1)
            self.assertIn("[ERROR]", err_bad_prom)

        rc_bad_reg, _, err_bad_reg = self.run_cli(
            [
                "--rig-dir",
                str(rig_dir),
                "traps",
                "register",
                "--registry",
                str(reg_path),
                "--id",
                "TRAP-ML-BAD",
                "--title",
                "Stub Remediation Candidate",
                "--pattern",
                r"\bforbidden_stub_api\(",
                "--positive-fixture",
                "forbidden_stub_api(1)",
                "--negative-fixture",
                "allowed_api(1)",
                "--status",
                "promoted",
                "--remediation",
                "ATOM-01",
            ],
        )
        self.assertEqual(rc_bad_reg, 1)
        self.assertIn("[ERROR]", err_bad_reg)

        # Promote with updated multiline fixtures and prescriptive remediation.
        rc_prom, out_prom, err_prom = self.run_cli(
            [
                "--rig-dir",
                str(rig_dir),
                "traps",
                "promote",
                "--registry",
                str(reg_path),
                "--id",
                "TRAP-ML-001",
                "--positive-fixture",
                pos_ml,
                "--negative-fixture",
                neg_ml,
                "--match-mode",
                "multiline",
                "--remediation",
                "Pass an explicit timeout= float argument to open_raw_socket() on multiline invocations.",
                "--json",
            ],
        )
        self.assertEqual(rc_prom, 0, err_prom)
        self.assertTrue(json.loads(out_prom)["ok"])

        ok_ver, errs_ver, count_ver = verify_trap_fixtures(reg_path)
        self.assertTrue(ok_ver, errs_ver)
        self.assertEqual(count_ver, 1)

    def test_tier_a_harvested_blocker_cli_promote_requires_pattern_and_preserves_traces(self) -> None:
        rig_dir = self.tmp_dir / ".stigmerig"
        reg_path = self.tmp_dir / "traps.json"
        original_traces = [
            "Task JOB-010 (worker-alpha)",
            "Task JOB-011 (worker-beta)",
            "Task JOB-012 (worker-gamma)",
        ]
        harvested = {
            "version": "1.0.0",
            "traps": [
                {
                    "id": "TRAP-001",
                    "title": "Unbounded subprocess wait hung worker lease",
                    "reason": "Unbounded subprocess wait hung worker lease",
                    "trigger_pattern": "unbounded subprocess wait hung worker lease",
                    "status": "candidate",
                    "occurrences": 3,
                    "promotion_candidate": True,
                    "first_seen_at": "2026-11-15T01:00:00Z",
                    "last_seen_at": "2026-11-15T02:00:00Z",
                    "source_traces": list(original_traces),
                }
            ],
        }
        reg_path.write_text(json.dumps(harvested, indent=2) + "\n", encoding="utf-8")

        # Missing --pattern on harvested blocker candidate must fail with exit 1.
        rc_no_pat, _, err_no_pat = self.run_cli(
            [
                "--rig-dir",
                str(rig_dir),
                "traps",
                "promote",
                "--registry",
                str(reg_path),
                "--id",
                "TRAP-001",
                "--positive-fixture",
                "proc.wait()",
                "--negative-fixture",
                "proc.wait(timeout=10)",
                "--remediation",
                "Pass an explicit timeout= parameter to proc.wait() so subprocesses cannot hang leases.",
            ],
        )
        self.assertEqual(rc_no_pat, 1)
        self.assertIn("Cannot promote harvested blocker TRAP-001", err_no_pat)

        # Full promotion with --pattern, multiline fixtures, and prescriptive remediation exits 0 and preserves lineage.
        rc_ok, _, err_ok = self.run_cli(
            [
                "--rig-dir",
                str(rig_dir),
                "traps",
                "promote",
                "--registry",
                str(reg_path),
                "--id",
                "TRAP-001",
                "--pattern",
                r"\bproc\.wait\(\s*\)",
                "--positive-fixture",
                "proc = spawn_job()\nproc.wait()\n",
                "--negative-fixture",
                "proc = spawn_job()\nproc.wait(timeout=10)\n",
                "--match-mode",
                "multiline",
                "--remediation",
                "Pass an explicit timeout= parameter to proc.wait() so subprocesses cannot hang leases.",
                "--json",
            ],
        )
        self.assertEqual(rc_ok, 0, err_ok)

        stored = json.loads(reg_path.read_text(encoding="utf-8"))["traps"][0]
        self.assertEqual(stored["status"], "promoted")
        self.assertFalse(stored["promotion_candidate"])
        self.assertEqual(stored["occurrences"], 3)
        self.assertEqual(stored["source_traces"], original_traces)

        ok_ver, errs_ver, count_ver = verify_trap_fixtures(reg_path)
        self.assertTrue(ok_ver, errs_ver)
        self.assertEqual(count_ver, 1)


class TestCliPathSecurityBoundaryGuards(_HermeticTempTestCase):
    """Verify CLI --registry and scan path security guards against control chars, .git/.env, and links."""

    def test_registry_and_scan_reject_nul_and_control_characters(self) -> None:
        parser = cli.build_parser()
        rig_dir = self.tmp_dir / ".stigmerig"
        valid_reg = self.tmp_dir / "valid_traps.json"

        for ctrl_char in ("\x00", "\x01", "\x1b", "\x7f", "\n"):
            bad_reg = str(self.tmp_dir / f"bad{ctrl_char}traps.json")
            with self.assertRaises(ValueError):
                cli._validate_cli_args(parser.parse_args(["--rig-dir", str(rig_dir), "traps", "verify", "--registry", bad_reg]))

            bad_scan = str(self.tmp_dir / f"src{ctrl_char}file.ts")
            with self.assertRaises(ValueError):
                validate_traps_cli_args(
                    parser.parse_args(["traps", "scan", bad_scan, "--registry", str(valid_reg)]),
                    rig_dir,
                )

        # Verify end-to-end CLI exit code 1 and [ERROR] reporting on NUL and control characters.
        rc_reg, _, err_reg = self.run_cli(
            ["--rig-dir", str(rig_dir), "traps", "verify", "--registry", str(self.tmp_dir / "bad\x00traps.json")]
        )
        self.assertEqual(rc_reg, 1)
        self.assertIn("[ERROR]", err_reg)

        rc_scan, _, err_scan = self.run_cli(
            ["--rig-dir", str(rig_dir), "traps", "scan", str(self.tmp_dir / "src\x1bfile.ts")]
        )
        self.assertEqual(rc_scan, 1)
        self.assertIn("[ERROR]", err_scan)

    def test_registry_and_scan_reject_git_and_env_traversal_and_db_targets(self) -> None:
        parser = cli.build_parser()
        ws = self.tmp_dir / "ws"
        rig_dir = ws / ".stigmerig"
        (ws / ".git" / "hooks").mkdir(parents=True)
        (ws / ".env.d").mkdir(parents=True)
        (ws / "sub").mkdir(parents=True)
        (ws / ".env").write_text("TOKEN=1\n", encoding="utf-8")
        (ws / ".env.local").write_text("TOKEN=2\n", encoding="utf-8")

        bad_registries = (
            ws / ".git" / "traps.json",
            ws / ".GIT" / "traps.json",
            ws / "sub" / ".." / ".git" / "traps.json",
            ws / ".env",
            ws / ".env.local",
            ws / ".env.d" / "traps.json",
            ws / "queue.db",
            ws / "audit.db",
        )
        for bad_reg in bad_registries:
            with self.assertRaises(ValueError, msg=f"Expected registry {bad_reg} to raise ValueError"):
                validate_traps_cli_args(
                    parser.parse_args(["traps", "seed", "--pack", "generic", "--registry", str(bad_reg)]),
                    rig_dir,
                )

        with self.assertRaises(IsADirectoryError):
            validate_traps_cli_args(
                parser.parse_args(["traps", "seed", "--pack", "generic", "--registry", str(ws / "sub")]),
                rig_dir,
            )

        for bad_scan in (ws / ".git", ws / "sub" / ".." / ".git", ws / ".env", ws / ".env.local", ws / ".ENV.prod"):
            with self.assertRaises(ValueError, msg=f"Expected scan path {bad_scan} to raise ValueError"):
                validate_traps_cli_args(
                    parser.parse_args(["traps", "scan", str(bad_scan)]),
                    rig_dir,
                )

        # End-to-end CLI exit 1 verification on .git and .env traversal.
        rc_git, _, err_git = self.run_cli(
            ["--rig-dir", str(rig_dir), "traps", "seed", "--pack", "generic", "--registry", str(ws / "sub" / ".." / ".git" / "traps.json")]
        )
        self.assertEqual(rc_git, 1)
        self.assertIn("[ERROR]", err_git)

        rc_env, _, err_env = self.run_cli(
            ["--rig-dir", str(rig_dir), "traps", "scan", str(ws / ".env.local")]
        )
        self.assertEqual(rc_env, 1)
        self.assertIn("[ERROR]", err_env)

    def test_registry_and_scan_reject_symlinks_and_junctions(self) -> None:
        parser = cli.build_parser()
        rig_dir = self.tmp_dir / ".stigmerig"
        real_dir = self.tmp_dir / "real_pkg"
        real_dir.mkdir(parents=True)
        (real_dir / "clean.ts").write_text("export const ok = 1;\n", encoding="utf-8")

        real_reg = real_dir / "real_traps.json"
        seed_starter_traps(real_reg, pack="generic", status="promoted")

        linked_dir = self.tmp_dir / "linked_pkg"
        if not _create_dir_link(linked_dir, real_dir):
            self.skipTest("Platform does not permit directory symlinks or NTFS junctions")

        for bad_scan in (linked_dir, linked_dir / "clean.ts"):
            with self.assertRaises(ValueError):
                validate_traps_cli_args(
                    parser.parse_args(["traps", "scan", str(bad_scan), "--registry", str(real_reg)]),
                    rig_dir,
                )

        rc_scan, _, err_scan = self.run_cli(
            ["--rig-dir", str(rig_dir), "traps", "scan", str(linked_dir), "--registry", str(real_reg)]
        )
        self.assertEqual(rc_scan, 1)
        self.assertIn("[ERROR]", err_scan)

        rc_seed, _, err_seed = self.run_cli(
            [
                "--rig-dir",
                str(rig_dir),
                "traps",
                "seed",
                "--pack",
                "generic",
                "--registry",
                str(linked_dir / "through_link.json"),
            ]
        )
        self.assertEqual(rc_seed, 1)
        self.assertIn("[ERROR]", err_seed)
        self.assertFalse((real_dir / "through_link.json").exists())

        file_link_reg = self.tmp_dir / "reg_link.json"
        file_link_src = self.tmp_dir / "src_link.ts"
        try:
            file_link_reg.symlink_to(real_reg)
            file_link_src.symlink_to(real_dir / "clean.ts")
        except OSError:
            return

        with self.assertRaises(ValueError):
            validate_traps_cli_args(
                parser.parse_args(["traps", "verify", "--registry", str(file_link_reg)]),
                rig_dir,
            )
        with self.assertRaises(ValueError):
            validate_traps_cli_args(
                parser.parse_args(["traps", "scan", str(file_link_src), "--registry", str(real_reg)]),
                rig_dir,
            )


class TestTwoProcessConcurrentFailTaskAndPromote(_HermeticTempTestCase):
    """Verify cross-process lock serialization between StigmeRigDB.fail_task and CLI traps register/promote."""

    def test_two_process_concurrent_fail_task_and_traps_register_promote(self) -> None:
        ws_dir = self.tmp_dir / "concurrent_ws"
        rig_dir = ws_dir / ".stigmerig"
        sync_dir = self.tmp_dir / "sync"
        sync_dir.mkdir(parents=True)

        rig = StigmeRigDB(rig_dir=rig_dir)
        rig.init_rig(scaffold_icm=False)

        blocker_specs = [
            ("JOB-201", "worker-a-1", "Database migration lock timeout alpha"),
            ("JOB-202", "worker-a-2", "Upstream socket reset beta"),
            ("JOB-203", "worker-a-3", "Schema validation mismatch gamma"),
            ("JOB-204", "worker-a-4", "Deadletter queue overflow delta"),
            ("JOB-205", "worker-a-5", "Artifact checksum mismatch epsilon"),
            ("JOB-206", "worker-a-6", "Worker lease heartbeat stall zeta"),
        ]
        # Seed working task rows in one SQLite batch so setup stays sub-10ms.
        with rig.get_queue_conn() as conn:
            conn.executemany(
                """
                INSERT INTO tasks (
                    id, title, status, priority, created_at, updated_at,
                    claimed_by, lease_until, version, context_path, archetype
                ) VALUES (?, ?, 'working', 100, '2026-11-15T00:00:00Z', '2026-11-15T00:00:00Z',
                          ?, '2099-01-01T00:00:00Z', 1, ?, 'diff_synthesizer')
                """,
                [
                    (job_id, f"Concurrent {job_id}", agent_name, f".stigmerig/tasks/{job_id}.md")
                    for job_id, agent_name, _ in blocker_specs
                ],
            )
            conn.commit()

        src_root = str(Path(__file__).resolve().parent.parent / "src")
        child_env = os.environ.copy()
        child_env.pop("STIGMERIG_ROOT", None)
        existing_py_path = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = (
            f"{src_root}{os.pathsep}{existing_py_path}" if existing_py_path else src_root
        )

        worker_a_code = textwrap.dedent(
            """
            import json
            from pathlib import Path
            import sys
            import time
            sys.path.insert(0, sys.argv[4])
            from stigmerig.db import StigmeRigDB

            rig_dir = Path(sys.argv[1])
            sync_dir = Path(sys.argv[2])
            specs = json.loads(sys.argv[3])

            (sync_dir / "ready_a").write_text("1", encoding="utf-8")
            deadline = time.monotonic() + 3.0
            while not (sync_dir / "ready_b").exists() and time.monotonic() < deadline:
                time.sleep(0.002)

            db = StigmeRigDB(rig_dir=rig_dir)
            for job_id, agent_name, reason in specs:
                assert db.fail_task(job_id, agent=agent_name, reason=reason) is True
            """
        )

        worker_b_code = textwrap.dedent(
            """
            from contextlib import redirect_stdout
            import io
            from pathlib import Path
            import sys
            import time
            sys.path.insert(0, sys.argv[4])
            import stigmerig.cli as cli

            rig_dir = sys.argv[1]
            sync_dir = Path(sys.argv[2])
            count = int(sys.argv[3])

            (sync_dir / "ready_b").write_text("1", encoding="utf-8")
            deadline = time.monotonic() + 3.0
            while not (sync_dir / "ready_a").exists() and time.monotonic() < deadline:
                time.sleep(0.002)

            sink = io.StringIO()
            with redirect_stdout(sink):
                for idx in range(1, count + 1):
                    trap_id = f"TRAP-CONC-{idx:02d}"
                    rc_reg = cli.main([
                        "--rig-dir", rig_dir,
                        "traps", "register",
                        "--id", trap_id,
                        "--domain", "concurrency-suite",
                        "--title", f"Concurrent Polyglot Rule {idx:02d}",
                        "--pattern", rf"\\bunsafe_call_{idx:02d}\\(",
                        "--positive-fixture", f"res = unsafe_call_{idx:02d}(arg)\\n",
                        "--negative-fixture", f"res = safe_call_{idx:02d}(arg)\\n",
                        "--match-mode", "multiline",
                        "--status", "candidate",
                        "--json",
                    ])
                    assert rc_reg == 0, f"register failed for {trap_id}: {rc_reg}"
                    rc_prom = cli.main([
                        "--rig-dir", rig_dir,
                        "traps", "promote",
                        "--id", trap_id,
                        "--remediation",
                        f"Replace unsafe_call_{idx:02d}() with bounded safe_call_{idx:02d}() across polyglot modules.",
                        "--json",
                    ])
                    assert rc_prom == 0, f"promote failed for {trap_id}: {rc_prom}"
            """
        )

        proc_a = subprocess.Popen(
            [
                sys.executable,
                "-S",
                "-W",
                "error",
                "-c",
                worker_a_code,
                str(rig_dir),
                str(sync_dir),
                json.dumps(blocker_specs),
                src_root,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )
        proc_b = subprocess.Popen(
            [
                sys.executable,
                "-S",
                "-W",
                "error",
                "-c",
                worker_b_code,
                str(rig_dir),
                str(sync_dir),
                "3",
                src_root,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )

        out_a, err_a = proc_a.communicate(timeout=10.0)
        out_b, err_b = proc_b.communicate(timeout=10.0)

        self.assertEqual(proc_a.returncode, 0, f"Process A failed: stdout={out_a!r} stderr={err_a!r}")
        self.assertEqual(proc_b.returncode, 0, f"Process B failed: stdout={out_b!r} stderr={err_b!r}")
        self.assertEqual(err_a, "")
        self.assertEqual(err_b, "")

        traps_file = rig_dir / "traps.json"
        self.assertTrue(traps_file.is_file())
        self.assertFalse((rig_dir / "traps.json.lock").exists())
        self.assertEqual([p for p in rig_dir.rglob("*") if p.name.endswith(".tmp")], [])

        parsed_doc = json.loads(traps_file.read_text(encoding="utf-8"))
        traps_list = parsed_doc["traps"]
        self.assertEqual(len(traps_list), 9)

        promoted_ids = {
            t.get("trap_id") or t.get("id")
            for t in traps_list
            if t.get("status") == "promoted"
        }
        self.assertEqual(promoted_ids, {"TRAP-CONC-01", "TRAP-CONC-02", "TRAP-CONC-03"})

        candidate_traces = {
            trace
            for t in traps_list
            if t.get("status") == "candidate"
            for trace in t.get("source_traces", [])
        }
        expected_traces = {f"Task {job_id} ({agent})" for job_id, agent, _ in blocker_specs}
        self.assertEqual(candidate_traces, expected_traces)

        ok_ver, errs_ver, count_ver = verify_trap_fixtures(traps_file, include_candidates=False)
        self.assertTrue(ok_ver, errs_ver)
        self.assertEqual(count_ver, 3)

        self.assertEqual(len(rig.list_tasks(status="blocked")), 6)
        audit_ok, audit_msg = rig.verify_audit_chain()
        self.assertTrue(audit_ok, audit_msg)

    def test_dual_registry_repo_and_local_trap_merge(self) -> None:
        repo = self.tmp_dir
        src = repo / "src"
        src.mkdir(parents=True)
        (src / "app.py").write_text(
            "def f():\n    try:\n        work()\n    except Exception:\n        pass\n    # LOCAL_ONLY_TRAP\n",
            encoding="utf-8",
        )

        # Team/CI-enforced trap in tracked stigmerig.traps.json
        rc_seed, _, err_seed = self.run_cli(
            ["traps", "seed", "--pack", "python", "--registry", str(repo / "stigmerig.traps.json"), "--json"],
            cwd=repo,
        )
        self.assertEqual(rc_seed, 0, err_seed)

        # User/local trap in gitignored .stigmerig/traps.json
        rc_reg, _, err_reg = self.run_cli(
            [
                "traps",
                "register",
                "--trap-id",
                "TRAP-LOCAL-001",
                "--domain",
                "local-hygiene",
                "--title",
                "Local Marker Forbidden",
                "--pattern",
                "LOCAL_ONLY_TRAP",
                "--forbidden",
                "Do not leave LOCAL_ONLY_TRAP markers in source.",
                "--source-trace",
                "local-dev-session",
                "--positive-fixture",
                "# LOCAL_ONLY_TRAP",
                "--negative-fixture",
                "# clean",
                "--status",
                "promoted",
                "--remediation",
                "Remove the temporary LOCAL_ONLY_TRAP marker before committing.",
                "--json",
            ],
            cwd=repo,
        )
        self.assertEqual(rc_reg, 0, err_reg)

        # Auto-discovery merges both registries without --registry
        rc_scan, out_scan, _ = self.run_cli(["traps", "scan", "--json"], cwd=repo)
        self.assertEqual(rc_scan, 1)
        report = json.loads(out_scan)
        self.assertEqual(len(report["registries"]), 2)
        hit_ids = {v["trap_id"] for v in report["violations"]}
        self.assertEqual(hit_ids, {"TRAP-PY-001", "TRAP-LOCAL-001"})
        # stigmerig.traps.json itself must be excluded from scanning
        self.assertEqual(report["scanned_files"], 1)


if __name__ == "__main__":
    unittest.main()

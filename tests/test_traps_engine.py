"""Unit tests for the zero-dependency StigmeRig polyglot trap engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from stigmerig.traps import (
    DEFAULT_PRUNED_DIRS,
    MAX_FILE_SIZE,
    MAX_PATTERN_LENGTH,
    POLYGLOT_EXTENSIONS,
    STARTER_TRAP_PACKS,
    audit_content_for_traps,
    audit_file,
    collect_scannable_files,
    compile_glob_regex,
    dismiss_trap,
    is_prescriptive_remediation,
    is_target_applicable,
    load_promoted_traps,
    promote_trap,
    register_trap,
    scan_paths,
    seed_starter_traps,
    trap_file_lock,
    validate_trap_payload,
    verify_trap_fixtures,
)


class TestTrapsEngineConstantsAndValidation(unittest.TestCase):
    """Verify engine constants, regex payload validation, and remediation gate."""

    def test_polyglot_extensions_and_pruned_dirs_inventory(self) -> None:
        self.assertGreaterEqual(len(POLYGLOT_EXTENSIONS), 25)
        for required_ext in (
            ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".rs", ".go", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp",
            ".hpp", ".cs", ".rb", ".php", ".swift", ".zig", ".sql", ".sh",
            ".bash", ".ps1", ".tf", ".hcl", ".toml", ".yaml", ".yml", ".md",
            ".svelte", ".vue",
        ):
            self.assertIn(required_ext, POLYGLOT_EXTENSIONS)

        for required_dir in (
            ".git", ".hg", ".svn", ".stigmerig", ".worktrees", "node_modules",
            "target", "dist", "build", "out", "vendor", ".next", ".nuxt",
            "bin", "obj", ".gradle", "__pycache__", ".venv", "venv",
            ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "coverage",
        ):
            self.assertIn(required_dir, DEFAULT_PRUNED_DIRS)

        self.assertEqual(MAX_PATTERN_LENGTH, 200)
        self.assertEqual(MAX_FILE_SIZE, 2 * 1024 * 1024)

    def test_is_prescriptive_remediation(self) -> None:
        self.assertFalse(is_prescriptive_remediation(""))
        self.assertFalse(is_prescriptive_remediation("   "))
        self.assertFalse(is_prescriptive_remediation("N/A"))
        self.assertFalse(is_prescriptive_remediation("na"))
        self.assertFalse(is_prescriptive_remediation("None"))
        self.assertFalse(is_prescriptive_remediation("TBD"))
        self.assertFalse(is_prescriptive_remediation("Fix this bug now."))
        self.assertFalse(is_prescriptive_remediation("ATOM-01"))
        self.assertFalse(is_prescriptive_remediation("ATOM-VERY-LONG-BARE-IDENTIFIER-CODE-99"))

        self.assertTrue(
            is_prescriptive_remediation(
                "Catch narrow OSError or ValueError tuples instead of bare exception blocks per ruff S110."
            )
        )
        self.assertTrue(
            is_prescriptive_remediation(
                "ATOM-05: Wrap sqlite3.connect() in contextlib.closing() so handles close deterministically."
            )
        )

    def test_validate_trap_payload_line_and_multiline(self) -> None:
        ok, msg = validate_trap_payload(
            pattern=r"\bforbidden_call\(",
            pos_fixture="res = forbidden_call(arg)",
            neg_fixture="res = allowed_call(arg)",
        )
        self.assertTrue(ok, msg)

        ok_long, msg_long = validate_trap_payload("x" * 201, "x", "y")
        self.assertFalse(ok_long)
        self.assertIn("exceeds maximum of 200 characters", msg_long)

        ok_bad, msg_bad = validate_trap_payload(r"[unclosed", "a", "b")
        self.assertFalse(ok_bad)
        self.assertIn("failed to compile", msg_bad)

        # Anchored ^ pattern past line 1 requires re.MULTILINE in multiline mode
        anchored_pat = r"^([ \t]*)except\s+Exception:\s*\n\1[ \t]+pass\b"
        pos_multi = "def run():\n    try:\n        work()\n    ex" + "cept Exception:\n        pass\n"
        neg_multi = "def run():\n    try:\n        work()\n    except ValueError:\n        return None\n"

        ok_ml, msg_ml = validate_trap_payload(anchored_pat, pos_multi, neg_multi, match_mode="multiline")
        self.assertTrue(ok_ml, msg_ml)

        ok_line, _ = validate_trap_payload(anchored_pat, pos_multi, neg_multi, match_mode="line")
        self.assertFalse(ok_line)


class TestSegmentAwareGlobsAndDiscovery(unittest.TestCase):
    """Verify segment-aware glob compilation, negations, sentinels, and pruning."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_compile_glob_regex_segment_boundaries(self) -> None:
        sub_ctx = compile_glob_regex("*/CONTEXT.md")
        self.assertIsNotNone(sub_ctx.match("SubProject/CONTEXT.md"))
        self.assertIsNone(sub_ctx.match("CONTEXT.md"))
        self.assertIsNone(sub_ctx.match(".agents/orchestrator/CONTEXT.md"))

        qmark = compile_glob_regex("src/?.py")
        self.assertIsNotNone(qmark.match("src/a.py"))
        self.assertIsNone(qmark.match("src/ab.py"))
        self.assertIsNone(qmark.match("src///py"))

        rec_prefix = compile_glob_regex("**/src/**/*.py")
        self.assertIsNotNone(rec_prefix.match("src/main.py"))
        self.assertIsNotNone(rec_prefix.match("pkg/src/mod/util.py"))

        rec_suffix = compile_glob_regex("tests/**")
        self.assertIsNotNone(rec_suffix.match("tests"))
        self.assertIsNotNone(rec_suffix.match("tests/unit/test_core.py"))
        self.assertIsNone(rec_suffix.match("tests_extra/test_core.py"))

        all_glob = compile_glob_regex("**")
        self.assertIsNotNone(all_glob.match("any/nested/path/file.rs"))

    def test_is_target_applicable_context_negations_and_exclusions(self) -> None:
        ctx_globs = ["AGENTS.md", "CONTEXT.md", "*/CONTEXT.md"]
        self.assertTrue(is_target_applicable(Path("AGENTS.md"), ctx_globs))
        self.assertTrue(is_target_applicable(Path("nested/pkg/AGENTS.md"), ctx_globs))
        self.assertTrue(is_target_applicable(Path("CONTEXT.md"), ctx_globs))
        self.assertTrue(is_target_applicable(Path("SubModule/CONTEXT.md"), ctx_globs))
        self.assertFalse(is_target_applicable(Path(".agents/orchestrator/CONTEXT.md"), ctx_globs))
        self.assertFalse(is_target_applicable(Path(".agents/orchestrator/context.md"), ctx_globs))

        code_globs = [
            "src/**/*.py",
            "!src/stigmerig/db.py",
            "!tests/**",
            "!**/tests/**",
            "!**/test_*.py",
        ]
        self.assertTrue(is_target_applicable(Path("src/stigmerig/wave.py"), code_globs))
        self.assertFalse(is_target_applicable(Path("src/stigmerig/db.py"), code_globs))
        self.assertFalse(is_target_applicable(Path("src/tests/helper.py"), code_globs))
        self.assertFalse(is_target_applicable(Path("src/stigmerig/test_local.py"), code_globs))

        # Excluded registry filenames never self-match
        self.assertFalse(is_target_applicable(Path("traps.json"), ["**"]))
        self.assertFalse(is_target_applicable(Path("Trap_Invariants.json"), ["**"]))

        # Extra pruned dirs and relative prefixes
        self.assertFalse(
            is_target_applicable(
                Path("restricted_vault/notes.py"),
                ["**/*.py"],
                extra_pruned_dirs=("restricted_vault",),
            )
        )
        self.assertFalse(
            is_target_applicable(
                Path("sub/Restricted_Vault/notes.py"),
                ["**/*.py"],
                extra_pruned_dirs=("restricted_vault",),
            )
        )
        self.assertFalse(
            is_target_applicable(
                Path("vendor_packs/external/mod.py"),
                ["**/*.py"],
                extra_pruned_rel_prefixes=("vendor_packs/external",),
            )
        )
        self.assertTrue(
            is_target_applicable(
                Path("vendor_packs/internal/mod.py"),
                ["**/*.py"],
                extra_pruned_rel_prefixes=("vendor_packs/external",),
            )
        )

    def test_sentinel_files_and_polyglot_file_discovery(self) -> None:
        repo = self.tmp_dir / "workspace"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "engine.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "src" / "client.ts").write_text("export const x = 1;\n", encoding="utf-8")
        (repo / "src" / "lib.rs").write_text("pub fn ok() {}\n", encoding="utf-8")
        (repo / "src" / "main.go").write_text("package main\n", encoding="utf-8")
        (repo / "src" / "infra.tf").write_text("variable \"env\" {}\n", encoding="utf-8")
        (repo / "src" / "binary.bin").write_bytes(b"\x00\x01\x02")
        (repo / "traps.json").write_text("{}", encoding="utf-8")
        (repo / "Trap_Invariants.json").write_text("{}", encoding="utf-8")

        # Oversized file (> max_file_size)
        (repo / "src" / "oversized.py").write_bytes(b"a" * 1024)

        # Default pruned build trees
        for pruned_name in ("node_modules", "target", "vendor", "__pycache__", ".stigmerig"):
            p_dir = repo / pruned_name
            p_dir.mkdir()
            (p_dir / "ignored.py").write_text("x = 2\n", encoding="utf-8")

        # Pluggable extra_pruned_dirs + extra_pruned_rel_prefixes + sentinel_files
        vault = repo / "private_sandbox"
        vault.mkdir()
        (vault / "secret.py").write_text("x = 3\n", encoding="utf-8")

        ext_bundle = repo / "bundles" / "third_party"
        ext_bundle.mkdir(parents=True)
        (ext_bundle / "plugin.ts").write_text("export const p = 1;\n", encoding="utf-8")

        guarded = repo / "guarded_zone"
        guarded.mkdir()
        (guarded / "SKIP_SCAN.sentinel").write_text("hold\n", encoding="utf-8")
        (guarded / "protected.rs").write_text("pub fn secret() {}\n", encoding="utf-8")

        self.assertFalse(
            is_target_applicable(
                guarded / "protected.rs",
                ["**/*.rs"],
                repo_root=repo,
                sentinel_files=("SKIP_SCAN.sentinel",),
            )
        )
        self.assertFalse(
            is_target_applicable(
                Path("guarded_zone/protected.rs"),
                ["**/*.rs"],
                repo_root=repo,
                sentinel_files=("SKIP_SCAN.sentinel",),
            )
        )

        discovered = collect_scannable_files(
            repo,
            extra_pruned_dirs=("private_sandbox",),
            extra_pruned_rel_prefixes=("bundles/third_party",),
            sentinel_files=("SKIP_SCAN.sentinel",),
            repo_root=repo,
            max_file_size=512,
        )
        rel_discovered = [p.resolve().relative_to(repo.resolve()).as_posix() for p in discovered]
        self.assertEqual(
            rel_discovered,
            [
                "src/client.ts",
                "src/engine.py",
                "src/infra.tf",
                "src/lib.rs",
                "src/main.go",
            ],
        )


class TestMultilineScanningLocksAndPromotion(unittest.TestCase):
    """Verify multiline scanning, stale lock recovery, Tier A/B schemas, and starter packs."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_multiline_anchored_audit_past_line_one(self) -> None:
        trap = {
            "id": "TRAP-ML-01",
            "title": "Anchored Multiline Silent Handler",
            "reason": "Swallowing broad exceptions silently.",
            "remediation": "Catch specific exception types and log context per ruff S110.",
            "trigger_pattern": r"^([ \t]*)except\s+Exception:\s*\n\1[ \t]+pass\b",
            "match_mode": "multiline",
            "status": "promoted",
        }
        sample = (
            "import os\n"
            "\n"
            "def process_item(path: str) -> None:\n"
            "    try:\n"
            "        os.stat(path)\n"
            "    ex" + "cept Exception:\n"
            "        pass\n"
        )
        violations = audit_content_for_traps(sample, [trap], "pkg/worker.py")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["trap_id"], "TRAP-ML-01")
        self.assertEqual(violations[0]["line"], 6)
        self.assertIn("ruff S110", violations[0]["remediation"])

        # Same pattern in line mode must not match across newline boundaries
        line_trap = dict(trap, match_mode="line")
        self.assertEqual(audit_content_for_traps(sample, [line_trap], "pkg/worker.py"), [])

    def test_trap_file_lock_stale_recovery_and_timeout(self) -> None:
        reg_path = self.tmp_dir / "traps.json"
        lock_path = self.tmp_dir / "traps.json.lock"

        # 1. Stale lockfile is automatically recovered and cleaned up on exit
        lock_path.write_text('{"pid": 999999, "acquired_at": 1.0}\n', encoding="utf-8")
        stale_ts = time.time() - 120.0
        os.utime(lock_path, (stale_ts, stale_ts))

        with trap_file_lock(reg_path, timeout=0.5, stale_after=5.0) as acquired_path:
            self.assertEqual(acquired_path, lock_path)
            self.assertTrue(lock_path.exists())
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock_data["pid"], os.getpid())

        self.assertFalse(lock_path.exists())

        # 2. Fresh lockfile held within stale_after raises TimeoutError
        lock_path.write_text('{"pid": 999999, "acquired_at": 1.0}\n', encoding="utf-8")
        try:
            with self.assertRaises(TimeoutError):
                with trap_file_lock(reg_path, timeout=0.08, stale_after=60.0):
                    pass
        finally:
            lock_path.unlink(missing_ok=True)

    def test_tier_a_harvested_blocker_promotion_preserves_traces(self) -> None:
        reg_path = self.tmp_dir / "traps.json"
        harvested_doc = {
            "version": "1.0.0",
            "traps": [
                {
                    "id": "TRAP-001",
                    "title": "Unbounded socket connect call without timeout",
                    "reason": "Unbounded socket connect call hung worker lease",
                    "trigger_pattern": "unbounded socket connect call hung worker lease",
                    "status": "candidate",
                    "occurrences": 3,
                    "promotion_candidate": True,
                    "first_seen_at": "2026-11-15T08:00:00Z",
                    "last_seen_at": "2026-11-15T10:00:00Z",
                    "source_traces": [
                        "Task JOB-101 (worker-a)",
                        "Task JOB-102 (worker-b)",
                        "Task JOB-103 (worker-c)",
                    ],
                }
            ],
        }
        reg_path.write_text(json.dumps(harvested_doc, indent=2), encoding="utf-8")

        # Promotion without fixtures must fail
        with self.assertRaises(ValueError):
            promote_trap("TRAP-001", reg_path)

        # Promotion with stub remediation must fail when enforce_prescriptive=True
        with self.assertRaises(ValueError):
            promote_trap(
                "TRAP-001",
                reg_path,
                pattern=r"\bsocket\.create_connection\(\s*addr\s*\)",
                pos_fixture="conn = socket.create_connection(addr)",
                neg_fixture="conn = socket.create_connection(addr, timeout=5.0)",
                remediation="ATOM-05",
            )

        # Valid evidence-linked promotion upgrades in place and preserves traces + occurrences
        promoted_ok = promote_trap(
            "TRAP-001",
            reg_path,
            pattern=r"\bsocket\.create_connection\(\s*addr\s*\)",
            pos_fixture="conn = socket.create_connection(addr)",
            neg_fixture="conn = socket.create_connection(addr, timeout=5.0)",
            remediation="Pass an explicit timeout= parameter to socket.create_connection() to prevent hung leases.",
            target_globs=["**/*.py", "!**/tests/**"],
        )
        self.assertTrue(promoted_ok)

        persisted = json.loads(reg_path.read_text(encoding="utf-8"))
        entry = persisted["traps"][0]
        self.assertEqual(entry["id"], "TRAP-001")
        self.assertEqual(entry["status"], "promoted")
        self.assertFalse(entry["promotion_candidate"])
        self.assertEqual(entry["occurrences"], 3)
        self.assertEqual(
            entry["source_traces"],
            [
                "Task JOB-101 (worker-a)",
                "Task JOB-102 (worker-b)",
                "Task JOB-103 (worker-c)",
            ],
        )

        ok_verify, errors, count = verify_trap_fixtures(reg_path)
        self.assertTrue(ok_verify, errors)
        self.assertEqual(count, 1)

        loaded = load_promoted_traps(reg_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["trap_id"], "TRAP-001")
        self.assertEqual(loaded[0]["id"], "TRAP-001")

        hits = audit_content_for_traps("c = socket.create_connection(addr)\n", loaded, "src/store.py")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["trap_id"], "TRAP-001")

    def test_tier_b_register_dedup_dismiss_and_prescriptive_verify(self) -> None:
        reg_path = self.tmp_dir / "Trap_Invariants.json"

        # Backward-compatible programmatic registration allows empty remediation when candidate
        cand = register_trap(
            domain="ai-systems",
            title="Stub Candidate Trap",
            pattern=r"\blegacy_unsafe_api\(",
            forbidden="Calling deprecated unsafe API.",
            source_trace="trace-1",
            pos_fixture="legacy_unsafe_api(data)",
            neg_fixture="safe_api(data)",
            status="promoted",
            remediation="ATOM-01",
            registry_path=reg_path,
        )
        self.assertEqual(cand["trap_id"], "TRAP-001")

        # Duplicate registration increments occurrences and appends source_traces
        dup = register_trap(
            domain="ai-systems",
            title="  stub   candidate trap ",
            pattern=r"\blegacy_unsafe_api\(",
            forbidden="Calling deprecated unsafe API.",
            source_trace="trace-2",
            pos_fixture="legacy_unsafe_api(data)",
            neg_fixture="safe_api(data)",
            registry_path=reg_path,
        )
        self.assertEqual(dup["occurrences"], 2)
        self.assertEqual(dup["source_traces"], ["trace-1", "trace-2"])

        # verify_trap_fixtures flags promoted traps with non-prescriptive ATOM-01 stub
        ok_strict, errs_strict, _ = verify_trap_fixtures(reg_path, require_prescriptive_on_promoted=True)
        self.assertFalse(ok_strict)
        self.assertEqual(len(errs_strict), 1)
        self.assertIn("TRAP-001", errs_strict[0])

        ok_relaxed, errs_relaxed, _ = verify_trap_fixtures(reg_path, require_prescriptive_on_promoted=False)
        self.assertTrue(ok_relaxed, errs_relaxed)

        # enforce_prescriptive_remediation=True rejects empty remediation on register_trap
        with self.assertRaises(ValueError):
            register_trap(
                domain="ai-systems",
                title="Strict Trap",
                pattern=r"\bstrict_bad\(",
                forbidden="Bad call.",
                source_trace="trace-3",
                pos_fixture="strict_bad()",
                neg_fixture="strict_good()",
                enforce_prescriptive_remediation=True,
                registry_path=reg_path,
            )

        # Dismiss removes trap from promoted active list
        self.assertTrue(dismiss_trap("TRAP-001", reason="Superseded", registry_path=reg_path))
        self.assertEqual(load_promoted_traps(reg_path, include_candidates=False), [])

    def test_all_six_starter_packs_verify_and_self_scan_cleanly(self) -> None:
        self.assertEqual(
            set(STARTER_TRAP_PACKS.keys()),
            {"generic", "python", "typescript", "rust", "go", "verification"},
        )

        # Verify each individual pack and combined 'all' pack
        expected_linters = {
            "generic": "git diff --check",
            "python": "ruff S110",
            "typescript": "@typescript-eslint/ban-ts-comment",
            "rust": "clippy",
            "go": "errcheck",
            "verification": "ruff PGH004",
        }
        for pack_name, expected_rule in expected_linters.items():
            pack_reg = self.tmp_dir / f"traps_{pack_name}.json"
            seeded = seed_starter_traps(pack_reg, pack=pack_name, status="promoted")
            self.assertGreaterEqual(len(seeded), 1)
            ok, errors, count = verify_trap_fixtures(
                pack_reg,
                include_candidates=True,
                require_prescriptive_on_promoted=True,
            )
            self.assertTrue(ok, f"Pack {pack_name} failed verification: {errors}")
            self.assertEqual(count, len(seeded))
            combined_rem = " ".join(t["remediation"] for t in seeded)
            self.assertIn(expected_rule, combined_rem)

        all_reg = self.tmp_dir / "traps_all.json"
        first_seed = seed_starter_traps(all_reg, pack="all", status="promoted")
        second_seed = seed_starter_traps(all_reg, pack="all", status="promoted")
        self.assertEqual(len(first_seed), 11)
        self.assertEqual(len(second_seed), 11)

        persisted_all = json.loads(all_reg.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted_all["traps"]), 11)

        ok_all, errors_all, count_all = verify_trap_fixtures(all_reg)
        self.assertTrue(ok_all, errors_all)
        self.assertEqual(count_all, 11)

        # Scanning StigmeRig/src/stigmerig/traps.py itself with all 11 starter traps must yield 0 violations
        engine_file = Path(__file__).resolve().parent.parent / "src" / "stigmerig" / "traps.py"
        active_traps = load_promoted_traps(all_reg)
        self_violations = audit_file(engine_file, active_traps, repo_root=engine_file.parent.parent.parent)
        self.assertEqual(self_violations, [])

        # End-to-end polyglot scan_paths check on synthetic violations
        sample_proj = self.tmp_dir / "poly_proj"
        (sample_proj / "src").mkdir(parents=True)
        (sample_proj / "src" / "bad.go").write_text(
            "package main\nfunc run() {\n\tif " + "err != nil {}\n}\n",
            encoding="utf-8",
        )
        (sample_proj / "src" / "bad_test.go").write_text(
            "package main\nfunc TestRun() {\n\tif " + "err != nil {}\n}\n",
            encoding="utf-8",
        )
        scanned_count, violations = scan_paths([sample_proj], active_traps, repo_root=sample_proj)
        self.assertEqual(scanned_count, 2)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["trap_id"], "TRAP-GO-001")


if __name__ == "__main__":
    unittest.main()

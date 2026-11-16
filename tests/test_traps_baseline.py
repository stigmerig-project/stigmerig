"""Unit tests for brownfield trap baseline ratchet (`--write-baseline` and `--baseline`)."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from stigmerig.traps import (
    add_traps_parser,
    run_traps_command,
    seed_starter_traps,
    validate_traps_cli_args,
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


def _parse_traps_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="stigmerig")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_traps_parser(subparsers)
    return parser.parse_args(["traps", *argv])


def _run_traps(argv: list[str], rig_dir: Path) -> tuple[int, str, str]:
    args = _parse_traps_args(argv)
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = run_traps_command(args, rig_dir)
    return rc, out.getvalue(), err.getvalue()


class TestTrapsBaselineRatchet(unittest.TestCase):
    """Verify `--write-baseline` and `--baseline` multiset ratchet behavior."""

    def _init_repo_with_python_pack(self, root: Path) -> tuple[Path, Path]:
        rig_dir = root / ".stigmerig"
        rig_dir.mkdir(parents=True, exist_ok=True)
        reg_path = rig_dir / "traps.json"
        seed_starter_traps(reg_path, pack="python", status="promoted")
        return rig_dir, reg_path

    def test_line_shifts_keep_baseline_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rig_dir, _ = self._init_repo_with_python_pack(repo)
            src_dir = repo / "src"
            src_dir.mkdir()
            target_file = src_dir / "legacy.py"
            target_file.write_text(
                "def f():\n    try:\n        x = 1\n    except Exception:\n        pass\n",
                encoding="utf-8",
            )

            baseline_file = rig_dir / "traps.baseline.json"
            rc_w, out_w, err_w = _run_traps(
                ["scan", str(repo), "--write-baseline", str(baseline_file), "--json"],
                rig_dir,
            )
            self.assertEqual(rc_w, 0, err_w)
            write_report = json.loads(out_w)
            self.assertTrue(write_report["ok"])
            self.assertEqual(write_report["baselined_violations"], 1)
            self.assertEqual(write_report["baselined_fingerprints"], 1)

            baseline_data = json.loads(baseline_file.read_text(encoding="utf-8"))
            self.assertEqual(baseline_data["version"], "1.0.0")
            self.assertEqual(len(baseline_data["entries"]), 1)
            entry = baseline_data["entries"][0]
            self.assertEqual(entry["file"], "src/legacy.py")
            self.assertEqual(entry["trap_id"], "TRAP-PY-001")
            self.assertEqual(entry["count"], 1)
            expected_sha = hashlib.sha256("except Exception: pass".encode("utf-8")).hexdigest()
            self.assertEqual(entry["snippet_sha256"], expected_sha)

            # Shift line numbers downward by prepending comments/blank lines
            shifted = (
                "# comment 1\n# comment 2\n\n\n"
                + target_file.read_text(encoding="utf-8")
            )
            target_file.write_text(shifted, encoding="utf-8")

            rc_b, out_b, err_b = _run_traps(
                ["scan", str(repo), "--baseline", str(baseline_file), "--json"],
                rig_dir,
            )
            self.assertEqual(rc_b, 0, err_b)
            scan_report = json.loads(out_b)
            self.assertTrue(scan_report["ok"])
            self.assertEqual(scan_report["violations"], [])
            self.assertEqual(scan_report["suppressed_violations"], 1)

    def test_duplicate_identical_violation_in_baselined_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rig_dir, _ = self._init_repo_with_python_pack(repo)
            target_file = repo / "app.py"
            target_file.write_text(
                "def first():\n    try:\n        return 1\n    except Exception:\n        pass\n",
                encoding="utf-8",
            )
            baseline_file = rig_dir / "baseline.json"
            rc_w, _, err_w = _run_traps(
                ["scan", str(repo), "--write-baseline", str(baseline_file)],
                rig_dir,
            )
            self.assertEqual(rc_w, 0, err_w)

            # Add an identical second violation in the same baselined file
            target_file.write_text(
                target_file.read_text(encoding="utf-8")
                + "\ndef second():\n    try:\n        return 2\n    except Exception:\n        pass\n",
                encoding="utf-8",
            )

            rc_b, out_b, _ = _run_traps(
                ["scan", str(repo), "--baseline", str(baseline_file), "--json"],
                rig_dir,
            )
            self.assertEqual(rc_b, 1)
            report = json.loads(out_b)
            self.assertFalse(report["ok"])
            self.assertEqual(report["suppressed_violations"], 1)
            self.assertEqual(len(report["violations"]), 1)
            self.assertEqual(report["violations"][0]["trap_id"], "TRAP-PY-001")
            self.assertEqual(report["violations"][0]["line"], 10)

    def test_missing_baseline_file_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rig_dir, _ = self._init_repo_with_python_pack(repo)
            (repo / "clean.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
            missing = rig_dir / "does_not_exist.json"

            rc_text, _, err_text = _run_traps(
                ["scan", str(repo), "--baseline", str(missing)],
                rig_dir,
            )
            self.assertEqual(rc_text, 1)
            self.assertIn("Baseline file not found", err_text)

            rc_json, out_json, _ = _run_traps(
                ["scan", str(repo), "--baseline", str(missing), "--json"],
                rig_dir,
            )
            self.assertEqual(rc_json, 1)
            payload = json.loads(out_json)
            self.assertFalse(payload["ok"])
            self.assertIn("Baseline file not found", payload["error"])

    def test_baseline_path_rejects_symlinks_junctions_and_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rig_dir, _ = self._init_repo_with_python_pack(repo)
            real_dir = repo / "real_dir"
            real_dir.mkdir()
            real_baseline = real_dir / "baseline.json"
            real_baseline.write_text('{"entries": [], "version": "1.0.0"}\n', encoding="utf-8")

            link_dir = repo / "link_dir"
            if not _create_dir_link(link_dir, real_dir):
                self.skipTest("Platform does not permit directory symlinks or NTFS junctions")

            for flag in ("--baseline", "--write-baseline"):
                args = _parse_traps_args(["scan", str(repo), flag, str(link_dir / "baseline.json")])
                with self.assertRaises(ValueError):
                    validate_traps_cli_args(args, rig_dir)

            file_link = repo / "file_link.json"
            try:
                file_link.symlink_to(real_baseline)
                file_link_created = True
            except OSError:
                file_link_created = False

            if file_link_created:
                for flag in ("--baseline", "--write-baseline"):
                    args = _parse_traps_args(["scan", str(repo), flag, str(file_link)])
                    with self.assertRaises(ValueError):
                        validate_traps_cli_args(args, rig_dir)

            for bad_path in (
                repo / ".git" / "baseline.json",
                repo / ".env",
                rig_dir / "queue.db",
                rig_dir / "audit.db",
            ):
                for flag in ("--baseline", "--write-baseline"):
                    args = _parse_traps_args(["scan", str(repo), flag, str(bad_path)])
                    with self.assertRaises(ValueError):
                        validate_traps_cli_args(args, rig_dir)

    def test_active_baseline_file_is_excluded_from_scan_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rig_dir, _ = self._init_repo_with_python_pack(repo)
            # Use .md extension (which is in POLYGLOT_EXTENSIONS) to verify exclusion
            seed_starter_traps(rig_dir / "traps.json", pack="verification", status="promoted")
            baseline_md = repo / "baseline.md"
            baseline_md.write_text("TODO: cite placeholder\n", encoding="utf-8")
            (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")

            rc_w, out_w, err_w = _run_traps(
                ["scan", str(repo), "--write-baseline", str(baseline_md), "--json"],
                rig_dir,
            )
            self.assertEqual(rc_w, 0, err_w)
            report = json.loads(out_w)
            self.assertEqual(report["baselined_violations"], 0)


if __name__ == "__main__":
    unittest.main()

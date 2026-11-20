"""Hermetic tests for StigmeRig evergreen freshness ratchets and subtractive diff_metrics."""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stigmerig.cli import main as cli_main
from stigmerig.db import (
    DEFAULT_RIG_JSON_TEMPLATE,
    StigmeRigDB,
    parse_diffstat_metrics,
)
from stigmerig.telemetry import TelemetryExporter
from stigmerig.traps import (
    TRAP_EVG_001_REMEDIATION,
    collect_recently_touched_paths,
    run_freshness_scan,
    scan_annotations_in_text,
)
from stigmerig.wave import WaveRunner, WorktreeManager


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True, capture_output=True)


def _git_commit_with_date(repo: Path, msg: str, iso_date: str) -> None:
    env_script = {
        "GIT_AUTHOR_DATE": iso_date,
        "GIT_COMMITTER_DATE": iso_date,
    }
    import os

    env = {**os.environ, **env_script}
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, env=env, check=True, capture_output=True)


def test_rig_json_evergreen_defaults_backfill_and_validation(tmp_path: Path) -> None:
    template_data = json.loads(DEFAULT_RIG_JSON_TEMPLATE)
    assert template_data["platform_targets"] == {}
    assert template_data["freshness"] == {
        "stale_threshold_days": 180,
        "protected_globs": ["**/migrations/**", "**/contracts/**"],
    }

    rig_dir = tmp_path / "fresh_rig" / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig(scaffold_icm=True)
    cfg = db._load_rig_config()
    assert cfg["platform_targets"] == {}
    assert cfg["freshness"]["stale_threshold_days"] == 180
    assert cfg["freshness"]["protected_globs"] == ("**/migrations/**", "**/contracts/**")

    legacy_rig = tmp_path / "legacy_rig" / ".stigmerig"
    legacy_db = StigmeRigDB(rig_dir=legacy_rig)
    legacy_db.init_rig(scaffold_icm=False)
    (legacy_rig / "rig.json").write_text('{"max_pending_review": 2}\n', encoding="utf-8")
    backfilled = legacy_db._load_rig_config()
    assert backfilled["platform_targets"] == {}
    assert backfilled["freshness"]["stale_threshold_days"] == 180

    (legacy_rig / "rig.json").write_text('{"platform_targets": {"python": "3.12"}, "freshness": {"stale_threshold_days": 0}}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="stale_threshold_days"):
        legacy_db._load_rig_config()

    (legacy_rig / "rig.json").write_text('{"platform_targets": "bad"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="platform_targets"):
        legacy_db._load_rig_config()

    (legacy_rig / "rig.json").write_text('{"freshness": {"stale_threshold_days": true}}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="stale_threshold_days"):
        legacy_db._load_rig_config()


def test_scan_annotations_in_text_polyglot_semver_date_and_escaped_strings() -> None:
    text = "\n".join([
        'literal = "\\x40stability: deprecated \\x40remove-when: 2020-01-01"',
        "# \x40stability: deprecated",
        "# \x40remove-when: python>=3.11",
        "// \x40remove-when: node>=22.0.0",
        "-- \x40remove-when: 2026-11-20",
        "/* \x40stability: deprecated",
        "   keep for now */",
        "# \x40stability: deprecated",
        "# \x40remove-when: python>=3.99",
    ])
    expired, deprecated = scan_annotations_in_text(
        text,
        "src/shim.py",
        {"python": "3.12", "node": "20.11.0"},
        "2026-11-20",
    )
    assert len(expired) == 2
    assert {item["forbidden"] for item in expired} == {"python>=3.11", "2026-11-20"}
    assert all(item["trap_id"] == "TRAP-EVG-001" for item in expired)
    assert all(item["remediation"] == TRAP_EVG_001_REMEDIATION for item in expired)
    assert len(deprecated) == 1
    assert deprecated[0]["line"] == 6


def test_traps_freshness_git_staleness_protected_globs_and_non_git_fallback(tmp_path: Path, capsys) -> None:
    non_git = tmp_path / "nogit"
    non_git.mkdir()
    assert collect_recently_touched_paths(non_git, 180) is None
    ng_report = run_freshness_scan([non_git], repo_root=non_git)
    assert ng_report["stale_files"] == []

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "migrations").mkdir()
    (repo / "contracts").mkdir()
    (repo / "src").mkdir()
    (repo / "old.py").write_text("# \x40remove-when: python>=3.11\n", encoding="utf-8")
    (repo / "migrations" / "001.sql").write_text("-- init\n", encoding="utf-8")
    (repo / "contracts" / "schema.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "add", "old.py", "migrations/001.sql", "contracts/schema.json"], cwd=repo, check=True, capture_output=True)
    _git_commit_with_date(repo, "old commit", "2024-01-01T00:00:00Z")

    (repo / "src" / "recent.py").write_text("# active\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/recent.py"], cwd=repo, check=True, capture_output=True)
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _git_commit_with_date(repo, "recent commit", now_iso)

    db = StigmeRigDB(rig_dir=repo / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    targets_file = repo / "targets.json"
    targets_file.write_text(
        json.dumps({"platform_targets": {"python": "3.12"}, "freshness": {"stale_threshold_days": 30}}),
        encoding="utf-8",
    )

    t0 = time.perf_counter()
    report = run_freshness_scan([repo], rig_dir=repo / ".stigmerig", targets_json=targets_file, repo_root=repo)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 1000.0
    assert report["stale_threshold_days"] == 30
    assert report["stale_files"] == ["old.py"]
    assert len(report["expired_annotations"]) == 1

    # Advisory exit 0 without --fail-on-expired; exit 1 with --fail-on-expired
    assert cli_main(["--rig-dir", str(repo / ".stigmerig"), "traps", "freshness", str(repo), "--targets-json", str(targets_file), "--json"]) == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["ok"] is True and out_ok["stale_files"] == ["old.py"]

    assert cli_main(["--rig-dir", str(repo / ".stigmerig"), "traps", "freshness", str(repo), "--targets-json", str(targets_file), "--fail-on-expired", "--json"]) == 1
    out_fail = json.loads(capsys.readouterr().out)
    assert out_fail["ok"] is False and len(out_fail["expired_annotations"]) == 1

    # traps scan and --baseline remain completely unaffected by @remove-when
    baseline = repo / "baseline.json"
    assert cli_main(["--rig-dir", str(repo / ".stigmerig"), "traps", "scan", str(repo), "--write-baseline", str(baseline)]) == 0
    capsys.readouterr()
    assert cli_main(["--rig-dir", str(repo / ".stigmerig"), "traps", "scan", str(repo), "--baseline", str(baseline), "--json"]) == 0
    scan_res = json.loads(capsys.readouterr().out)
    assert scan_res["violations"] == []


def test_parse_diffstat_metrics_variants_and_errors() -> None:
    assert parse_diffstat_metrics("added=10, deleted=4, files_deleted=1") == {
        "lines_added": 10,
        "lines_deleted": 4,
        "net_loc_delta": 6,
        "files_deleted": 1,
    }
    assert parse_diffstat_metrics("2 files changed, 3 insertions(+), 9 deletions(-)") == {
        "lines_added": 3,
        "lines_deleted": 9,
        "net_loc_delta": -6,
        "files_deleted": 0,
    }
    assert parse_diffstat_metrics("1 file changed, 4 deletions(-), files_deleted=1") == {
        "lines_added": 0,
        "lines_deleted": 4,
        "net_loc_delta": -4,
        "files_deleted": 1,
    }
    assert parse_diffstat_metrics("5\t12\t{old => new}/mod.py\n-\t-\tasset.png") == {
        "lines_added": 5,
        "lines_deleted": 12,
        "net_loc_delta": -7,
        "files_deleted": 0,
    }
    assert parse_diffstat_metrics("-\t-\tlogo.png") == {
        "lines_added": 0,
        "lines_deleted": 0,
        "net_loc_delta": 0,
        "files_deleted": 0,
    }

    for bad in ("", "   ", "added=10", "added=-1,deleted=0", "garbage string"):
        with pytest.raises(ValueError):
            parse_diffstat_metrics(bad)


def test_cli_done_review_approve_forwarding_and_telemetry_dedup(tmp_path: Path, capsys) -> None:
    rig_dir = tmp_path / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig(scaffold_icm=False)
    db.create_task(task_id="TSK-1", title="Shrink code", author="Arch")
    db.create_task(task_id="TSK-2", title="Zero net delta", author="Arch")
    db.create_task(task_id="TSK-3", title="CLI done diffstat", author="Arch")

    # Reject invalid --diffstat upfront on task done
    assert cli_main(["--rig-dir", str(rig_dir), "task", "done", "--id", "TSK-3", "--agent", "A1", "--verifier", "V1", "--diffstat", "invalid"]) == 1
    capsys.readouterr()

    assert cli_main(["--rig-dir", str(rig_dir), "task", "claim", "--id", "TSK-3", "--agent", "A1"]) == 0
    assert cli_main(["--rig-dir", str(rig_dir), "task", "done", "--id", "TSK-3", "--agent", "A1", "--verifier", "V1", "--diffstat", "added=2,deleted=8,files_deleted=1"]) == 0
    capsys.readouterr()

    # Rework review preserves free-form review diffstat on old review and uses latest review on approve
    assert db.claim_task("TSK-1", "A1") is True
    assert db.submit_for_review("TSK-1", "A1", "first pass", diff_metrics={"lines_added": 99, "lines_deleted": 0, "files_deleted": 0})[0]
    assert db.fail_task("TSK-1", "V1", "needs cleanup") is True
    with db.get_queue_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'todo', claimed_by = NULL WHERE id = 'TSK-1'")
        conn.commit()
    assert db.claim_task("TSK-1", "A2") is True
    assert cli_main(["--rig-dir", str(rig_dir), "task", "review", "--id", "TSK-1", "--agent", "A2", "--summary", "free-form check", "--diffstat", "lots of cleanup"]) == 0
    assert db.approve_review("TSK-1", verifier="V1")[0]

    # Separate clean task where latest review carries subtractive diff_metrics into approve_review
    assert db.claim_task("TSK-2", "A1") is True
    assert cli_main(["--rig-dir", str(rig_dir), "task", "review", "--id", "TSK-2", "--agent", "A1", "--summary", "deleted dead helper", "--diffstat", "added=1,deleted=5,files_deleted=1"]) == 0
    capsys.readouterr()
    assert db.approve_review("TSK-2", verifier="V1")[0]

    # Duplicate completed event with different metrics to verify per-task deduplication (latest wins)
    db.log_audit("TSK-3", "completed", "A1", "verified", "{}")
    with db.get_queue_conn() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, 'completed', ?, ?, ?)",
            ("TSK-3", "A1", json.dumps({"diff_metrics": {"lines_added": 5, "lines_deleted": 5, "net_loc_delta": 0, "files_deleted": 0}}), "2026-11-20T12:00:00Z"),
        )
        conn.commit()

    metrics = TelemetryExporter(db).compute_topology_and_dmv_metrics()
    # Latest TSK-3 has net_loc_delta == 0 (not subtractive); TSK-2 carried forward added=1, deleted=5, files_deleted=1
    assert metrics["lines_added_total"] == 6
    assert metrics["lines_deleted_total"] == 10
    assert metrics["net_loc_delta_total"] == -4
    assert metrics["files_deleted_total"] == 1
    assert metrics["subtractive_task_count"] == 1

    md = TelemetryExporter(db).generate_metrics_markdown()
    assert "**Subtractive Tasks (`subtractive_task_count`)**: 1" in md
    assert "`net_loc_delta_total`: `-4`" in md


def test_wave_collects_three_dot_diff_metrics_on_both_merge_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "keep.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "drop1.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    (repo / "drop2.py").write_text("m = 1\nn = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "keep.py", "drop1.py", "drop2.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)

    db = StigmeRigDB(rig_dir=repo / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    wt_mgr = WorktreeManager(repo_root=repo)
    runner = WaveRunner(db=db, worktree_mgr=wt_mgr)

    db.create_task(task_id="TSK-W1", title="Worktree commit delete", author="Arch")
    db.create_task(task_id="TSK-W2", title="Pre-committed branch delete", author="Arch")

    def act_commit_path(wt_dir: Path) -> bool:
        (wt_dir / "drop1.py").unlink()
        (wt_dir / "keep.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        # Advance main concurrently during wave to prove three-dot diff ignores main-side additions
        (repo / "concurrent_main.py").write_text("1\n2\n3\n4\n", encoding="utf-8")
        subprocess.run(["git", "add", "concurrent_main.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "concurrent"], cwd=repo, check=True, capture_output=True)
        return True

    def act_precommitted_path(wt_dir: Path) -> bool:
        (wt_dir / "drop2.py").unlink()
        subprocess.run(["git", "add", "drop2.py"], cwd=wt_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "precommit drop2"], cwd=wt_dir, check=True, capture_output=True)
        return True

    ok1, msg1 = runner.run_task_in_worktree("TSK-W1", "A1", act_commit_path, verifier="V1")
    assert ok1, msg1
    ok2, msg2 = runner.run_task_in_worktree("TSK-W2", "A1", act_precommitted_path, verifier="V1")
    assert ok2, msg2

    with db.get_queue_conn() as conn:
        rows = conn.execute(
            "SELECT task_id, payload FROM task_events WHERE event_type = 'completed' ORDER BY id ASC"
        ).fetchall()
    by_tid = {r["task_id"]: json.loads(r["payload"])["diff_metrics"] for r in rows}
    assert by_tid["TSK-W1"] == {"lines_added": 1, "lines_deleted": 3, "net_loc_delta": -2, "files_deleted": 1}
    assert by_tid["TSK-W2"] == {"lines_added": 0, "lines_deleted": 2, "net_loc_delta": -2, "files_deleted": 1}

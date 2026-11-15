"""Hermetic hardening tests for TelemetryExporter payloads, timestamps, and table escaping."""
import datetime, json, re
from pathlib import Path
from stigmerig.db import StigmeRigDB
from stigmerig.telemetry import TelemetryExporter, parse_iso


def test_mermaid_non_dict_payloads_whitespace_verifier_and_sanitization(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    events = [
        ("TSK-1", "created", 'Lead "Alpha"\n1', "null", "2026-03-01T10:00:00Z"),
        ("TSK-1", "claimed", "WorkerA", "[1, 2, 3]", "2026-03-01T10:01:00Z"),
        ("TSK-1", "blocked", "WorkerA", '"raw_string"', "2026-03-01T10:02:00Z"),
        ("TSK-1", "blocked", "WorkerA", "42", "2026-03-01T10:03:00Z"),
        ("TSK-1", "blocked", "WorkerA", json.dumps({"reason": 'Lock "bad"\nline1\r\nline2'}), "2026-03-01T10:04:00Z"),
        ("TSK-1", "completed", "WorkerA", json.dumps({"verifier": "  \t\n "}), "2026-03-01T10:05:00Z"),
        ("TSK-2", "completed", "WorkerA", json.dumps({"verifier": '  QA "Bot"\n2  '}), "2026-03-01T10:06:00Z"),
    ]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, ?, ?, ?)", events)
        conn.commit()
    lines = TelemetryExporter(db).generate_mermaid_sequence().splitlines()
    assert all("\r" not in line and line.count('"') in (0, 2) for line in lines)
    assert '    actor Lead__Alpha__1 as "Lead \'Alpha\' 1"' in lines and '    actor QA__Bot__2 as "QA \'Bot\' 2"' in lines
    assert "    WorkerA->>System: Completed TSK-1" in lines and not any("Submit TSK-1 for review" in l for l in lines)
    assert "    WorkerA->>QA__Bot__2: Submit TSK-2 for review" in lines and "    WorkerA->>System: Report blocked TSK-1 (unknown)" in lines
    assert "    WorkerA->>System: Report blocked TSK-1 (Lock 'bad' line1 line2)" in lines


def test_parse_iso_and_mixed_naive_utc_metrics(tmp_path: Path) -> None:
    naive_sql = parse_iso("2026-03-01 10:00:00")
    assert naive_sql.tzinfo == datetime.timezone.utc and naive_sql == parse_iso("2026-03-01T10:00:00") == parse_iso("2026-03-01T10:00:00Z")
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    tasks = [("EPIC-1", "Epic", "2026-03-01 10:00:00", "2026-03-01T10:20:00Z", None), ("TSK-A", "A", "2026-03-01 10:00:00", "2026-03-01T10:05:00Z", "EPIC-1"), ("TSK-B", "B", "2026-03-01T10:06:00Z", "2026-03-01 10:12:00", "EPIC-1")]
    evs = [("TSK-A", "created", "2026-03-01 10:00:00"), ("TSK-A", "claimed", "2026-03-01T10:01:00Z"), ("TSK-A", "completed", "2026-03-01T10:05:00"), ("TSK-B", "created", "2026-03-01T10:06:00Z"), ("TSK-B", "completed", "2026-03-01 10:12:00")]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype) VALUES (?, ?, 'done', 100, ?, ?, ?, 'diff_synthesizer')", tasks)
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, 'WorkerA', '{}', ?)", evs)
        conn.commit()
    exporter = TelemetryExporter(db)
    assert {d["task_id"]: d for d in exporter.compute_duration_metrics()}["TSK-A"] == {"task_id": "TSK-A", "wait_seconds": 60.0, "exec_seconds": 240.0, "total_seconds": 300.0}
    gov = exporter.compute_topology_and_dmv_metrics()
    assert gov["completed_tasks"] == 2 and gov["epics"]["EPIC-1"]["topology_class"] == "linear"


def test_metrics_markdown_escapes_pipes_and_newlines_in_traps(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    for idx in range(1, 4):
        db.create_task(f"TSK-T{idx}", f"Trap {idx}")
        db.fail_task(f"TSK-T{idx}", agent="WorkerA", reason="Schema lock | step 1\nstep 2\r\nstep 3")
    doc = json.loads((db.rig_dir / "traps.json").read_text(encoding="utf-8"))
    doc["traps"].append({"id": "TRAP-002|INJ\nX", "occurrences": 4, "trigger_pattern": "pipe|trig\npat", "reason": "Injected | col\nbreak", "status": "candidate"})
    (db.rig_dir / "traps.json").write_text(json.dumps(doc), encoding="utf-8")
    md = TelemetryExporter(db).generate_metrics_markdown()
    trap_rows = [line for line in md.splitlines() if line.startswith("| `TRAP-")]
    assert "\r" not in md and len(trap_rows) == 2 and all(r"\|" in row and len(re.findall(r"(?<!\\)\|", row)) == 5 for row in trap_rows)

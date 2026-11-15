"""Hermetic tests for TelemetryExporter schema guards, timestamp parsing, self-parent cycles, and markdown escaping."""
import json, re, sqlite3
from pathlib import Path
import pytest
from stigmerig.cli import main
from stigmerig.db import StigmeRigDB
from stigmerig.telemetry import TelemetryExporter


def test_get_events_missing_task_id_column_and_blank_rows(tmp_path: Path) -> None:
    (tmp_path / "nc").mkdir()
    with sqlite3.connect(tmp_path / "nc" / "queue.db") as conn:
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, event_type TEXT, agent TEXT, timestamp TEXT)")
        conn.execute("INSERT INTO task_events VALUES (1, 'created', 'W1', '2026-03-01T10:00:00Z')")
    exp_nc = TelemetryExporter(StigmeRigDB(rig_dir=tmp_path / "nc"))
    assert exp_nc.get_events() == [] and exp_nc.get_events("T1") == [] and exp_nc.compute_duration_metrics() == []
    assert "No events recorded" in exp_nc.generate_mermaid_sequence()
    (tmp_path / "bl").mkdir()
    with sqlite3.connect(tmp_path / "bl" / "queue.db") as conn:
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, event_type TEXT, agent TEXT, timestamp TEXT)")
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, timestamp) VALUES (?, 'created', 'W1', '2026-03-01T10:00:00Z')", [(None,), ("",), ("  \t\n ",), ("TSK-OK",)])
    exp_bl = TelemetryExporter(StigmeRigDB(rig_dir=tmp_path / "bl"))
    assert [e["task_id"] for e in exp_bl.get_events()] == ["TSK-OK"] == [m["task_id"] for m in exp_bl.compute_duration_metrics()]
    assert "Create TSK-OK" in exp_bl.generate_mermaid_sequence()


def test_compute_duration_metrics_unparseable_and_missing_timestamps(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    evs = [("TSK-BAD", "created", "2026-03-01T10:00:00Z"), ("TSK-BAD", "claimed", "not-iso"), ("TSK-BAD", "completed", ""), ("TSK-PART", "created", "bad"), ("TSK-PART", "claimed", "2026-03-01T10:01:00Z"), ("TSK-PART", "completed", "2026-03-01T10:05:00Z")]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, 'W1', '{}', ?)", evs)
        conn.commit()
    by_id = {m["task_id"]: m for m in TelemetryExporter(db).compute_duration_metrics()}
    assert by_id["TSK-BAD"] == {"task_id": "TSK-BAD", "wait_seconds": None, "exec_seconds": None, "total_seconds": None}
    assert by_id["TSK-PART"] == {"task_id": "TSK-PART", "wait_seconds": None, "exec_seconds": 240.0, "total_seconds": None}


def test_self_parent_cycles_and_json_force_claimed_rework(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    t0 = "2026-03-01T10:00:00Z"
    tasks = [("EPIC-1", "E", t0, t0, "EPIC-1", "stigmerig_coordinator"), ("TSK-C1", "C", t0, t0, "EPIC-1", "diff_synthesizer"), ("TSK-SELF", "S", t0, t0, "TSK-SELF", "diff_synthesizer"), ("TSK-JR", "R", t0, t0, None, "diff_synthesizer"), ("TSK-JM", "M", t0, t0, None, "diff_synthesizer"), ("TSK-PL", "P", t0, t0, None, "diff_synthesizer")]
    evs = [("TSK-JR", "claimed", json.dumps({"reason": "Force-claimed expired lease"})), ("TSK-JM", "claimed", json.dumps({"message": "Force-claimed stale worker"})), ("TSK-PL", "claimed", "Force-claimed plain string"), ("TSK-C1", "claimed", json.dumps({"other": "Force-claimed", "reason": "Normal claim"}))]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype) VALUES (?, ?, 'working', 10, ?, ?, ?, ?)", tasks)
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, 'W1', ?, '2026-03-01T10:01:00Z')", evs)
        conn.commit()
    gov = TelemetryExporter(db).compute_topology_and_dmv_metrics()
    assert (gov["total_records"], gov["total_tasks"], set(gov["epics"]), gov["rework_tasks"], gov["rework_rate"]) == (6, 5, {"EPIC-1"}, 3, 0.6)


def test_metrics_markdown_escapes_task_epic_and_topology_cells(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    tid, eid = "TSK-1|INJ\r\nL2`X", "EPIC-1|BAD\nE2`Y"
    with db.get_queue_conn() as conn:
        conn.execute("INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype) VALUES (?, 'T', 'done', 10, '2026-03-01T10:00:00Z', '2026-03-01T10:02:00Z', ?, 'diff_synthesizer')", (tid, eid))
        conn.execute("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, 'created', 'W1', '{}', '2026-03-01T10:00:00Z')", (tid,))
        conn.commit()
    exporter = TelemetryExporter(db)
    orig_gov = exporter.compute_topology_and_dmv_metrics()
    orig_gov["epics"][eid]["topology_class"] = "single|inj\n`cls"
    monkeypatch.setattr(exporter, "compute_topology_and_dmv_metrics", lambda: orig_gov)
    md = exporter.generate_metrics_markdown()
    t_row, e_row = next(l for l in md.splitlines() if "TSK-1" in l), next(l for l in md.splitlines() if "EPIC-1" in l)
    assert "\r" not in md and t_row.startswith(r"| `TSK-1\|INJ L2'X` |") and len(re.findall(r"(?<!\\)\|", t_row)) == 5 and t_row.count("`") == 2
    assert e_row.startswith(r"| `EPIC-1\|BAD E2'Y` |") and e_row.endswith(r"| `single\|inj 'cls` |") and len(re.findall(r"(?<!\\)\|", e_row)) == 6 and e_row.count("`") == 4


def test_mermaid_actor_collisions_symbol_fallbacks_and_task_filter(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    evs = [
        ("TSK-1", "created", "qa-bot", "{}", "2026-03-01T10:00:00Z"), ("TSK-1", "claimed", "qa:bot", "{}", "2026-03-01T10:01:00Z"),
        ("TSK-1", "lease_renewed", "!!!", "{}", "2026-03-01T10:02:00Z"), ("TSK-1", "completed", "qa-bot", json.dumps({"verifier": "qa-bot"}), "2026-03-01T10:03:00Z"),
        ("TSK-2", "claimed", "qa/bot", "{}", "2026-03-01T10:04:00Z"), ("TSK-2", "custom_check", "???", "{}", "2026-03-01T10:05:00Z"),
        ("TSK-2", "completed", "qa_bot_1", json.dumps({"verifier": "qa:bot"}), "2026-03-01T10:06:00Z"),
    ]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, ?, ?, ?)", evs)
        conn.commit()
    exp = TelemetryExporter(db)
    full, t1_seq = exp.generate_mermaid_sequence().splitlines(), exp.generate_mermaid_sequence(task_id="TSK-1")
    assert [l.split()[1] for l in full if l.startswith("    actor ")] == ["System", "qa_bot", "qa_bot_1", "actor", "qa_bot_2", "actor_1", "qa_bot_1_1"]
    assert {'    actor qa_bot as "qa-bot"', '    actor qa_bot_1 as "qa:bot"', '    actor qa_bot_2 as "qa/bot"', "    actor qa_bot_1_1", '    actor actor as "!!!"', '    actor actor_1 as "???"'} <= set(full)
    assert "    qa_bot->>System: Completed TSK-1" in full and not any("TSK-1 for review" in l for l in full)
    assert {"    qa_bot_1_1->>qa_bot_1: Submit TSK-2 for review", "    qa_bot_1->>System: Verified TSK-2", "    actor->>System: lease_renewed TSK-1", "    actor_1->>System: custom_check TSK-2"} <= set(full)
    assert "Create TSK-1" in t1_seq and "lease_renewed TSK-1" in t1_seq and "Completed TSK-1" in t1_seq and "TSK-2" not in t1_seq and "qa/bot" not in t1_seq


def test_single_epic_dmv_boundary_ceremony_and_dismissed_traps(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    t0, t1 = "2026-03-01T10:00:00Z", "2026-03-01T10:05:00Z"
    tasks = [("EPC-SOLO", "Solo", t0, t1, None, "stigmerig_coordinator"), ("TSK-V0", "V0", t0, t1, "EPC-SOLO", "unit_tester"), ("TSK-V1", "V1", t0, t1, None, "prose_auditor"), ("TSK-V2", "V2", t0, t1, None, "policy_gatekeeper"), *[(f"TSK-D{i}", f"D{i}", t0, t1, None, "diff_synthesizer") for i in range(7)]]
    traps = [{"id": "TRAP-2", "occurrences": 2, "status": "candidate", "reason": "low"}, {"id": "TRAP-3", "occurrences": 3, "status": "candidate", "reason": "hit"}, {"id": "TRAP-DIS", "occurrences": 6, "status": "DISMISSED", "reason": "upper"}, {"id": "TRAP-PAD", "occurrences": 4, "status": " Dismissed ", "reason": "padded"}]
    (db.rig_dir / "traps.json").write_text(json.dumps({"traps": traps}), encoding="utf-8")
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype) VALUES (?, ?, 'done', 10, ?, ?, ?, ?)", tasks)
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, 'SELF_EXECUTION_OVERRIDE', 'W1', '{}', ?)", [("TSK-V0", t0), ("TSK-D0", t0)])
        conn.commit()
    exp = TelemetryExporter(db)
    gov = exp.compute_topology_and_dmv_metrics()
    assert gov["epics"]["EPC-SOLO"] == {"total_tasks": 1, "phase_depth": 1, "max_wave_width": 1, "topology_class": "single"}
    assert (gov["verify_only_tasks"], gov["total_tasks"], gov["verify_only_ratio"], gov["dmv_alert"], gov["self_execution_overrides"], gov["ceremony_ratio"]) == (3, 10, 0.30, False, 2, 0.50)
    assert gov["blocked_reasons"]["total_traps"] == 4 and [t["id"] for t in gov["blocked_reasons"]["recurring_candidates"]] == ["TRAP-3"]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype) VALUES (?, 'Extra', 'done', 10, ?, ?, NULL, ?)", [("TSK-V3", t0, t1, "topology_reader"), ("TSK-D7", t0, t1, "diff_synthesizer"), ("TSK-D8", t0, t1, "diff_synthesizer")])
        conn.commit()
    gov_hi = exp.compute_topology_and_dmv_metrics()
    assert (gov_hi["verify_only_ratio"], gov_hi["dmv_alert"], gov_hi["ceremony_ratio"]) == (0.3077, True, 0.4615)


def test_300_event_batch_cli_metrics_json_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    def iso(sec: int) -> str: return f"2026-03-01T10:{sec // 60:02d}:{sec % 60:02d}Z"
    tasks = [("EPC-B1", "E1", "done", iso(0), iso(600), None, "stigmerig_coordinator"), ("EPC-B2", "E2", "done", iso(0), iso(1100), None, "stigmerig_coordinator"), *[(f"TSK-B{i:03d}", f"T{i}", "blocked" if i >= 95 else "done", iso(i * 10), iso(i * 10 + 7), "EPC-B1" if i < 50 else ("EPC-B2" if i < 99 else None), "unit_tester" if i % 5 == 0 else "diff_synthesizer") for i in range(100)]]
    evs = [ev for i in range(100) for ev in ((f"TSK-B{i:03d}", "created", f"Lead{i % 3}", "{}", iso(i * 10)), (f"TSK-B{i:03d}", "claimed", f"W{i % 4}", "{}", iso(i * 10 + 2)), (f"TSK-B{i:03d}", "blocked" if i >= 95 else "completed", f"W{i % 4}", json.dumps({"reason": "dep"} if i >= 95 else {"verifier": "Rev"}), iso(i * 10 + 7)))]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype) VALUES (?, ?, ?, 10, ?, ?, ?, ?)", tasks)
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, ?, ?, ?)", evs)
        conn.commit()
    exp = TelemetryExporter(db)
    direct, out_file = exp.export_metrics_json(), tmp_path / "metrics_300.json"
    capsys.readouterr()
    assert len(evs) == 300 == len(exp.get_events()) and len(direct["durations"]) == 100
    assert main(["--rig-dir", str(db.rig_dir), "telemetry", "metrics", "--json"]) == 0 and json.loads(capsys.readouterr().out) == direct
    assert main(["--rig-dir", str(db.rig_dir), "telemetry", "metrics", "--json", "--output", str(out_file)]) == 0 and json.loads(out_file.read_text(encoding="utf-8")) == direct

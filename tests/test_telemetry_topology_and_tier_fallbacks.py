"""Hermetic tests for claim-window wave topology, hybrid Tier-A/B fallbacks, and blocked reasons."""
import datetime, sqlite3
from pathlib import Path
import pytest
import stigmerig.telemetry as telemetry_mod
from stigmerig.db import StigmeRigDB
from stigmerig.telemetry import TelemetryExporter, _try_parse_iso, parse_iso


def test_claim_window_wave_topology_shapes_and_unclaimed_fallback(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    t0 = "2026-03-01T10:00:00Z"
    tasks = [
        ("EPC-LIN", "L", t0, "2026-03-01T10:15:00Z", None, "stigmerig_coordinator"),
        ("L1", "1", t0, "2026-03-01T10:04:00Z", "EPC-LIN", "diff_synthesizer"), ("L2", "2", t0, "2026-03-01T10:08:00Z", "EPC-LIN", "unit_tester"), ("L3", "3", t0, "2026-03-01T10:12:00Z", "EPC-LIN", "policy_gatekeeper"),
        ("EPC-DAG", "D", t0, "2026-03-01T10:12:00Z", None, "stigmerig_coordinator"),
        ("D1", "1", t0, "2026-03-01T10:05:00Z", "EPC-DAG", "diff_synthesizer"), ("D2", "2", t0, "2026-03-01T10:06:00Z", "EPC-DAG", "diff_synthesizer"), ("D3", "3", t0, "2026-03-01T10:10:00Z", "EPC-DAG", "unit_tester"),
        ("F1", "U1", t0, "2026-03-01T10:04:00Z", "EPC-FB", "diff_synthesizer"), ("F2", "U2", "2026-03-01T10:05:00Z", "2026-03-01T10:09:00Z", "EPC-FB", "diff_synthesizer"),
    ]
    evs = [
        ("L1", "claimed", "2026-03-01T09:55:00Z"), ("L1", "claimed", "2026-03-01T10:01:00Z"), ("L1", "claimed", "2026-03-01T10:02:00Z"), ("L1", "completed", "2026-03-01T10:04:00Z"),
        ("L2", "claimed", "2026-03-01T10:05:00Z"), ("L2", "completed", "2026-03-01T10:08:00Z"), ("L3", "claimed", "2026-03-01T10:09:00Z"), ("L3", "completed", "2026-03-01T10:12:00Z"),
        ("D1", "claimed", "2026-03-01T10:01:00Z"), ("D1", "completed", "2026-03-01T10:05:00Z"), ("D2", "claimed", "2026-03-01T10:02:00Z"), ("D2", "completed", "2026-03-01T10:06:00Z"),
        ("D3", "claimed", "2026-03-01T10:06:00Z"), ("D3", "completed", "2026-03-01T10:10:00Z"), ("F2", "claimed", "2026-03-01T09:50:00Z"),
    ]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, priority, created_at, updated_at, parent_id, archetype) VALUES (?, ?, 'done', 10, ?, ?, ?, ?)", tasks)
        conn.executemany("INSERT INTO task_events (task_id, event_type, agent, payload, timestamp) VALUES (?, ?, 'W1', '{}', ?)", evs)
        conn.commit()
    epics = TelemetryExporter(db).compute_topology_and_dmv_metrics()["epics"]
    assert epics["EPC-LIN"] == {"total_tasks": 3, "phase_depth": 3, "max_wave_width": 1, "topology_class": "linear"}
    assert epics["EPC-DAG"] == {"total_tasks": 3, "phase_depth": 2, "max_wave_width": 2, "topology_class": "hybrid_dag"}
    assert epics["EPC-FB"] == {"total_tasks": 2, "phase_depth": 2, "max_wave_width": 1, "topology_class": "linear"}


def test_hybrid_tier_ab_fallbacks_missing_tables_and_blocked_reasons(tmp_path: Path) -> None:
    empty_exp = TelemetryExporter(StigmeRigDB(rig_dir=tmp_path / "missing"))
    assert empty_exp.get_events() == [] and empty_exp.compute_topology_and_dmv_metrics()["total_tasks"] == 0
    (tmp_path / "to").mkdir()
    with sqlite3.connect(tmp_path / "to" / "queue.db") as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, created_at TEXT, updated_at TEXT, parent_id TEXT, archetype TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?, 'T', 'done', '2026-03-01T10:00:00Z', '2026-03-01T10:05:00Z', NULL, ?)", [("EPC-EMPTY", "stigmerig_coordinator"), ("T1", "diff_synthesizer"), ("T2", "unit_tester")])
    to_exp = TelemetryExporter(StigmeRigDB(rig_dir=tmp_path / "to"))
    to_gov = to_exp.compute_topology_and_dmv_metrics()
    assert to_exp.get_events() == [] and (to_gov["total_records"], to_gov["total_tasks"], to_gov["verify_only_ratio"]) == (3, 2, 0.5)
    (tmp_path / "hyb").mkdir()
    with sqlite3.connect(tmp_path / "hyb" / "queue.db") as conn:
        conn.execute("CREATE TABLE task_events (event_id INTEGER PRIMARY KEY, id INTEGER, task_id TEXT, event_type TEXT, actor TEXT, agent TEXT, message TEXT, payload TEXT, created_at TEXT, timestamp TEXT)")
        conn.executemany("INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(1, None, "T1", "AGENT CLAIMED", "LegActor", None, "Force-claimed", "", "2026-03-01T10:01:00Z", "  "), (2, 2, "T1", "claimed", "OldActor", "NewAgent", "old", '{"ok":true}', "2026-03-01T09:00:00Z", "2026-03-01T10:02:00Z"), *[(i + 3, None, "T1", "BLOCKED", "LegActor", "", p, None, "2026-03-01T10:03:00Z", None) for i, p in enumerate(["null", "true", "false", "0", "42", "[1]", "{}", '{"reason": null}', '{"reason": "lock_err"}', "plain_err"])]])
    hyb_exp = TelemetryExporter(StigmeRigDB(rig_dir=tmp_path / "hyb"))
    evs = hyb_exp.get_events("T1")
    assert (evs[0]["id"], evs[0]["agent"], evs[0]["payload"], evs[0]["timestamp"], evs[1]["agent"], evs[1]["payload"], evs[1]["timestamp"]) == (1, "LegActor", "Force-claimed", "2026-03-01T10:01:00Z", "NewAgent", '{"ok":true}', "2026-03-01T10:02:00Z")
    assert hyb_exp.compute_topology_and_dmv_metrics()["blocked_reasons"]["by_reason"] == {"unspecified": 8, "lock_err": 1, "plain_err": 1}


def test_single_scan_iso_slicing_tier_a_task_id_and_message_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_mod.re, "sub", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("regex used")))
    off_dt = parse_iso("2026-03-01T05:00:00-05:00")
    assert off_dt.utcoffset() == datetime.timedelta(hours=-5) and off_dt == parse_iso("2026-03-01T10:00:00z") == _try_parse_iso("2026-03-01 10:00:00")
    assert _try_parse_iso("2026-03-01T10:00:00Z") == off_dt and _try_parse_iso("   ") is None
    (tmp_path / "ta").mkdir()
    with sqlite3.connect(tmp_path / "ta" / "queue.db") as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT, title TEXT, status TEXT, type TEXT, created_at TEXT, updated_at TEXT, parent_id TEXT, archetype TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?, 'T', ?, ?, '2026-03-01T10:00:00Z', '2026-03-01T10:06:00Z', ?, ?)", [
            ("EPC-TA", "done", "epic", None, "diff_synthesizer"), ("TA-1", "working", "task", "EPC-TA", "diff_synthesizer"),
            ("TA-2", "blocked", "task", "EPC-TA", "unit_tester"), (None, "done", "task", "EPC-TA", "diff_synthesizer"), ("  \t\n ", "done", "task", None, "unit_tester"),
        ])
        conn.execute("CREATE TABLE task_events (event_id INTEGER PRIMARY KEY, task_id TEXT, event_type TEXT, actor TEXT, message TEXT, payload TEXT, created_at TEXT)")
        conn.executemany("INSERT INTO task_events (task_id, event_type, actor, message, payload, created_at) VALUES (?, ?, 'W1', ?, ?, ?)", [
            ("TA-1", "AGENT CREATED", "", "{}", "2026-03-01T10:00:00z"), ("TA-1", "AGENT CLAIMED", "", "{}", "2026-03-01T10:01:00Z"),
            ("TA-1", "AGENT DONE", "", "{}", "2026-03-01T10:04:00Z"), ("TA-2", "CLAIMED", "", "{}", "2026-03-01T10:04:30Z"), ("TA-2", "BLOCKED", "msg_plain", "{}", "2026-03-01T10:05:00Z"),
            ("TA-2", "BLOCKED", '{"reason": "msg_json"}', " { } ", "2026-03-01T10:05:10Z"), ("TA-2", "BLOCKED", "msg_empty_p", "", "2026-03-01T10:05:20Z"),
            ("TA-2", "BLOCKED", "ignored", '{"reason": "payload_wins"}', "2026-03-01T10:05:30Z"), ("TA-2", "BLOCKED", "   ", "{}", "2026-03-01T10:05:40Z"),
        ])
    exp = TelemetryExporter(StigmeRigDB(rig_dir=tmp_path / "ta"))
    scans, parses, orig_get = [], [], exp.get_events
    monkeypatch.setattr(exp, "get_events", lambda tid=None: (scans.append(1), orig_get(tid))[1])
    monkeypatch.setattr(telemetry_mod, "_try_parse_iso", lambda v: (parses.append(1), _try_parse_iso(v))[1])
    gov = exp.compute_topology_and_dmv_metrics()
    assert (len(scans), len(parses), gov["total_records"], gov["total_tasks"], gov["completed_tasks"], gov["rework_tasks"], gov["verify_only_ratio"]) == (1, 13, 3, 2, 1, 1, 0.5)
    assert gov["epics"]["EPC-TA"]["topology_class"] == "linear" and gov["blocked_reasons"]["by_reason"] == {"msg_plain": 1, "msg_json": 1, "msg_empty_p": 1, "payload_wins": 1, "unspecified": 1}
    exported = exp.export_metrics_json()
    assert len(scans) == 2 and exported["durations"][0] == {"task_id": "TA-1", "wait_seconds": 60.0, "exec_seconds": 180.0, "total_seconds": 240.0}
    monkeypatch.setattr(exp, "compute_topology_and_dmv_metrics", lambda: gov)
    assert "EPC-TA" in exp.generate_metrics_markdown() and len(scans) == 3


def test_markdown_single_scan_get_events_trim_and_tier_a_normalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "norm").mkdir()
    with sqlite3.connect(tmp_path / "norm" / "queue.db") as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT, title TEXT, status TEXT, type TEXT, created_at TEXT, updated_at TEXT, parent_id TEXT, archetype TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?, 'T', ?, ?, '2026-03-01T10:00:00Z', '2026-03-01T10:05:00Z', ?, ?)", [
            ("  EPC-A  ", "todo", "epic", None, "stigmerig_coordinator"),
            ("EPC-A", "completed", "epic", None, "stigmerig_coordinator"),
            ("T-CMP", "working", "task", "EPC-A", "diff_synthesizer"),
            ("  T-CMP  ", "completed", "task", "EPC-A", "UNIT_TESTER"),
            ("T-TD", " task_done ", "task", "EPC-A", "Prose_Auditor"),
            ("T-TC", "TASK_COMPLETED", "task", "EPC-A", "  Policy_Gatekeeper  "),
            ("T-AD", "  Agent   Done  ", "task", "EPC-A", "Topology_Reader"),
            ("T-OPEN", "working", "task", "EPC-A", "Diff_Synthesizer"),
        ])
    exp = TelemetryExporter(StigmeRigDB(rig_dir=tmp_path / "norm"))
    gov_no_ev = exp.compute_topology_and_dmv_metrics()
    assert (gov_no_ev["total_records"], gov_no_ev["total_tasks"], gov_no_ev["completed_tasks"], gov_no_ev["verify_only_tasks"], gov_no_ev["verify_only_ratio"], gov_no_ev["dmv_alert"]) == (6, 5, 4, 4, 0.8, True)
    assert gov_no_ev["epics"]["EPC-A"]["total_tasks"] == 5

    with sqlite3.connect(tmp_path / "norm" / "queue.db") as conn:
        conn.execute("CREATE TABLE task_events (event_id INTEGER PRIMARY KEY, task_id TEXT, event_type TEXT, actor TEXT, message TEXT, created_at TEXT)")
        conn.executemany("INSERT INTO task_events (task_id, event_type, actor, message, created_at) VALUES (?, ?, 'W1', '{}', ?)", [
            ("  T-CMP  ", "CREATED", "2026-03-01T10:00:00Z"),
            ("T-CMP", "CLAIMED", "2026-03-01T10:01:00Z"),
            ("\tT-CMP\n", "COMPLETED", "2026-03-01T10:04:00Z"),
            ("  T-TD ", "CREATED", "2026-03-01T10:00:00Z"),
        ])
    assert exp.get_events("") == [] and exp.get_events("   ") == [] and exp.get_events("\t\n") == []
    assert [e["task_id"] for e in exp.get_events()] == ["T-CMP", "T-CMP", "T-CMP", "T-TD"]
    assert [e["event_type"] for e in exp.get_events("  T-CMP  ")] == ["created", "claimed", "completed"] == [e["event_type"] for e in exp.get_events("T-CMP")]

    scans, dur_seen, gov_seen = [], [], []
    orig_get, orig_dur, orig_gov = exp.get_events, exp.compute_duration_metrics, exp.compute_topology_and_dmv_metrics
    monkeypatch.setattr(exp, "get_events", lambda tid=None: (scans.append(tid), orig_get(tid))[1])
    monkeypatch.setattr(exp, "compute_duration_metrics", lambda evs=None: (dur_seen.append(evs), orig_dur(evs))[1])
    monkeypatch.setattr(exp, "compute_topology_and_dmv_metrics", lambda evs=None: (gov_seen.append(evs), orig_gov(evs))[1])

    md = exp.generate_metrics_markdown()
    assert scans == [None] and len(dur_seen) == 1 and len(gov_seen) == 1 and dur_seen[0] is gov_seen[0]
    assert "| `T-CMP` | 60.0 | 180.0 | 240.0 |" in md and "4 / 5 completed (6 total records)" in md
    assert exp.generate_metrics_markdown(events=dur_seen[0]) == md and len(scans) == 1
    md_empty = exp.generate_metrics_markdown(events=[])
    assert len(scans) == 1 and "| `T-CMP` |" not in md_empty and "4 / 5 completed (6 total records)" in md_empty

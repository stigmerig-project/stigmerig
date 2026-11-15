"""Hermetic tests for DB upfront validation, evidence/trap guards, and WORM audit table checks."""
import concurrent.futures, hashlib, json, os, sqlite3, threading
from pathlib import Path
import pytest
from stigmerig.db import GENESIS_HASH, StigmeRigDB, _validate_audit_header_fields

def test_create_and_fail_upfront_validation_prevents_split_brain(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "ws" / "custom_rig"); db.init_rig(scaffold_icm=False)
    for tid, title, pid, author, arch, prio in [("T1", "", None, None, "x", 1), ("T1", "  ", None, None, "x", 1), ("T1", "a\x00b", None, None, "x", 1), ("T1", "Ok", None, None, "", 1), ("T1", "Ok", None, None, " \t", 1), ("T1", "Ok", None, None, "a\nb", 1), ("T1", "Ok", None, None, "x", True), ("T1", "Ok", None, None, "x", False), ("T1", "Ok", None, None, "x", 1 << 63), ("T1", "Ok", None, None, "x", -(1 << 63) - 1), ("T1", "Ok", "", None, "x", 1), ("T1", "Ok", "../bad", None, "x", 1), ("T1", "Ok", "T1", None, "x", 1), ("T1", "Ok", None, "", "x", 1), ("T1", "Ok", None, " \t ", "x", 1), ("T1", "Ok", None, "ag\x00", "x", 1), ("T1", "Ok", None, "ag\n1", "x", 1), ("T1", "Ok", None, "ag\r1", "x", 1)]:
        with pytest.raises(ValueError): db.create_task(tid, title, parent_id=pid, author=author, archetype=arch, priority=prio)  # type: ignore[arg-type]
    assert db.get_task("T1") is None and not db.get_task_file("T1").exists() and db.verify_audit_chain() == (True, "Audit log is empty")
    db.create_task("T-OK", "Valid", priority=-(1 << 63))
    for bad_ag, bad_rsn in [("", "r"), ("  ", "r"), ("a\nb", "r"), ("a\rb", "r"), ("a\x00b", "r"), (None, "r"), ("Ag", ""), ("Ag", " \n "), ("Ag", None), ("Ag", 9), ("Ag", "r\x00")]:
        with pytest.raises(ValueError): db.fail_task("T-OK", bad_ag, bad_rsn)  # type: ignore[arg-type]
    assert db.get_task("T-OK")["status"] == "todo" and not (db.rig_dir / "traps.json").exists() and db.claim_task("T-OK", "Worker|Special\nSeat") and db.complete_task("T-OK", "Worker|Special\nSeat", verifier="Rev1")[0] and db.verify_audit_chain()[0]

def test_evidence_guards_and_trap_corruption_normalization(tmp_path: Path) -> None:
    ws, db = tmp_path / "ws", StigmeRigDB(rig_dir=tmp_path / "ws" / "my_rig"); db.init_rig(scaffold_icm=True)
    for rel in ("a", ".git/config", "sub/.git/HEAD"): (ws / rel).parent.mkdir(parents=True, exist_ok=True); (ws / rel).write_text("secret", encoding="utf-8")
    db.create_task("T-EV", "Evidence check"); assert db.claim_task("T-EV", "Worker1")
    for bad_ev in ("a", "", b"a", Path("a"), ["my_rig/rig.json"], ["my_rig/Task_Board.md"], [".git/config"], ["sub/.git/HEAD"]):
        with pytest.raises(ValueError): db.complete_task("T-EV", "Worker1", verifier="Rev1", evidence_paths=bad_ev)  # type: ignore[arg-type]
    assert db.complete_task("T-EV", "Worker1", verifier="Rev1", evidence_paths=["a"])[0] is True
    for idx, (occ, traces) in enumerate([("nan", "str-trace"), (True, 42), (False, {"x": 1}), (None, None), (-7, True)], start=1):
        (db.rig_dir / "traps.json").write_text(json.dumps({"version": "1.0.0", "traps": [{"id": "TRAP-001", "title": "Database lock timeout", "trigger_pattern": "database lock timeout", "occurrences": occ, "source_traces": traces}]}), encoding="utf-8")
        db.create_task(f"T-F{idx}", "Fail"); assert db.fail_task(f"T-F{idx}", "Worker1", "Database lock timeout") is True
        traps = json.loads((db.rig_dir / "traps.json").read_text(encoding="utf-8"))["traps"]
        assert len(traps) == 1 and type(traps[0]["occurrences"]) is int and traps[0]["occurrences"] == 1 and traps[0]["source_traces"] == [f"Task T-F{idx} (Worker1)"]

def test_audit_header_lock_missing_db_view_id_and_sequence_guards(tmp_path: Path) -> None:
    assert all(_validate_audit_header_fields(*a) is not None for ch in ("\r", "\n", "\x00") for a in [(f"T{ch}1", "a", "g", "v"), ("T1", f"a{ch}", "g", "v"), ("T1", "a", f"g{ch}", "v"), ("T1", "a", "g", f"v{ch}")])
    uninit = StigmeRigDB(rig_dir=tmp_path / "uninit")
    with pytest.raises(FileNotFoundError): uninit.log_audit("T1", "act", "Ag", "ok")
    assert not uninit.audit_db_path.exists()
    db, held, release, done = StigmeRigDB(rig_dir=tmp_path / "rig"), threading.Event(), threading.Event(), threading.Event(); db.init_rig(scaffold_icm=False)
    t1 = threading.Thread(target=lambda: (db._lock.acquire(), held.set(), release.wait(5), db._lock.release()))
    t2 = threading.Thread(target=lambda: (db.log_audit("T-1", "act", "Ag", "ok"), done.set()))
    t1.start(); held.wait(5); t2.start(); assert not done.wait(0.05); release.set(); t1.join(5); t2.join(5); assert done.is_set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(lambda i: StigmeRigDB(db.rig_dir).log_audit(f"T-{i}", "act", "Ag", "ok"), range(2, 8)))
    assert db.verify_audit_chain()[0] is True
    for bad_seq in (6, 1, 0, -2, "7", "bad", 7.5, None):
        with db.get_audit_conn() as c: c.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'audit_log'", (bad_seq,)); c.commit()
        assert db.verify_audit_chain()[0] is False
    with db.get_audit_conn() as c: c.executescript("DELETE FROM sqlite_sequence WHERE name='audit_log';")
    assert db.verify_audit_chain() == (False, "Audit sequence metadata corrupted")
    with db.get_audit_conn() as c: c.executescript("ALTER TABLE audit_log RENAME TO _raw; CREATE VIEW audit_log AS SELECT * FROM _raw;")
    assert db.verify_audit_chain() == (False, "audit_log must be a base table") and pytest.raises(sqlite3.OperationalError, db.log_audit, "T-9", "act", "Ag", "ok")
    db_id, h1 = StigmeRigDB(rig_dir=tmp_path / "bad_id"), hashlib.sha256(b"x").hexdigest(); db_id.rig_dir.mkdir()
    for bad_id in (1.0, "1", None, b"1"):
        with db_id.get_audit_conn() as c: c.executescript("DROP TABLE IF EXISTS audit_log; CREATE TABLE audit_log (id BLOB, previous_hash TEXT, hash TEXT);"); c.execute("INSERT INTO audit_log VALUES (?, ?, ?)", (bad_id, GENESIS_HASH, h1)); c.commit()
        assert db_id.verify_audit_chain()[0] is False


def test_sqlite_indexes_duplicate_task_integrity_and_terminal_board_sections(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "ws" / ".stigmerig")
    db.init_rig(scaffold_icm=False); db.init_rig(scaffold_icm=False)
    db.create_task("TSK-1", "Primary Task", parent_id="EPIC-1", priority=10)
    with db.get_queue_conn() as conn:
        idx_cols = {(t, tuple(r["name"] for r in conn.execute(f"PRAGMA index_info('{i['name']}')").fetchall())) for t in ("tasks", "task_events") for i in conn.execute(f"PRAGMA index_list('{t}')").fetchall()}
        assert {("tasks", ("status", "priority", "created_at", "id")), ("tasks", ("parent_id", "status")), ("task_events", ("task_id", "id"))} <= idx_cols
        for sql, params in [("SELECT * FROM tasks WHERE 1=1 AND status = ? ORDER BY priority ASC, created_at ASC, id ASC", ("todo",)), ("SELECT id, status FROM tasks WHERE parent_id = ? AND status NOT IN ('done', 'cancelled', 'canceled', 'archived')", ("EPIC-1",)), ("SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", ("TSK-1",))]:
            plan = " | ".join(r["detail"] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall())
            assert "USING INDEX" in plan and "TEMP B-TREE" not in plan
        conn.execute("INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES ('TSK-RAW', 'Raw', 'todo', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"); conn.commit()
    for tid in ("TSK-1", "TSK-RAW"):
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            db.create_task(tid, "Collision Title")
        assert str(exc_info.value) == f"Task '{tid}' already exists"
    with db.get_queue_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1
    assert db.get_task("TSK-1")["title"] == "Primary Task" and "Primary Task" in db.get_task_file("TSK-1").read_text(encoding="utf-8")
    assert not db.get_task_file("TSK-RAW").exists() and db.verify_audit_chain() == (True, "Verified 1 audit entries with intact hash chain")
    baseline = [ln for ln in db.sync_board().read_text(encoding="utf-8").splitlines() if not ln.startswith("*Last synchronized:")]
    assert not any("Cancelled / Archived" in ln for ln in baseline)
    for tid, st, prio in [("T-CAN1", "cancelled", 20), ("T-CAN2", "canceled", 30), ("T-ARC1", "archived", 5)]:
        db.create_task(tid, f"Title {st}", priority=prio)
        with db.get_queue_conn() as conn: conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (st, tid)); conn.commit()
    term_part = db.sync_board().read_text(encoding="utf-8").split("## Cancelled / Archived (3)\n\n", 1)[1]
    assert [ln.split("|")[1].strip() for ln in term_part.splitlines() if ln.startswith("| `")] == ["`T-ARC1`", "`T-CAN1`", "`T-CAN2`"]
    with db.get_queue_conn() as conn: conn.execute("DELETE FROM tasks WHERE id IN ('T-CAN1', 'T-CAN2', 'T-ARC1')"); conn.commit()
    assert [ln for ln in db.sync_board().read_text(encoding="utf-8").splitlines() if not ln.startswith("*Last synchronized:")] == baseline


def _make_dir_link(link: Path, target: Path, *, dangling: bool = False) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    if dangling:
        target.rmdir()


def test_tier_b_audit_schema_downgrade_spoofing_blocked(tmp_path: Path) -> None:
    for idx, ddl_ops in enumerate([
        ("ALTER TABLE audit_log RENAME COLUMN entry_hash TO hash;", "ALTER TABLE audit_log DROP COLUMN payload_hash;", "UPDATE audit_log SET verification_status = 'verified', hash = ? WHERE id = 1"),
        ("ALTER TABLE audit_log RENAME COLUMN entry_hash TO hash;", "ALTER TABLE audit_log DROP COLUMN verification_status;", "UPDATE audit_log SET hash = ? WHERE id = 1"),
        ("ALTER TABLE audit_log DROP COLUMN payload_hash;", "ALTER TABLE audit_log DROP COLUMN verification_status;", "UPDATE audit_log SET entry_hash = ? WHERE id = 1"),
        ("ALTER TABLE audit_log RENAME COLUMN entry_hash TO hash;", "ALTER TABLE audit_log DROP COLUMN payload_hash;", "ALTER TABLE audit_log RENAME COLUMN verification_status TO VERIFICATION_STATUS;", "UPDATE audit_log SET hash = ? WHERE id = 1"),
    ]):
        db = StigmeRigDB(rig_dir=tmp_path / f"spoof_{idx}")
        db.init_rig(scaffold_icm=False)
        db.log_audit("TSK-1", "task_completed", "WorkerA", "unverified", "payload-1")
        assert db.verify_audit_chain()[0] is True
        forged_hash = hashlib.sha256(b"forged-tier-a").hexdigest()
        with db.get_audit_conn() as conn:
            conn.executescript(
                "DROP TRIGGER IF EXISTS prevent_audit_update;"
                "DROP TRIGGER IF EXISTS prevent_audit_delete;"
                "DROP TRIGGER IF EXISTS verify_hash_chain;"
                "DROP TRIGGER IF EXISTS enforce_contiguous_audit_id;"
            )
            for stmt in ddl_ops[:-1]:
                conn.execute(stmt)
            conn.execute(ddl_ops[-1], (forged_hash,))
            conn.commit()
        assert db.verify_audit_chain() == (False, "audit_log schema invalid")
        with db.get_audit_conn() as conn:
            conn.executescript("DELETE FROM audit_log; DELETE FROM sqlite_sequence WHERE name='audit_log';")
        assert db.verify_audit_chain() == (False, "audit_log schema invalid")

    db_tier_a = StigmeRigDB(rig_dir=tmp_path / "valid_tier_a")
    db_tier_a.rig_dir.mkdir(parents=True)
    h1 = hashlib.sha256(b"legacy-1").hexdigest()
    with db_tier_a.get_audit_conn() as conn:
        conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, timestamp TEXT, actor TEXT, task_id TEXT, action TEXT, previous_hash TEXT, hash TEXT)")
        conn.execute("INSERT INTO audit_log VALUES (1, '2026-01-01T00:00:00Z', 'LegacyAg', 'TSK-1', 'created', ?, ?)", (GENESIS_HASH, h1))
        conn.commit()
    assert db_tier_a.verify_audit_chain() == (True, "Verified 1 audit entries with intact hash chain")


def test_complete_task_bool_approval_and_evidence_symlink_printable_casefold_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    db = StigmeRigDB(rig_dir=ws / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    (ws / "sub").mkdir(parents=True)
    (ws / "Note.txt").write_text("evidence-note", encoding="utf-8")
    (ws / "sub" / "ev.txt").write_text("evidence-sub", encoding="utf-8")
    (ws / "real_link_target.txt").write_text("target-bytes", encoding="utf-8")

    db.create_task("TSK-BOOL", "Approval bool check")
    assert db.claim_task("TSK-BOOL", "WorkerA") is True
    for bad_flag in (1, 0, "true", "false", "0", "", None, [True], 1.0, {}):
        with pytest.raises((TypeError, ValueError)):
            db.complete_task("TSK-BOOL", "WorkerA", verifier="Rev1", allow_self_approval=bad_flag)  # type: ignore[arg-type]
        with pytest.raises((TypeError, ValueError)):
            db.complete_task("TSK-BOOL", "WorkerA", verifier=None, allow_self_approval=bad_flag)  # type: ignore[arg-type]
    assert db.get_task("TSK-BOOL")["status"] == "working"  # type: ignore[index]

    _make_dir_link(ws / "sub_link", ws / "sub")
    _make_dir_link(ws / "up_link", ws.parent)
    _make_dir_link(ws / "dangling_link", ws / "vanished_dir", dangling=True)
    _make_dir_link(tmp_path / "ws_alias", ws)
    _make_dir_link(tmp_path / "parent_alias", tmp_path)

    file_link = ws / "file_link.txt"
    created_file_symlink = False
    try:
        file_link.symlink_to(ws / "real_link_target.txt")
        created_file_symlink = True
    except OSError:
        file_link.write_text("simulated-symlink", encoding="utf-8")

    orig_is_symlink = Path.is_symlink
    with monkeypatch.context() as mp:
        if not created_file_symlink:
            mp.setattr(Path, "is_symlink", lambda self: self.name == "file_link.txt" or orig_is_symlink(self))
        for bad_link_ev in (
            ["file_link.txt"],
            ["./file_link.txt"],
            ["sub/../file_link.txt"],
            [file_link],
            ["sub_link/ev.txt"],
            [ws / "sub_link" / "ev.txt"],
            ["sub_link/../Note.txt"],
            ["up_link/ws/Note.txt"],
            [ws / "up_link" / "ws" / "Note.txt"],
            ["dangling_link/ev.txt"],
            ["dangling_link"],
            [tmp_path / "ws_alias" / "Note.txt"],
            [tmp_path / "parent_alias" / "ws" / "Note.txt"],
            ["../ws/Note.txt"],
        ):
            with pytest.raises(ValueError):
                db._validate_and_hash_evidence(bad_link_ev)
        for rep_link_ev in (
            ["file_link.txt"],
            ["sub_link/ev.txt"],
            ["sub_link/../Note.txt"],
            ["up_link/ws/Note.txt"],
            ["dangling_link/ev.txt"],
        ):
            with pytest.raises(ValueError):
                db.complete_task("TSK-BOOL", "WorkerA", verifier="Rev1", evidence_paths=rep_link_ev)

    for ctrl in ("\n", "\r", "\t", "\x00", "\x01", "\x1b", "\x1f", "\x7f", "\u200b", "\u202e"):
        for bad_print_ev in ([f"Note{ctrl}.txt"], [Path(f"sub/ev{ctrl}.txt")]):
            with pytest.raises(ValueError):
                db._validate_and_hash_evidence(bad_print_ev)
    for rep_ctrl in ("\n", "\x1b", "\x7f"):
        with pytest.raises(ValueError):
            db.complete_task("TSK-BOOL", "WorkerA", verifier="Rev1", evidence_paths=[f"Note{rep_ctrl}.txt"])

    if os.path.normcase("A") == "a":
        for dup_ev in (["Note.txt", "note.txt"], ["Note.txt", "NOTE.TXT"], ["sub/ev.txt", "SUB/EV.TXT"]):
            with pytest.raises(ValueError, match="Duplicate evidence path"):
                db._validate_and_hash_evidence(dup_ev)
            with pytest.raises(ValueError, match="Duplicate evidence path"):
                db.complete_task("TSK-BOOL", "WorkerA", verifier="Rev1", evidence_paths=dup_ev)

    (ws / "straße.txt").write_text("sharp-s", encoding="utf-8")
    orig_resolve, orig_exists, orig_is_file, orig_stat = Path.resolve, Path.exists, Path.is_file, Path.stat
    fake_st = os.stat_result((orig_stat(ws / "straße.txt").st_mode, 0, 0, 1, 0, 0, 7, 0, 0, 0))
    with monkeypatch.context() as mp:
        mp.setattr(os.path, "normcase", str.lower)
        mp.setattr(
            Path,
            "resolve",
            lambda self, *a, **kw: (orig_resolve(ws) / "STRASSE.TXT") if self.name == "STRASSE.TXT" else orig_resolve(self, *a, **kw),
        )
        mp.setattr(Path, "exists", lambda self: True if self.name == "STRASSE.TXT" else orig_exists(self))
        mp.setattr(Path, "is_file", lambda self: True if self.name == "STRASSE.TXT" else orig_is_file(self))
        mp.setattr(Path, "stat", lambda self, *a, **kw: fake_st if self.name == "STRASSE.TXT" else orig_stat(self, *a, **kw))
        with pytest.raises(ValueError, match="Duplicate evidence path"):
            db._validate_and_hash_evidence(["straße.txt", "STRASSE.TXT"])

    assert db.get_task("TSK-BOOL")["status"] == "working"  # type: ignore[index]
    ok, msg = db.complete_task("TSK-BOOL", "WorkerA", verifier="Rev1", allow_self_approval=False, evidence_paths=["Note.txt", "sub/ev.txt"])
    assert ok is True and "successfully" in msg
    db.create_task("TSK-SELF-OK", "Explicit bool self approval")
    assert db.claim_task("TSK-SELF-OK", "WorkerA") is True
    assert db.complete_task("TSK-SELF-OK", "WorkerA", verifier=None, allow_self_approval=True)[0] is True
    assert db.verify_audit_chain()[0] is True


def test_fail_task_printable_reason_and_null_lease_working_ownership_guards(tmp_path: Path) -> None:
    db = StigmeRigDB(rig_dir=tmp_path / "ws" / ".stigmerig")
    db.init_rig(scaffold_icm=False)
    db.create_task("TSK-NULL-LEASE", "Working with NULL lease")
    assert db.claim_task("TSK-NULL-LEASE", "OwnerAgent") is True
    with db.get_queue_conn() as conn:
        conn.execute("UPDATE tasks SET lease_until = NULL WHERE id = 'TSK-NULL-LEASE'")
        conn.commit()
    baseline_audit = db.verify_audit_chain()
    assert baseline_audit[0] is True

    for bad_reason in (
        "bad\x00nul",
        "bad\x01soh",
        "bad\x07bel",
        "bad\x08bs",
        "bad\x0bvt",
        "bad\x0cff",
        "\x1b[31mansi-red\x1b[0m",
        "bad\x1funit",
        "bad\x7fdel",
        "bad\x9bcsi",
        "bad\u200bzwsp",
        "bad\u202ebidi",
    ):
        with pytest.raises(ValueError):
            db.fail_task("TSK-NULL-LEASE", "OwnerAgent", bad_reason)

    with pytest.raises(PermissionError):
        db.fail_task("TSK-NULL-LEASE", "IntruderAgent", "Attempted hijack of NULL-lease working task")

    task_after = db.get_task("TSK-NULL-LEASE")
    assert task_after is not None
    assert task_after["status"] == "working" and task_after["claimed_by"] == "OwnerAgent" and task_after["lease_until"] is None
    assert not (db.rig_dir / "traps.json").exists()
    with db.get_queue_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE event_type = 'blocked'").fetchone()[0] == 0

    multiline_reason = "Step 1\tindented\nStep 2\r\nStep 3"
    assert db.fail_task("TSK-NULL-LEASE", "OwnerAgent", multiline_reason) is True
    assert db.get_task("TSK-NULL-LEASE")["status"] == "blocked"  # type: ignore[index]

    db.create_task("TSK-EXP-LEASE", "Working with expired lease")
    assert db.claim_task("TSK-EXP-LEASE", "OwnerAgent") is True
    with db.get_queue_conn() as conn:
        conn.execute("UPDATE tasks SET lease_until = '2020-01-01T00:00:00Z' WHERE id = 'TSK-EXP-LEASE'")
        conn.commit()
    assert db.fail_task("TSK-EXP-LEASE", "RecoveryAgent", "Lease expired recovery") is True
    assert db.get_task("TSK-EXP-LEASE")["status"] == "blocked"  # type: ignore[index]
    assert db.verify_audit_chain()[0] is True


def test_init_rig_rejects_legacy_audit_schema_without_entry_hash(tmp_path: Path) -> None:
    """Fail loudly when opening a legacy v1 audit_log table lacking entry_hash."""
    rig_dir = tmp_path / ".stigmerig"
    rig_dir.mkdir()
    with sqlite3.connect(rig_dir / "audit.db") as conn:
        conn.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT NOT NULL, previous_hash TEXT)"
        )
        conn.commit()
    db = StigmeRigDB(rig_dir)
    with pytest.raises(RuntimeError, match="Legacy audit_log schema detected"):
        db.init_rig(scaffold_icm=False)


def test_complete_task_supports_external_rig_dir_with_workspace_root(tmp_path: Path) -> None:
    """Allow evidence hashing against workspace_root when rig_dir sits outside the repo."""
    repo_dir = tmp_path / "repo"
    rig_dir = tmp_path / "external_rig"
    repo_dir.mkdir()
    evidence = repo_dir / "src" / "app.py"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("print('ok')\n", encoding="utf-8")

    db = StigmeRigDB(rig_dir, workspace_root=repo_dir)
    db.init_rig(scaffold_icm=False)
    db.create_task("TSK-EXT-001", "External rig dir evidence test")
    assert db.claim_task("TSK-EXT-001", "builder/ext") is True

    ok, _ = db.complete_task(
        "TSK-EXT-001",
        agent="builder/ext",
        verifier="verifier/independent-01",
        session_id="ext-session",
        evidence_paths=["src/app.py"],
    )
    assert ok is True
    assert db.verify_audit_chain()[0] is True

    outside = tmp_path / "outside.txt"
    outside.write_text("nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes allowed root"):
        db._validate_and_hash_evidence([outside])

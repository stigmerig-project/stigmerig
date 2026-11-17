"""Edge-case tests for CLI validation, uninitialized rig guards, and error handlers."""

from pathlib import Path
import pytest
from stigmerig.cli import main


@pytest.fixture
def init_rig_dir(tmp_path: Path) -> Path:
    rig_dir = tmp_path / ".stigmerig"
    assert main(["--rig-dir", str(rig_dir), "init", "--no-icm"]) == 0
    assert main(["--rig-dir", str(rig_dir), "task", "create", "--id", "TSK-1", "--title", "Seed"]) == 0
    return rig_dir


def test_uninitialized_and_partial_rig_guards(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty_dir = tmp_path / "empty_rig"
    empty_dir.mkdir()
    for cmd in (["audit", "verify"], ["task", "list"], ["board", "sync"], ["telemetry", "sequence"], ["telemetry", "metrics"]):
        assert main(["--rig-dir", str(empty_dir), *cmd]) == 1 and (err := capsys.readouterr().err).startswith("[ERROR]") and "(missing queue.db, audit.db)" in err and "stigmerig init" in err
        assert list(empty_dir.iterdir()) == []
    for present, missing in (("queue.db", "audit.db"), ("audit.db", "queue.db")):
        partial = tmp_path / f"only_{present}"
        partial.mkdir()
        (partial / present).write_bytes(b"")
        assert main(["--rig-dir", str(partial), "audit", "verify"]) == 1
        assert (err := capsys.readouterr().err).startswith("[ERROR]") and f"(missing {missing})" in err and "stigmerig init" in err and not (partial / missing).exists()


@pytest.mark.parametrize("argv", [
    ["task", "claim", "--id", "TSK-1", "--agent", "Ag", "--ttl", "0"],
    ["task", "claim", "--id", "TSK-1", "--agent", "Ag", "--ttl", "-5"],
    ["task", "create", "--id", "TSK-2", "--title", "   "],
    ["task", "claim", "--id", "TSK-1", "--agent", " \t "],
    ["task", "done", "--id", "TSK-1", "--agent", "   ", "--allow-self-approval"],
    ["task", "fail", "--id", "TSK-1", "--agent", "   ", "--reason", "bad"],
    ["task", "fail", "--id", "TSK-1", "--agent", "Ag", "--reason", " \n "],
    ["audit", "log", "--task", "TSK-1", "--action", "  ", "--agent", "Ag", "--status", "ok"],
    ["audit", "log", "--task", "TSK-1", "--action", "act", "--agent", "  ", "--status", "ok"],
    ["audit", "log", "--task", "TSK-1", "--action", "act", "--agent", "Ag", "--status", "  "],
    ["audit", "log", "--task", "../pwn", "--action", "act", "--agent", "Ag", "--status", "ok"],
    ["audit", "log", "--task", "a/b", "--action", "act", "--agent", "Ag", "--status", "ok"],
    ["telemetry", "sequence", "--task", "../pwn"],
    ["telemetry", "sequence", "--task", "   "],
])
def test_cli_rejects_invalid_flags(init_rig_dir: Path, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--rig-dir", str(init_rig_dir), *argv]) == 1 and "[ERROR]" in capsys.readouterr().err


def test_telemetry_output_parents_and_dir_target(init_rig_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outs = [tmp_path / "nested" / name for name in ("s/diag.md", "m/table.md", "j/metrics.json")]
    assert main(["--rig-dir", str(init_rig_dir), "telemetry", "sequence", "--output", str(outs[0])]) == 0
    assert main(["--rig-dir", str(init_rig_dir), "telemetry", "metrics", "--output", str(outs[1])]) == 0
    assert main(["--rig-dir", str(init_rig_dir), "telemetry", "metrics", "--json", "--output", str(outs[2])]) == 0
    assert all(p.is_file() for p in outs)
    target_dir = tmp_path / "existing_dir"
    target_dir.mkdir()
    for sub in (["sequence", "--output", str(target_dir)], ["metrics", "--output", str(target_dir)], ["metrics", "--json", "--output", str(target_dir)]):
        capsys.readouterr()
        assert main(["--rig-dir", str(init_rig_dir), "telemetry", *sub]) == 1 and "[ERROR]" in capsys.readouterr().err


def test_database_error_and_oserror_handlers(init_rig_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker_file = tmp_path / "not_a_dir"
    blocker_file.write_text("file", encoding="utf-8")
    assert main(["--rig-dir", str(init_rig_dir), "telemetry", "metrics", "--output", str(blocker_file / "out.md")]) == 1
    assert "[ERROR]" in capsys.readouterr().err
    (init_rig_dir / "queue.db").write_bytes(b"corrupted sqlite header bytes")
    capsys.readouterr()
    assert main(["--rig-dir", str(init_rig_dir), "task", "list"]) == 1 and "[ERROR]" in capsys.readouterr().err


def test_upfront_output_runtime_loop_and_standardized_diagnostics(init_rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import sys
    from stigmerig import cli, db as db_mod, telemetry as telem_mod
    f_anc, ex_dir, sym_f, seen, real_print = tmp_path / "f_anc", tmp_path / "ex_dir", tmp_path / "sym.md", [], print
    f_anc.write_text("x", encoding="utf-8")
    ex_dir.mkdir()
    run_err = lambda *a: (capsys.readouterr(), main(["--rig-dir", str(init_rig_dir), *a]), capsys.readouterr().err)[1:]
    with monkeypatch.context() as mp:
        mp.setattr("builtins.print", lambda *a, **kw: (seen.append(sys.exc_info()[0]), real_print(*a, **kw))[1])
        assert main(["--rig-dir", str(tmp_path / "no_rig"), "task", "list"]) == 1 and seen[-1] is FileNotFoundError
        orig_is_symlink = Path.is_symlink
        mp.setattr(Path, "is_symlink", lambda self: self == sym_f or orig_is_symlink(self))
        mp.setattr(cli, "StigmeRigDB", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DB touched")))
        mp.setattr(telem_mod, "TelemetryExporter", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Telemetry touched")))
        for out_p, exc_cls in ((ex_dir, IsADirectoryError), *((p, ValueError) for p in (f"{tmp_path}/b\no.md", f"{tmp_path}/b\x7fo.md", sym_f, tmp_path / ".git/o.md", tmp_path / ".worktrees/w/o.md", tmp_path / ".env.local", f_anc / "s/o.md", init_rig_dir, init_rig_dir / "o.md"))):
            argv = ["--rig-dir", str(init_rig_dir), "telemetry", "metrics", "--output", str(out_p)]
            assert all(pytest.raises(exc_cls, fn, *fn_a) for fn, fn_a in ((cli._validate_cli_args, (cli.build_parser().parse_args(argv),)), (cli._write_output, (str(out_p), "x", init_rig_dir))))
            assert (r := run_err("telemetry", "metrics", "--output", str(out_p)))[0] == 1 and r[1].startswith("[ERROR] ")
        mp.setattr(Path, "resolve", lambda self, *a, **kw: (_ for _ in ()).throw(RuntimeError("Symlink loop")))
        assert (r := run_err("task", "list"))[0] == 1 and r[1].startswith("[ERROR] Symlink loop")
    rig, stale, real_get = db_mod.StigmeRigDB(rig_dir=init_rig_dir), [1], db_mod.StigmeRigDB.get_task
    with monkeypatch.context() as mp:
        mp.setattr(db_mod.StigmeRigDB, "get_task", lambda self, tid: {**real_get(self, tid), "version": 99} if stale and stale.pop() else real_get(self, tid))
        assert (r := run_err("task", "claim", "--id", "TSK-1", "--agent", "A"))[0] == 1 and r[1].startswith("[ERROR] ") and "CAS version conflict" in r[1]
        mp.setattr(db_mod.StigmeRigDB, "fail_task", lambda *a, **kw: False)
        assert (r := run_err("task", "fail", "--id", "TSK-1", "--agent", "A", "--reason", "r"))[0] == 1 and r[1].startswith("[ERROR] Failed to mark task TSK-1 blocked")
    assert all((r := run_err(*c))[0] == 1 and r[1].startswith("[ERROR] ") and s in r[1] for c, s in ((["task", "get", "--id", "TASK-404"], "Task TASK-404 not found"), (["task", "done", "--id", "TSK-1", "--agent", "A", "--allow-self-approval"], "Failed to complete task TSK-1:"), (["task", "claim", "--id", "TASK-404", "--agent", "A"], "task not found")))
    assert main(["--rig-dir", str(init_rig_dir), "task", "claim", "--id", "TSK-1", "--agent", "AgentA"]) == 0 and (r := run_err("task", "claim", "--id", "TSK-1", "--agent", "AgentB"))[0] == 1 and r[1].startswith("[ERROR] ") and "active lease held by 'AgentA'" in r[1]
    with rig.get_queue_conn() as c, rig.get_audit_conn() as ac:
        assert all((c.executescript(f"UPDATE tasks SET status = '{st}' WHERE id = 'TSK-1';"), (r := run_err("task", "claim", "--id", "TSK-1", "--agent", "AgentA"))[0] == 1 and r[1].startswith("[ERROR] ") and f"non-claimable status '{st}'" in r[1])[1] for st in ("done", "blocked", "cancelled", "archived"))
        ac.executescript("DROP TRIGGER IF EXISTS prevent_audit_update; UPDATE audit_log SET payload_hash = '00' WHERE id = 1;")
    assert (r := run_err("audit", "verify"))[0] == 1 and r[1].startswith("[ERROR] Audit verification FAILED:")

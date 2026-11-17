"""Hermetic boundary-guard tests for CLI path flags, task/verifier strings, and TTL overflow."""

from pathlib import Path
import sqlite3
import subprocess
import pytest
import stigmerig.cli as cli
from stigmerig.cli import _validate_cli_args, _write_output, build_parser, main
from stigmerig.db import StigmeRigDB


@pytest.fixture
def seeded_rig(tmp_path: Path) -> Path:
    rig_dir = tmp_path / "ws" / ".stigmerig"
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "note.txt").write_text("artifact", encoding="utf-8")
    assert main(["--rig-dir", str(rig_dir), "init", "--no-icm"]) == 0
    assert main(["--rig-dir", str(rig_dir), "task", "create", "--id", "TSK-1", "--title", "Seed"]) == 0
    return rig_dir


def test_rig_dir_and_init_path_reject_blank_and_regular_files(
    seeded_rig: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(seeded_rig.parent)
    monkeypatch.setenv("STIGMERIG_ROOT", str(seeded_rig))
    reg_file = tmp_path / "collision.txt"
    reg_file.write_text("immutable", encoding="utf-8")
    for argv in (
        ["--rig-dir", "", "task", "list"], ["--rig-dir", " \t ", "init", "--no-icm"],
        ["init", "--path", ""], ["init", "--path", "  \n "], ["--rig-dir", str(seeded_rig), "init", "--path", ""],
        ["--rig-dir", str(reg_file), "init", "--no-icm"], ["--rig-dir", str(reg_file), "task", "list"], ["init", "--path", str(reg_file)],
    ):
        capsys.readouterr()
        with pytest.raises(ValueError):
            _validate_cli_args(build_parser().parse_args(argv))
        assert main(argv) == 1 and "[ERROR]" in capsys.readouterr().err
    assert reg_file.read_text(encoding="utf-8") == "immutable"


@pytest.mark.parametrize("argv", [
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--parent", ""],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--parent", "   "],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--parent", "../bad"],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--parent", "bad/id"],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--parent", ".hidden"],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--parent", "a" * 65],
    ["task", "list", "--parent", ""],
    ["task", "list", "--parent", " \t "],
    ["task", "list", "--parent", "../escape"],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--author", ""],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--author", "  "],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--archetype", ""],
    ["task", "create", "--id", "TSK-2", "--title", "Ok", "--archetype", " \n "],
    ["task", "done", "--id", "TSK-1", "--agent", "W", "--verifier", ""],
    ["task", "done", "--id", "TSK-1", "--agent", "W", "--verifier", "   ", "--allow-self-approval"],
    ["task", "done", "--id", "TSK-1", "--agent", "W", "--allow-self-approval", "--session-id", ""],
    ["task", "done", "--id", "TSK-1", "--agent", "W", "--allow-self-approval", "--session-id", " \t "],
    ["task", "done", "--id", "TSK-1", "--agent", "W", "--allow-self-approval", "--evidence", ""],
    ["task", "done", "--id", "TSK-1", "--agent", "W", "--allow-self-approval", "--evidence", "note.txt", "--evidence", "  "],
])
def test_validate_cli_args_rejects_blank_strings_and_malformed_parent(
    seeded_rig: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    full_argv = ["--rig-dir", str(seeded_rig), *argv]
    with pytest.raises(ValueError):
        _validate_cli_args(build_parser().parse_args(full_argv))
    assert main(full_argv) == 1 and "[ERROR]" in capsys.readouterr().err


def test_ttl_overflow_and_valid_boundary_roundtrip(
    seeded_rig: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for huge_ttl in ("300000000000", str(10**18), str(10**30)):
        argv = ["--rig-dir", str(seeded_rig), "task", "claim", "--id", "TSK-1", "--agent", "W", "--ttl", huge_ttl]
        with pytest.raises(ValueError):
            _validate_cli_args(build_parser().parse_args(argv))
        capsys.readouterr()
        assert main(argv) == 1 and "[ERROR]" in capsys.readouterr().err
    assert main(["--rig-dir", str(seeded_rig), "task", "create", "--id", "TSK-2", "--title", "Child", "--parent", "TSK-1", "--author", "Op", "--archetype", "unit_tester"]) == 0
    assert main(["--rig-dir", str(seeded_rig), "task", "claim", "--id", "TSK-2", "--agent", "W", "--ttl", "600"]) == 0
    assert main(["--rig-dir", str(seeded_rig), "task", "done", "--id", "TSK-2", "--agent", "W", "--verifier", "Rev", "--session-id", "s-1", "--evidence", "note.txt"]) == 0
    monkeypatch.setattr(StigmeRigDB, "claim_task", lambda *a, **kw: (_ for _ in ()).throw(OverflowError("ttl overflow")))
    capsys.readouterr()
    assert main(["--rig-dir", str(seeded_rig), "task", "claim", "--id", "TSK-1", "--agent", "W", "--ttl", "60"]) == 1
    assert "[ERROR]" in capsys.readouterr().err


def _link_dir(link: Path, target: Path, dangling: bool = False) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    if dangling:
        target.rmdir()


def test_c1_escape_guards_reject_across_flags_and_write_output_before_db(
    seeded_rig: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = {n: (seeded_rig / n).read_bytes() for n in ("queue.db", "audit.db")}
    c1s = ("\x80", "\x85", "\x90", "\x9b[31m", "\x9f")
    bad_argvs = [
        *(a for c in c1s for a in (
            ["task", "claim", "--id", "TSK-1", "--agent", f"W{c}1"],
            ["task", "done", "--id", "TSK-1", "--agent", "W", "--verifier", f"R{c}1"],
            ["task", "done", "--id", "TSK-1", "--agent", "W", "--allow-self-approval", "--session-id", f"s{c}1"],
            ["task", "done", "--id", "TSK-1", "--agent", "W", "--verifier", "R", "--evidence", "note.txt", "--evidence", f"ev{c}.txt"],
            ["task", "create", "--id", "TSK-C", "--title", "Ok", "--author", f"Op{c}1"],
            ["task", "create", "--id", "TSK-C", "--title", "Ok", "--archetype", f"arch{c}1"],
            ["task", "create", "--id", "TSK-C", "--title", "Ok", "--tags", f"ok,t{c}1"],
            ["task", "list", "--status", f"todo{c}"],
            ["audit", "log", "--task", "TSK-1", "--action", f"act{c}", "--agent", "W", "--status", "ok"],
            ["audit", "log", "--task", "TSK-1", "--action", "act", "--agent", "W", "--status", f"ok{c}"],
            ["telemetry", "metrics", "--output", f"{tmp_path / 'out'}{c}.md"],
            ["worktree", "prune", "--output", f"{tmp_path / 'prune'}{c}.json"],
        )),
    ]
    with monkeypatch.context() as mp:
        mp.setattr(cli, "StigmeRigDB", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DB touched")))
        for c in c1s:
            with pytest.raises(ValueError):
                _write_output(f"{tmp_path / 'direct'}{c}.md", "x", seeded_rig)
        for rel_argv in bad_argvs:
            argv = ["--rig-dir", str(seeded_rig), *rel_argv]
            with pytest.raises(ValueError):
                _validate_cli_args(build_parser().parse_args(argv))
            capsys.readouterr()
            assert main(argv) == 1 and "[ERROR]" in capsys.readouterr().err
    assert {n: (seeded_rig / n).read_bytes() for n in snap} == snap


def test_upfront_evidence_symlink_root_and_denied_path_guards(
    seeded_rig: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws, custom_rig, sym_ev = seeded_rig.parent, seeded_rig.parent / "custom_rig", seeded_rig.parent / "sym_note.txt"
    for d in (ws / "sub", ws / ".git", ws / ".worktrees" / "T1", custom_rig):
        d.mkdir(parents=True, exist_ok=True)
    for f in (ws / "sub/a.txt", ws / ".git/HEAD", ws / ".worktrees/T1/f.txt", ws / ".env", ws / ".env.local", tmp_path / "outside.txt", custom_rig / "doc.txt"):
        f.write_text("x", encoding="utf-8")
    _link_dir(ws / "sub_link", ws / "sub")
    _link_dir(ws / "dang_link", tmp_path / "vanished_ev", dangling=True)
    _link_dir(tmp_path / "ext_link", ws)
    bad_ev = (
        "sym_note.txt", str(sym_ev), "sub_link", "sub_link/a.txt", "sub_link/../note.txt", "dang_link", str(tmp_path / "ext_link/note.txt"),
        "../outside.txt", "sub/../../outside.txt", "../ws/note.txt", str(tmp_path / "outside.txt"), ".", str(ws),
        ".git", ".git/HEAD", ".GIT/config", ".git/../note.txt", ".worktrees/T1/f.txt", ".WORKTREES/f.txt", ".worktrees/../note.txt",
        ".env", ".ENV", ".env.local", ".env_secret", "sub/.env.prod", "queue.db", "audit.db-wal", ".stigmerig", ".stigmerig/queue.db", str(seeded_rig / "rig.json"),
    )
    with monkeypatch.context() as mp:
        orig_is_symlink = Path.is_symlink
        mp.setattr(Path, "is_symlink", lambda self: self == sym_ev or orig_is_symlink(self))
        mp.setattr(cli, "StigmeRigDB", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DB touched")))
        for rig, ev in (*((seeded_rig, e) for e in bad_ev), (custom_rig, "custom_rig/doc.txt")):
            argv = ["--rig-dir", str(rig), "task", "done", "--id", "TSK-1", "--agent", "W", "--verifier", "R", "--evidence", ev]
            with pytest.raises(ValueError):
                _validate_cli_args(build_parser().parse_args(argv))
            capsys.readouterr()
            assert main(argv) == 1 and "[ERROR]" in capsys.readouterr().err


def test_main_called_process_and_sqlite_error_diagnostics(
    seeded_rig: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for cpe, expected in (
        (subprocess.CalledProcessError(128, ["git", "prune"], output="out\n", stderr="  fatal: prune lock  \n"), ": fatal: prune lock"),
        (subprocess.CalledProcessError(1, ["git", "prune"], output="  stdout fallback  \n", stderr="  \n"), ": stdout fallback"),
        (subprocess.CalledProcessError(2, ["git", "prune"], output=b"", stderr=b"  bytes err \r\n"), ": bytes err"),
        (subprocess.CalledProcessError(3, ["git", "prune"]), "exit status 3"),
    ):
        with monkeypatch.context() as mp:
            mp.setattr(StigmeRigDB, "list_tasks", lambda *_a, _e=cpe, **_kw: (_ for _ in ()).throw(_e))
            capsys.readouterr()
            assert main(["--rig-dir", str(seeded_rig), "task", "list"]) == 1 and expected in capsys.readouterr().err
    zero_rig = tmp_path / "zero_rig"
    zero_rig.mkdir()
    for n in ("queue.db", "audit.db"):
        (zero_rig / n).write_bytes(b"")
    for cmd in (["task", "list"], ["task", "create", "--id", "TSK-Z", "--title", "Z"], ["audit", "log", "--task", "TSK-1", "--action", "a", "--agent", "W", "--status", "ok"]):
        capsys.readouterr()
        assert main(["--rig-dir", str(zero_rig), *cmd]) == 1 and str(zero_rig.resolve()) in (err := capsys.readouterr().err) and "stigmerig init" in err
    capsys.readouterr()
    assert main(["--rig-dir", str(seeded_rig), "task", "create", "--id", "TSK-1", "--title", "Dup"]) == 1
    assert str(seeded_rig.resolve()) in (dup_err := capsys.readouterr().err) and "stigmerig init" not in dup_err
    with monkeypatch.context() as mp:
        mp.setattr(StigmeRigDB, "list_tasks", lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        capsys.readouterr()
        assert main(["--rig-dir", str(seeded_rig), "task", "list"]) == 1 and str(seeded_rig.resolve()) in (lk := capsys.readouterr().err) and "stigmerig init" not in lk
    (seeded_rig / "queue.db").write_bytes(b"corrupt sqlite bytes")
    capsys.readouterr()
    assert main(["--rig-dir", str(seeded_rig), "task", "list"]) == 1 and str(seeded_rig.resolve()) in (c_err := capsys.readouterr().err) and "stigmerig init" not in c_err

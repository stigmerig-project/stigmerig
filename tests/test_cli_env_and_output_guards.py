"""Hermetic tests for CLI STIGMERIG_ROOT validation, NUL/int/tag/parent guards, and atomic rig-safe outputs."""

import argparse
from pathlib import Path
import pytest
import stigmerig.cli as cli


def _link_dir(link: Path, target: Path, dangling: bool = False) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    if dangling:
        target.rmdir()


def _err(capsys: pytest.CaptureFixture[str], argv: list[str]) -> bool:
    capsys.readouterr()
    return cli.main(argv) == 1 and "[ERROR]" in capsys.readouterr().err


@pytest.fixture
def rig_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ws" / ".stigmerig"
    (tmp_path / "ws").mkdir()
    assert cli.main(["--rig-dir", str(d), "init", "--no-icm"]) == 0 and cli.main(["--rig-dir", str(d), "task", "create", "--id", "TSK-1", "--title", "S"]) == 0
    (d / "traps.json").write_text('{"traps": []}\n', encoding="utf-8")
    return d


def test_stigmerig_root_and_flag_guards(rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    reg_file, dangling = tmp_path / "file.txt", tmp_path / "dangling_root"
    monkeypatch.chdir(rig_dir.parent)
    reg_file.write_text("keep", encoding="utf-8")
    _link_dir(dangling, tmp_path / "vanished_root", dangling=True)
    for bad_env in ("", " \t\n ", "a\x00b", str(reg_file), str(dangling)):
        monkeypatch.setattr("stigmerig.cli.os.environ", {"STIGMERIG_ROOT": bad_env})
        assert _err(capsys, ["init", "--no-icm"]) and _err(capsys, ["task", "list"]) and cli.main(["--rig-dir", str(rig_dir), "task", "list"]) == 0
    monkeypatch.setenv("STIGMERIG_ROOT", str(reg_file))
    assert cli.main(["init", "--path", str(tmp_path / "explicit_init"), "--no-icm"]) == 0 and reg_file.read_text(encoding="utf-8") == "keep"
    monkeypatch.setenv("STIGMERIG_ROOT", str(rig_dir))
    assert cli.main(["task", "list"]) == 0 and not (tmp_path / "vanished_root").exists()
    snap = {n: (rig_dir / n).read_bytes() for n in ("queue.db", "audit.db", "traps.json")}
    for argv in (
        ["--rig-dir", f"{rig_dir}\x00x", "task", "list"], ["task", "create", "--id", "TSK\x002", "--title", "Ok"], ["task", "create", "--id", "TSK-2", "--title", "B\x00T"],
        ["task", "done", "--id", "TSK-1", "--agent", "W", "--evidence", "a\x00b"], ["audit", "log", "--task", "TSK-1", "--action", "a", "--agent", "W", "--status", "ok", "--payload", "p\x00q"],
        ["telemetry", "metrics", "--output", f"{tmp_path / 'o'}\x00.md"], ["task", "create", "--id", "TSK-2", "--title", "Ok", "--tags", ""], ["task", "create", "--id", "TSK-2", "--title", "Ok", "--tags", " \t "],
        ["task", "create", "--id", "TSK-2", "--title", "Ok", "--tags", "a, ,b"], ["task", "create", "--id", "TSK-2", "--title", "Self", "--parent", "TSK-2"],
        ["task", "create", "--id", "TSK-2", "--title", "Lo", "--priority", str(-(1 << 63) - 1)], ["task", "create", "--id", "TSK-2", "--title", "Hi", "--priority", str(1 << 63)],
    ):
        with pytest.raises(ValueError):
            cli._validate_cli_args(cli.build_parser().parse_args(argv))
        assert _err(capsys, argv)
    for ns in (argparse.Namespace(priority=True), argparse.Namespace(priority=False), argparse.Namespace(path=Path("a\x00b"))):
        with pytest.raises(ValueError):
            cli._validate_cli_args(ns)
    assert {n: (rig_dir / n).read_bytes() for n in snap} == snap
    assert cli.main(["task", "create", "--id", "TSK-MIN", "--title", "Min", "--parent", "TSK-1", "--tags", "cli,guard", "--priority", str(-(1 << 63))]) == 0
    assert cli.main(["task", "create", "--id", "TSK-MAX", "--title", "Max", "--priority", str((1 << 63) - 1)]) == 0


def test_write_output_symlinks_rig_containment_and_atomic_write(rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    dir_link, dang_link, snap = tmp_path / "dir_link", tmp_path / "dang_link", {n: (rig_dir / n).read_bytes() for n in ("queue.db", "audit.db", "traps.json")}
    _link_dir(dir_link, tmp_path / "real_out_dir")
    _link_dir(dang_link, tmp_path / "missing_target", dangling=True)
    monkeypatch.setenv("STIGMERIG_ROOT", str(rig_dir))
    for bad_out in (dir_link, dang_link, rig_dir, *(rig_dir / n for n in (*snap, "sub/m.md"))):
        assert all(_err(capsys, ["telemetry", *s, "--output", str(bad_out)]) for s in (["sequence"], ["metrics"], ["metrics", "--json"]))
    assert {n: (rig_dir / n).read_bytes() for n in snap} == snap and not (tmp_path / "missing_target").exists() and not (rig_dir / "sub").exists()
    fake_sym, calls, real_atomic, valid_out = tmp_path / "fake_sym.md", [], cli._atomic_write_text, tmp_path / "out" / "metrics.md"
    with monkeypatch.context() as mp:
        mp.delattr(Path, "is_junction", raising=False)
        assert cli.main(["init", "--path", str(tmp_path / "py310_rig"), "--no-icm"]) == 0 and cli.main(["telemetry", "metrics", "--output", str(tmp_path / "py310.md")]) == 0
        mp.setattr(Path, "is_symlink", lambda self: self == fake_sym)
        with pytest.raises(ValueError):
            cli._write_output(str(fake_sym), "data", rig_dir)
    monkeypatch.setattr(cli, "_atomic_write_text", lambda p, c: (calls.append(p), real_atomic(p, c))[1])
    assert cli.main(["telemetry", "metrics", "--output", str(valid_out)]) == 0 and calls == [valid_out]
    valid_out.write_text("preserved", encoding="utf-8")
    monkeypatch.setattr("stigmerig.db.os.replace", lambda *_a: (_ for _ in ()).throw(OSError("replace boom")))
    assert _err(capsys, ["telemetry", "sequence", "--output", str(valid_out)])
    assert valid_out.read_text(encoding="utf-8") == "preserved" and list(valid_out.parent.glob(".*.tmp")) == []

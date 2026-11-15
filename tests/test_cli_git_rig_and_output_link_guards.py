"""Hermetic tests for CLI .git/file-ancestor rig guards, symlink/.worktrees/.env output guards, and --evidence checks."""

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


def _rejects(capsys: pytest.CaptureFixture[str], argv: list[str]) -> bool:
    with pytest.raises(ValueError):
        cli._validate_cli_args(cli.build_parser().parse_args(argv))
    capsys.readouterr()
    return cli.main(argv) == 1 and "[ERROR]" in capsys.readouterr().err


@pytest.fixture
def rig_dir(tmp_path: Path) -> Path:
    ws, rig = tmp_path / "ws", tmp_path / "ws" / ".stigmerig"
    (ws / "sub").mkdir(parents=True)
    (ws / "note.txt").write_text("n1", encoding="utf-8")
    (ws / "sub" / "a.txt").write_text("n2", encoding="utf-8")
    assert cli.main(["--rig-dir", str(rig), "init", "--no-icm"]) == 0 and cli.main(["--rig-dir", str(rig), "task", "create", "--id", "TSK-1", "--title", "S"]) == 0
    return rig


def test_git_and_file_ancestor_rig_guards_and_init_path_precedence(rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo, wt, reg, git_link, dang = (tmp_path / n for n in ("repo", "wt", "reg.txt", "git_link", "dang"))
    for d in (repo / ".git" / "hooks", wt):
        d.mkdir(parents=True)
    reg.write_text("immutable", encoding="utf-8")
    (wt / ".git").write_text("gitdir: ../repo/.git/worktrees/wt\n", encoding="utf-8")
    _link_dir(git_link, repo / ".git")
    _link_dir(dang, tmp_path / "vanished", dangling=True)
    bad = (repo / ".git", repo / ".GIT/rig", repo / ".git/hooks/rig", repo / ".git./rig", repo / ".git /rig", repo / "x/../.git/rig", repo / ".git/../esc", git_link, git_link / "rig", git_link / "../esc", wt / ".git", wt / ".git/sub", reg, reg / "sub", reg / "a/b", dang / "sub")
    with monkeypatch.context() as mp:
        mp.setattr(cli, "StigmeRigDB", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DB accessed")))
        for bd in bad:
            mp.delenv("STIGMERIG_ROOT", raising=False)
            assert _rejects(capsys, ["--rig-dir", str(bd), "init", "--no-icm"]) and _rejects(capsys, ["--rig-dir", str(bd), "task", "list"]) and _rejects(capsys, ["init", "--path", str(bd), "--no-icm"])
            mp.setenv("STIGMERIG_ROOT", str(bd))
            assert _rejects(capsys, ["init", "--no-icm"]) and _rejects(capsys, ["task", "list"])
        mp.delenv("STIGMERIG_ROOT", raising=False)
        for bad_cwd in (repo / ".git", repo / ".git/hooks", git_link):
            mp.chdir(bad_cwd)
            assert _rejects(capsys, ["init", "--no-icm"]) and _rejects(capsys, ["task", "list"])
    for i, bad_env in enumerate((str(repo / ".git"), str(git_link / "rig"), str(reg / "sub"), "")):
        monkeypatch.setenv("STIGMERIG_ROOT", bad_env)
        ok = tmp_path / f"init_ok_{i}"
        assert cli.main(["init", "--path", str(ok), "--no-icm"]) == 0 and (ok / "queue.db").is_file() and cli.main(["--rig-dir", str(rig_dir), "task", "list"]) == 0
    assert _rejects(capsys, ["--rig-dir", str(repo / ".git"), "init", "--path", str(tmp_path / "never"), "--no-icm"]) and reg.read_text(encoding="utf-8") == "immutable" and not (tmp_path / "never").exists()


def test_output_symlink_worktrees_env_and_evidence_guards(rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    ws, sec_f = rig_dir.parent, tmp_path / "secret.txt"
    wt_dir, env_f, env_loc = ws / ".worktrees" / "T1", ws / ".env", ws / ".ENV.local"
    wt_dir.mkdir(parents=True)
    for f, txt in ((env_f, "K=1"), (env_loc, "K=2"), (sec_f, "SEC")):
        f.write_text(txt, encoding="utf-8")
    wt_link, dir_link, env_d_link, sub_link = tmp_path / "wt_l", tmp_path / "dir_l", tmp_path / "envd_l", ws / "sub_l"
    for lnk, tgt in ((wt_link, wt_dir), (dir_link, ws), (env_d_link, ws / ".env.d"), (sub_link, ws / "sub")):
        _link_dir(lnk, tgt)
    for bo in (dir_link, wt_link, ws / ".worktrees", wt_dir / "m.md", ws / ".WORKTREES/m.md", ws / ".worktrees./m.md", ws / ".worktrees/../esc.md", wt_link / "m.md", wt_link / "../esc.md", env_f, env_loc, ws / ".ENV", ws / ".env.", ws / ".env.prod", dir_link / ".env", dir_link / ".ENV.staging", env_d_link / "m.md"):
        with pytest.raises(ValueError):
            cli._write_output(str(bo), "x", rig_dir)
        assert all((capsys.readouterr(), cli.main(["--rig-dir", str(rig_dir), "telemetry", *s, "--output", str(bo)]) == 1)[1] for s in (["sequence"], ["metrics"], ["metrics", "--json"]))
    with monkeypatch.context() as mp:
        mp.setattr(Path, "is_symlink", lambda self: self == sec_f)
        with pytest.raises(ValueError):
            cli._write_output(str(sec_f), "overwrite", rig_dir)
        assert (capsys.readouterr(), cli.main(["--rig-dir", str(rig_dir), "telemetry", "metrics", "--output", str(sec_f)]) == 1)[1]
    monkeypatch.chdir(wt_dir)
    assert (capsys.readouterr(), cli.main(["--rig-dir", str(rig_dir), "telemetry", "metrics", "--output", "rel.md"]) == 1)[1]
    assert env_f.read_text(encoding="utf-8") == "K=1" and env_loc.read_text(encoding="utf-8") == "K=2" and sec_f.read_text(encoding="utf-8") == "SEC" and cli.main(["--rig-dir", str(rig_dir), "telemetry", "metrics", "--output", str(tmp_path / ".envrc")]) == 0
    base_done = ["--rig-dir", str(rig_dir), "task", "done", "--id", "TSK-1", "--agent", "W", "--verifier", "R"]
    bad_ev = (*(["--evidence", f"note{c}.txt"] for c in ("\n", "\r", "\t", "\x01", "\x1f", "\x7f")), ["--evidence", "note.txt", "--evidence", "note.txt"], ["--evidence", "note.txt", "--evidence", "./note.txt"], ["--evidence", "note.txt", "--evidence", "sub/../note.txt"], ["--evidence", "note.txt", "--evidence", str(ws / "note.txt")], ["--evidence", "sub/a.txt", "--evidence", "sub_l/a.txt"])
    with monkeypatch.context() as mp:
        mp.setattr(cli, "StigmeRigDB", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DB accessed")))
        assert all(_rejects(capsys, [*base_done, *ev]) for ev in bad_ev)
    assert cli.main(["--rig-dir", str(rig_dir), "task", "claim", "--id", "TSK-1", "--agent", "W"]) == 0 and cli.main([*base_done, "--evidence", "note.txt", "--evidence", "sub/a.txt"]) == 0

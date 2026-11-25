"""Hermetic tests for CLI CR/LF, status, audit, upfront verifier, CWD .stigmerig, and .git output guards."""

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
    d = tmp_path / "ws" / ".stigmerig"
    (tmp_path / "ws").mkdir()
    assert cli.main(["--rig-dir", str(d), "init", "--no-icm"]) == 0 and cli.main(["--rig-dir", str(d), "task", "create", "--id", "TSK-1", "--title", "S"]) == 0
    return d


def test_crlf_audit_colon_status_and_upfront_verifier_gates(rig_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("STIGMERIG_ROOT", str(rig_dir))
    assert cli.main(["task", "claim", "--id", "TSK-1", "--agent", "WorkerA"]) == 0
    assert cli.main(["task", "done", "--id", "TSK-1", "--agent", "WorkerA", "--verifier", "ReviewerB"]) == 0
    snap = {n: (rig_dir / n).read_bytes() for n in ("queue.db", "audit.db")}
    bad_argvs = [
        *(["task", "claim", "--id", "TSK-1", "--agent", f"W{nl}1"] for nl in ("\n", "\r", "\r\n")),
        ["task", "done", "--id", "TSK-1", "--agent", "WorkerA", "--verifier", "Rev\nB"],
        ["task", "done", "--id", "TSK-1", "--agent", "WorkerA", "--verifier", "Rev\rB", "--allow-self-approval"],
        ["task", "done", "--id", "TSK-1", "--agent", "WorkerA", "--allow-self-approval", "--session-id", "s\n1"],
        ["task", "create", "--id", "TSK-2", "--title", "T", "--author", "Op\n1"], ["task", "create", "--id", "TSK-2", "--title", "T", "--archetype", "diff\rsynth"],
        ["task", "create", "--id", "TSK-2", "--title", "T", "--tags", "a,\nb"], ["task", "create", "--id", "TSK-2", "--title", "T", "--tags", "a\r,b"],
        ["audit", "log", "--task", "TSK-1", "--action", "act\n1", "--agent", "W", "--status", "ok"],
        ["audit", "log", "--task", "TSK-1", "--action", "act", "--agent", "W", "--status", "ok\r1"],
        ["audit", "log", "--task", "TSK-1", "--action", "bad:act", "--agent", "W", "--status", "ok"],
        ["audit", "log", "--task", "TSK-1", "--action", "act", "--agent", "W", "--status", "bad:st"],
        *(["task", "list", "--status", s] for s in ("invalid", "pending", "TODO", "in_progress", "done:1", "todo\n")),
        ["task", "done", "--id", "TSK-1", "--agent", "WorkerA"],
        *(["task", "done", "--id", "TSK-1", "--agent", a, "--verifier", v] for a, v in (("WorkerA", "WorkerA"), ("WorkerA", "workera"), ("role/A", "seat:A"), ("A@h", "A"), ("s:A", "A"))),
    ]
    with monkeypatch.context() as mp:
        mp.setattr(cli, "StigmeRigDB", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DB accessed")))
        assert all(_rejects(capsys, argv) for argv in bad_argvs)
    assert _rejects(capsys, ["task", "done", "--id", "TSK-1", "--agent", "WorkerA"]) and _rejects(capsys, ["task", "done", "--id", "TSK-1", "--agent", "WorkerA", "--verifier", "WorkerA"])
    assert {n: (rig_dir / n).read_bytes() for n in snap} == snap and all(cli.main(["task", "list", "--status", s]) == 0 for s in cli.VALID_TASK_STATUSES)
    assert cli.main(["task", "done", "--id", "TSK-1", "--agent", "WorkerA", "--allow-self-approval"]) == 0
    assert cli.main(["audit", "log", "--task", "TSK-1", "--action", "chk", "--agent", "seat:W", "--status", "ok", "--payload", "a:b\nc"]) == 0 and cli.main(["audit", "verify"]) == 0


def test_default_cwd_stigmerig_collision_and_git_output_guards(rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("STIGMERIG_ROOT", raising=False)
    f_cwd, d_cwd, ok_cwd, repo, wt, git_link = (tmp_path / n for n in ("f_cwd", "d_cwd", "ok_cwd", "repo", "wt", "git_link"))
    for p in (f_cwd, d_cwd, ok_cwd, repo / ".git" / "hooks", wt):
        p.mkdir(parents=True)
    (f_cwd / ".stigmerig").write_text("keep", encoding="utf-8")
    _link_dir(d_cwd / ".stigmerig", tmp_path / "vanished_default", dangling=True)
    for bad_cwd in (f_cwd, d_cwd):
        monkeypatch.chdir(bad_cwd)
        assert _rejects(capsys, ["init", "--no-icm"]) and _rejects(capsys, ["task", "list"])
    assert (f_cwd / ".stigmerig").read_text(encoding="utf-8") == "keep" and not (tmp_path / "vanished_default").exists()
    assert cli.main(["--rig-dir", str(rig_dir), "task", "list"]) == 0 and cli.main(["init", "--path", str(tmp_path / "exp"), "--no-icm"]) == 0
    monkeypatch.chdir(ok_cwd)
    assert cli.main(["init", "--no-icm"]) == 0 and cli.main(["task", "list"]) == 0
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (wt / ".git").write_text("gitdir: ../repo/.git/worktrees/wt\n", encoding="utf-8")
    _link_dir(git_link, repo / ".git")
    for bad_out in (repo / ".git", wt / ".git", repo / ".git/HEAD", repo / ".git/hooks/pre-commit", repo / ".GIT/config", repo / ".git./HEAD", repo / ".git/../esc.md", repo / "sub/../.git/HEAD", git_link / "HEAD", git_link / "sub/m.md"):
        with pytest.raises(ValueError):
            cli._write_output(str(bad_out), "x", rig_dir)
        assert all((capsys.readouterr(), cli.main(["--rig-dir", str(rig_dir), "telemetry", *s, "--output", str(bad_out)]) == 1 and "[ERROR]" in capsys.readouterr().err)[1] for s in (["sequence"], ["metrics"], ["metrics", "--json"]))
    monkeypatch.chdir(repo / ".git")
    assert (capsys.readouterr(), cli.main(["--rig-dir", str(rig_dir), "telemetry", "metrics", "--output", "local.md"]) == 1 and "[ERROR]" in capsys.readouterr().err)[1]
    assert (repo / ".git/HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n" and (wt / ".git").read_text(encoding="utf-8") == "gitdir: ../repo/.git/worktrees/wt\n" and not (repo / ".git/local.md").exists()
    assert cli.main(["--rig-dir", str(rig_dir), "telemetry", "metrics", "--output", str(repo / ".github/m.md")]) == 0


def test_control_escape_flags_and_worktree_prune_recovery(rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import json, shutil, subprocess
    from stigmerig.db import StigmeRigDB
    from stigmerig.wave import WorktreeManager
    c7, c6 = ("\x1b[31m", "\x07", "\x7f", "\n", "\r", "\t", "\x1f"), ("\x1b[31m", "\x07", "\x7f", "\x0b", "\x1f", "\x9b")
    bad_ctrl = [*(["--rig-dir", f"{rig_dir}{c}x", "task", "list"] for c in c7), *(["init", "--path", f"{tmp_path / 'p'}{c}x", "--no-icm"] for c in c7), *(["--rig-dir", str(rig_dir), "task", "create", "--id", "TSK-C", "--title", f"T{c}1"] for c in c6), *(["--rig-dir", str(rig_dir), "task", "fail", "--id", "TSK-1", "--agent", "W", "--reason", f"R{c}1"] for c in c6), *(["--rig-dir", str(rig_dir), "audit", "log", "--task", "TSK-1", "--action", "a", "--agent", "W", "--status", "ok", "--payload", f"P{c}1"] for c in c6)]
    with monkeypatch.context() as mp:
        mp.setattr(cli, "StigmeRigDB", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DB accessed")))
        assert all(_rejects(capsys, argv) for argv in bad_ctrl)
    assert all(cli.main(["--rig-dir", str(rig_dir), *c]) == 0 for c in (["task", "create", "--id", "TSK-ML", "--title", "L1\nL2\r\n\tL3"], ["task", "fail", "--id", "TSK-ML", "--agent", "W", "--reason", "R1\n\tR2\r\n"], ["audit", "log", "--task", "TSK-ML", "--action", "chk", "--agent", "W", "--status", "ok", "--payload", "P1\nP2\r\n\tP3"], ["audit", "verify"]))
    monkeypatch.chdir(rig_dir.parent)
    assert all((capsys.readouterr(), cli.main(a) == 1 and (e := capsys.readouterr().err).startswith("[ERROR]") and "git repository" in e.lower())[1] for a in (["--rig-dir", str(rig_dir), "worktree", "prune"], ["worktree", "prune"]))
    g_repo, g_rig = tmp_path / "git_ws", tmp_path / "git_ws" / ".stigmerig"
    g_repo.mkdir()
    for gc in (["init", "-b", "main"], ["commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@l", "-c", "commit.gpgsign=false", *gc], cwd=g_repo, check=True, capture_output=True)
    assert cli.main(["--rig-dir", str(g_rig), "init", "--no-icm"]) == 0
    db, wt_mgr = StigmeRigDB(rig_dir=g_rig), WorktreeManager(repo_root=g_repo)
    st_map = {"TSK-DONE": "done", "TSK-CANC1": "cancelled", "TSK-CANC2": "canceled", "TSK-ARCH": "archived", "TSK-WORK": "working", "TSK-BLK": "blocked", "TSK-TODO": "todo"}
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES (?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", [(tid, tid, st) for tid, st in st_map.items()])
        conn.commit()
    for tid in ("TSK-DONE", "TSK-CANC1", "TSK-WORK", "TSK-BLK", "TSK-STALE"):
        (wt_mgr.create_worktree(tid) / "diag.txt").write_text(tid, encoding="utf-8")
    for tid in ("TSK-CANC2", "TSK-ARCH", "TSK-TODO"):
        (g_repo / ".worktrees" / tid).mkdir(parents=True, exist_ok=True)
        (g_repo / ".worktrees" / tid / "diag.txt").write_text(tid, encoding="utf-8")
        subprocess.run(["git", "branch", f"task/{tid}"], cwd=g_repo, check=True, capture_output=True)
    shutil.rmtree(g_repo / ".worktrees" / "TSK-STALE")
    term_ids, keep_ids = ("TSK-DONE", "TSK-CANC1", "TSK-CANC2", "TSK-ARCH"), ("TSK-WORK", "TSK-BLK", "TSK-TODO")
    refs = lambda: set(subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/task/"], cwd=g_repo, check=True, capture_output=True, text=True).stdout.splitlines())
    assert (g_repo / ".git/worktrees/TSK-STALE").exists() and (capsys.readouterr(), cli.main(["--rig-dir", str(g_rig), "worktree", "prune"]))[1] == 0 and (out1 := json.loads(capsys.readouterr().out))["ok"]
    assert set(out1["removed_worktrees"]) == set(term_ids) and out1["deleted_branches"] == [] and not (g_repo / ".git/worktrees/TSK-STALE").exists() and {f"task/{t}" for t in (*term_ids, *keep_ids)} <= refs()
    assert all(not (g_repo / ".worktrees" / t).exists() for t in term_ids) and all((g_repo / ".worktrees" / k / "diag.txt").read_text(encoding="utf-8") == k for k in keep_ids)
    wt_mgr.create_worktree("TSK-DONE")
    assert (capsys.readouterr(), cli.main(["--rig-dir", str(g_rig), "worktree", "prune", "--delete-branches"]))[1] == 0 and (out2 := json.loads(capsys.readouterr().out))["removed_worktrees"] == ["TSK-DONE"]
    assert out2["dry_run"] is False and set(out2["deleted_branches"]) == {f"task/{t}" for t in term_ids} and refs() == {f"task/{k}" for k in (*keep_ids, "TSK-STALE")} and all((g_repo / ".worktrees" / k / "diag.txt").read_text(encoding="utf-8") == k for k in keep_ids)


def test_worktree_prune_dry_run_output_stigmerig_root_and_legacy_ids(rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import json, shutil, subprocess
    from stigmerig.db import StigmeRigDB
    from stigmerig.wave import WorktreeManager

    g_repo, g_rig, plain_cwd, link_cwd, dir_link, sym_f = (tmp_path / n for n in ("g_ws", "g_ws/.stigmerig", "plain_cwd", "link_cwd", "dir_link", "sym.json"))
    for p in (g_repo, plain_cwd, link_cwd):
        p.mkdir(parents=True, exist_ok=True)
    for gc in (["init", "-b", "main"], ["commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@l", "-c", "commit.gpgsign=false", *gc], cwd=g_repo, check=True, capture_output=True)
    assert cli.main(["--rig-dir", str(g_rig), "init", "--no-icm"]) == 0
    _link_dir(link_cwd / ".stigmerig", g_rig)
    _link_dir(dir_link, tmp_path / "real_out")
    monkeypatch.delenv("STIGMERIG_ROOT", raising=False)
    for bad_cwd in (plain_cwd, link_cwd):
        monkeypatch.chdir(bad_cwd)
        assert (capsys.readouterr(), cli.main(["worktree", "prune"]) == 1 and "git repository" in capsys.readouterr().err.lower())[1]
    monkeypatch.setenv("STIGMERIG_ROOT", str(rig_dir))
    assert (capsys.readouterr(), cli.main(["worktree", "prune"]) == 1 and "git repository" in capsys.readouterr().err.lower())[1]

    db, wt_mgr = StigmeRigDB(rig_dir=g_rig), WorktreeManager(repo_root=g_repo)
    rows = [
        ("TSK-DONE1", "done"), ("TSK-CANC", "cancelled"), ("TSK-KEEP", "working"),
        ("", "done"), ("   ", "done"), ("../TSK-KEEP", "done"), ("legacy/task-1", "cancelled"),
        ("bad space", "archived"), (".hidden", "done"), ("-dash", "canceled"), ("TSK:BAD", "done"), ("A" * 65, "done"), ("bad/open", "todo"),
    ]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES (?, 'T', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", rows)
        conn.commit()
    for tid in ("TSK-DONE1", "TSK-CANC", "TSK-KEEP", "TSK-ORPHAN"):
        (wt_mgr.create_worktree(tid) / "f.txt").write_text(tid, encoding="utf-8")
    shutil.rmtree(g_repo / ".worktrees" / "TSK-ORPHAN")

    monkeypatch.setenv("STIGMERIG_ROOT", str(g_rig))
    monkeypatch.chdir(plain_cwd)
    bad_outs = (sym_f, dir_link, g_repo / ".git/r.json", g_repo / ".worktrees/r.json", g_repo / ".worktrees/TSK-KEEP/r.json", g_repo / ".env", g_repo / ".env.local", g_rig, g_rig / "r.json", g_rig / "sub/r.json")
    with monkeypatch.context() as mp:
        orig_is_symlink = Path.is_symlink
        mp.setattr(Path, "is_symlink", lambda self: self == sym_f or orig_is_symlink(self))
        mp.setattr(cli, "StigmeRigDB", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("DB accessed")))
        mp.setattr(cli, "_prune_worktrees", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("prune called")))
        assert all(_rejects(capsys, ["worktree", "prune", "--output", str(bo)]) and _rejects(capsys, ["--rig-dir", str(g_rig), "worktree", "prune", "--output", str(bo)]) for bo in bad_outs)

    refs = lambda: set(subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/task/"], cwd=g_repo, check=True, capture_output=True, text=True).stdout.splitlines())
    intact = lambda: all((g_repo / ".worktrees" / t / "f.txt").is_file() and (g_repo / ".git/worktrees" / t).is_dir() for t in ("TSK-DONE1", "TSK-CANC", "TSK-KEEP")) and (g_repo / ".git/worktrees/TSK-ORPHAN").is_dir() and {f"task/{t}" for t in ("TSK-DONE1", "TSK-CANC", "TSK-KEEP", "TSK-ORPHAN")} <= refs()
    assert (capsys.readouterr(), cli.main(["worktree", "prune", "--dry-run"]))[1] == 0 and (d1 := json.loads(capsys.readouterr().out))["ok"] and d1["dry_run"] is True
    assert d1["repo_root"] == str(g_repo.resolve()) and set(d1["removed_worktrees"]) == {"TSK-DONE1", "TSK-CANC"} and d1["deleted_branches"] == [] and intact()

    calls, real_atomic, rep = [], cli._atomic_write_text, tmp_path / "reports" / "prune.json"
    with monkeypatch.context() as mp:
        mp.setattr(cli, "_atomic_write_text", lambda p, c: (calls.append(p), real_atomic(p, c))[1])
        assert (capsys.readouterr(), cli.main(["worktree", "prune", "--dry-run", "--delete-branches", "--output", str(rep)]))[1] == 0
        d2 = json.loads(capsys.readouterr().out)
    assert calls == [rep] and json.loads(rep.read_text(encoding="utf-8")) == d2 and d2["ok"] is True and d2["dry_run"] is True
    assert set(d2["removed_worktrees"]) == {"TSK-DONE1", "TSK-CANC"} and set(d2["deleted_branches"]) == {"task/TSK-DONE1", "task/TSK-CANC"} and intact()
    with monkeypatch.context() as mp:
        mp.setattr("stigmerig.db.os.replace", lambda *_a: (_ for _ in ()).throw(OSError("boom")))
        assert (capsys.readouterr(), cli.main(["worktree", "prune", "--dry-run", "--output", str(rep)]) == 1 and "[ERROR]" in capsys.readouterr().err)[1]
    assert json.loads(rep.read_text(encoding="utf-8")) == d2 and list(rep.parent.glob(".*.tmp")) == []

    final_rep = tmp_path / "reports" / "final.json"
    assert (capsys.readouterr(), cli.main(["worktree", "prune", "--delete-branches", "--output", str(final_rep)]))[1] == 0
    d3 = json.loads(final_rep.read_text(encoding="utf-8"))
    assert d3 == json.loads(capsys.readouterr().out) and d3["ok"] is True and d3["dry_run"] is False
    assert set(d3["removed_worktrees"]) == {"TSK-DONE1", "TSK-CANC"} and set(d3["deleted_branches"]) == {"task/TSK-DONE1", "task/TSK-CANC"}
    assert all(not (g_repo / ".worktrees" / t).exists() and not (g_repo / ".git/worktrees" / t).exists() for t in ("TSK-DONE1", "TSK-CANC", "TSK-ORPHAN"))
    assert (g_repo / ".worktrees/TSK-KEEP/f.txt").read_text(encoding="utf-8") == "TSK-KEEP" and refs() == {"task/TSK-KEEP", "task/TSK-ORPHAN"}


def test_unicode_format_bidi_separator_upfront_rejection(rig_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import sqlite3

    uni_bad = ("\u200b", "\ufeff", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069", "\u2028", "\u2029")
    assert all(cli._has_ctrl(f"a{c}b") and cli._has_ctrl(f"a{c}b", allow_ws=True) and cli._has_ctrl(c, allow_ws=True) for c in uni_bad)
    assert not cli._has_ctrl("ok é\t\n\r", allow_ws=True) and cli._has_ctrl("ok\n", allow_ws=False)

    argvs = [
        argv
        for c in uni_bad
        for argv in (
            ["--rig-dir", str(rig_dir), "task", "create", "--id", "TSK-U", "--title", f"L1\n{c}L2"],
            ["--rig-dir", str(rig_dir), "task", "fail", "--id", "TSK-1", "--agent", "W", "--reason", f"R1\t{c}R2"],
            ["--rig-dir", str(rig_dir), "audit", "log", "--task", "TSK-1", "--action", "a", "--agent", "W", "--status", "ok", "--payload", f"P1\r\n{c}P2"],
            ["--rig-dir", str(rig_dir), "task", "get", "--id", f"TSK-{c}1"],
            ["--rig-dir", str(rig_dir), "task", "create", "--id", "TSK-U", "--title", "T", "--parent", f"TSK-{c}1"],
            ["--rig-dir", str(rig_dir), "audit", "log", "--task", f"TSK-{c}1", "--action", "a", "--agent", "W", "--status", "ok"],
            ["--rig-dir", str(rig_dir), "telemetry", "sequence", "--task", f"TSK-{c}1"],
            ["--rig-dir", str(rig_dir), "telemetry", "metrics", "--output", f"{tmp_path}/out{c}.md"],
            ["--rig-dir", str(rig_dir), "worktree", "prune", "--dry-run", "--output", f"{tmp_path}/out{c}.json"],
            ["--rig-dir", f"{rig_dir}{c}", "task", "list"],
            ["init", "--path", f"{tmp_path}/{c}rig", "--no-icm"],
            ["--rig-dir", str(rig_dir), "task", "done", "--id", "TSK-1", "--agent", "W", "--verifier", "V", "--evidence", f"ev{c}.txt"],
        )
    ]
    boom = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("FS or DB accessed"))
    with monkeypatch.context() as mp:
        for fn in ("resolve", "exists", "is_dir", "is_file", "is_symlink"):
            mp.setattr(Path, fn, boom)
        mp.setattr(cli, "StigmeRigDB", boom)
        mp.setattr(sqlite3, "connect", boom)
        mp.setattr(cli, "_atomic_write_text", boom)
        assert all(_rejects(capsys, argv) for argv in argvs)
        for c in uni_bad:
            with pytest.raises(ValueError):
                cli._write_output(f"{tmp_path}/out{c}.txt", "body", rig_dir)
        mp.setenv("STIGMERIG_ROOT", f"{rig_dir}\u200b")
        assert _rejects(capsys, ["task", "list"])


def test_prune_worktrees_lock_midop_and_external_linked_worktrees(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json, shutil, subprocess, threading
    from stigmerig.db import StigmeRigDB
    from stigmerig.wave import GIT_IN_PROGRESS_MARKERS, WorktreeManager, _git_dir

    g_repo, g_rig, ext_dir = tmp_path / "repo", tmp_path / "repo/.stigmerig", tmp_path / "ext"
    for p in (g_repo, ext_dir):
        p.mkdir(parents=True)
    for gc in (["init", "-b", "main"], ["commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@l", "-c", "commit.gpgsign=false", *gc], cwd=g_repo, check=True, capture_output=True)
    assert cli.main(["--rig-dir", str(g_rig), "init", "--no-icm"]) == 0
    db, wt_mgr = StigmeRigDB(rig_dir=g_rig), WorktreeManager(repo_root=g_repo)

    class _TrackedRLock:
        def __init__(self) -> None:
            self._rl = threading.RLock()
            self.depth = self.enters = self.exits = 0

        def __enter__(self) -> "_TrackedRLock":
            self._rl.__enter__()
            self.depth += 1
            self.enters += 1
            return self

        def __exit__(self, *exc: object) -> None:
            self.depth -= 1
            self.exits += 1
            self._rl.__exit__(*exc)

    tlock = _TrackedRLock()
    wt_mgr._lock = tlock  # type: ignore[assignment]
    held_ready, release_gate, order = threading.Event(), threading.Event(), []

    def _holder() -> None:
        with wt_mgr._lock:
            held_ready.set()
            release_gate.wait(timeout=5)
            order.append("released")

    th = threading.Thread(target=_holder)
    th.start()
    assert held_ready.wait(timeout=5)
    t_prune = threading.Thread(target=lambda: (cli._prune_worktrees(db, g_repo, dry_run=True, wt_mgr=wt_mgr), order.append("pruned")))
    t_prune.start()
    assert t_prune.is_alive()
    release_gate.set()
    th.join(timeout=5)
    t_prune.join(timeout=5)
    assert order == ["released", "pruned"] and tlock.enters >= 2 and tlock.exits == tlock.enters and tlock.depth == 0

    mid_ids = [f"TSK-OP{i}" for i in range(len(GIT_IN_PROGRESS_MARKERS))]
    all_term = ["TSK-CLEAN", "TSK-LOCK", *mid_ids, "TSK-EXTBR", "TSK-EXTRB", "TSK-BASE", "TSK-NOGD"]
    with db.get_queue_conn() as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES (?, 'T', 'done', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", [(t,) for t in all_term])
        conn.commit()
    for tid in ("TSK-CLEAN", "TSK-LOCK", *mid_ids):
        (wt_mgr.create_worktree(tid) / "f.txt").write_text(tid, encoding="utf-8")
    subprocess.run(["git", "worktree", "lock", str(g_repo / ".worktrees/TSK-LOCK")], cwd=g_repo, check=True, capture_output=True)
    for tid, marker in zip(mid_ids, GIT_IN_PROGRESS_MARKERS):
        mp = _git_dir(g_repo / ".worktrees" / tid) / marker
        mp.mkdir() if marker.startswith("rebase-") else mp.write_text("deadbeef\n", encoding="utf-8")

    subprocess.run(["git", "branch", "task/TSK-EXTRB"], cwd=g_repo, check=True, capture_output=True)
    subprocess.run(["git", "worktree", "add", "-b", "task/TSK-EXTBR", str(ext_dir / "linked-ext")], cwd=g_repo, check=True, capture_output=True)
    ext_rbm = _git_dir(ext_dir / "linked-ext") / "rebase-merge"
    ext_rbm.mkdir()
    (ext_rbm / "head-name").write_text("refs/heads/task/TSK-EXTRB\n", encoding="utf-8")
    (g_repo / ".worktrees/TSK-EXTBR").mkdir(parents=True)
    (g_repo / ".git/worktrees/TSK-NOGD").mkdir(parents=True)
    (g_repo / ".git/worktrees/TSK-NOGD/locked").write_text("hold\n", encoding="utf-8")
    subprocess.run(["git", "worktree", "add", "-b", "task/TSK-BASE", str(ext_dir / "TSK-BASE")], cwd=g_repo, check=True, capture_output=True)
    assert (g_repo / ".git/worktrees/TSK-BASE").is_dir() and not (g_repo / ".worktrees/TSK-BASE").exists()

    refs = lambda: set(subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/task/"], cwd=g_repo, check=True, capture_output=True, text=True).stdout.splitlines())
    dr1 = cli._prune_worktrees(db, g_repo, delete_branches=True, dry_run=True, wt_mgr=wt_mgr)
    assert set(dr1["removed_worktrees"]) == {"TSK-CLEAN", "TSK-EXTBR"} and dr1["deleted_branches"] == ["task/TSK-CLEAN"]

    lv1 = cli._prune_worktrees(db, g_repo, delete_branches=True, dry_run=False, wt_mgr=wt_mgr)
    assert set(lv1["removed_worktrees"]) == {"TSK-CLEAN", "TSK-EXTBR"} and lv1["deleted_branches"] == ["task/TSK-CLEAN"]
    assert not (g_repo / ".worktrees/TSK-CLEAN").exists() and not (g_repo / ".worktrees/TSK-EXTBR").exists() and (g_repo / ".git/worktrees/TSK-NOGD").is_dir()
    assert all((g_repo / ".worktrees" / t).is_dir() for t in ("TSK-LOCK", *mid_ids))
    assert (ext_dir / "linked-ext").is_dir() and (ext_dir / "TSK-BASE").is_dir()
    assert refs() == {f"task/{t}" for t in ("TSK-LOCK", *mid_ids, "TSK-EXTBR", "TSK-EXTRB", "TSK-BASE")}

    (g_repo / ".git/worktrees/TSK-NOGD/locked").unlink()
    subprocess.run(["git", "checkout", "-b", "other-br"], cwd=ext_dir / "TSK-BASE", check=True, capture_output=True)
    subprocess.run(["git", "worktree", "lock", str(ext_dir / "TSK-BASE")], cwd=g_repo, check=True, capture_output=True)
    (g_repo / ".git/worktrees/TSK-BASE/MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    assert (capsys.readouterr(), cli.main(["--rig-dir", str(g_rig), "worktree", "prune", "--dry-run", "--delete-branches"]))[1] == 0
    dr2 = json.loads(capsys.readouterr().out)
    assert dr2["removed_worktrees"] == ["TSK-NOGD"] and dr2["deleted_branches"] == ["task/TSK-BASE"]

    (g_repo / ".worktrees/TSK-BASE").mkdir(parents=True)
    assert (capsys.readouterr(), cli.main(["--rig-dir", str(g_rig), "worktree", "prune", "--delete-branches"]))[1] == 0
    lv2 = json.loads(capsys.readouterr().out)
    assert set(lv2["removed_worktrees"]) == {"TSK-BASE", "TSK-NOGD"} and lv2["deleted_branches"] == ["task/TSK-BASE"]
    assert not (g_repo / ".worktrees/TSK-BASE").exists() and not (g_repo / ".git/worktrees/TSK-NOGD").exists() and (ext_dir / "TSK-BASE").is_dir() and (g_repo / ".git/worktrees/TSK-BASE").is_dir() and "task/TSK-BASE" not in refs()

    shutil.rmtree(ext_rbm)
    subprocess.run(["git", "worktree", "unlock", str(g_repo / ".worktrees/TSK-LOCK")], cwd=g_repo, check=True, capture_output=True)
    for tid, marker in zip(mid_ids, GIT_IN_PROGRESS_MARKERS):
        mp = _git_dir(g_repo / ".worktrees" / tid) / marker
        shutil.rmtree(mp) if mp.is_dir() else mp.unlink()
    lv3 = cli._prune_worktrees(db, g_repo, delete_branches=True, dry_run=False, wt_mgr=wt_mgr)
    assert set(lv3["removed_worktrees"]) == {"TSK-LOCK", *mid_ids} and set(lv3["deleted_branches"]) == {f"task/{t}" for t in ("TSK-LOCK", *mid_ids, "TSK-EXTRB")}
    assert refs() == {"task/TSK-EXTBR"}


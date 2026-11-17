"""Hermetic tests for bounded AGENTS.md markers, SHA-256 drift checks, and --sync-icm."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from stigmerig import cli
from stigmerig.cli import main
from stigmerig.db import CANONICAL_AGENTS_SHA256, DEFAULT_AGENTS_TEMPLATE, StigmeRigDB


def _make_dir_link(link: Path, target: Path, *, dangling: bool = False) -> bool:
    """Create a directory symlink or Windows NTFS junction; return False if unsupported."""
    target.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        link.symlink_to(target, target_is_directory=True)
        created = True
    except OSError:
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            created = True
        except (ImportError, OSError):
            created = False
    if created and dangling:
        target.rmdir()
    return created


def _make_file_symlink_or_patch(
    link: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dangling: bool = False,
) -> None:
    """Create a real file symlink, or simulate one via Path.is_symlink if OS forbids it."""
    if not dangling:
        target.write_text("# Target\n", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError:
        if not dangling:
            link.write_text("# Simulated link\n", encoding="utf-8")
        orig_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: self == link or orig_is_symlink(self),
        )


def test_fresh_init_writes_bounded_markers_and_check_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fresh init writes canonical SHA-256 bounded markers and init --check exits 0."""
    project_dir = tmp_path / "fresh_proj"
    project_dir.mkdir()
    rig_dir = project_dir / ".stigmerig"

    assert main(["--rig-dir", str(rig_dir), "init"]) == 0
    capsys.readouterr()

    agents_bytes = (project_dir / "AGENTS.md").read_bytes()
    agents_text = agents_bytes.decode("utf-8")
    assert f"<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->" in agents_text
    assert "<!-- stigmerig:end -->" in agents_text
    assert "Autonomous Operation Invariants" in agents_text
    assert b"\r\n" not in agents_bytes

    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 0
    out = capsys.readouterr().out
    assert CANONICAL_AGENTS_SHA256 in out

    # Also verify --path support on init --check
    assert main(["init", "--path", str(rig_dir), "--check"]) == 0
    capsys.readouterr()


def test_uninitialized_dir_check_fails_without_creating_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """init --check on an uninitialized workspace exits 1 and creates zero directories or DBs."""
    ws = tmp_path / "uninit_ws"
    ws.mkdir()
    rig_dir = ws / ".stigmerig"

    rc = main(["--rig-dir", str(rig_dir), "init", "--check"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "[ERROR]" in captured.err
    assert "stigmerig init --sync-icm" in captured.err
    assert list(ws.iterdir()) == []
    assert not rig_dir.exists()
    assert not (rig_dir / "queue.db").exists()
    assert not (rig_dir / "audit.db").exists()


def test_tampered_agents_fails_check_without_sidecar_masking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drift in AGENTS.md bounded block or header hash fails --check even when sidecar is synced."""
    ws = tmp_path / "drift_ws"
    rig_dir = ws / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig(scaffold_icm=True)

    # Plant a fully canonical sidecar alongside AGENTS.md.
    (rig_dir / "AGENTS.stigmerig.md").write_bytes(DEFAULT_AGENTS_TEMPLATE.encode("utf-8"))
    agents_file = ws / "AGENTS.md"
    original = agents_file.read_text(encoding="utf-8")

    # Case 1: Tampered inner directive with original header hash.
    tampered_body = original.replace("Local-First Stigmergy", "Tampered Stigmergy")
    agents_file.write_bytes(tampered_body.encode("utf-8"))
    ok, msg = db.check_icm()
    assert ok is False
    assert "stigmerig init --sync-icm" in msg
    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 1
    assert "[ERROR]" in capsys.readouterr().err

    # Case 2: Canonical inner body with mismatched header hash.
    bad_hash = "0" * 16
    assert bad_hash != CANONICAL_AGENTS_SHA256
    tampered_header = original.replace(CANONICAL_AGENTS_SHA256, bad_hash)
    agents_file.write_bytes(tampered_header.encode("utf-8"))
    ok_hdr, msg_hdr = db.check_icm()
    assert ok_hdr is False
    assert "stigmerig init --sync-icm" in msg_hdr
    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 1
    assert "[ERROR]" in capsys.readouterr().err


@pytest.mark.parametrize("newline_seq", ["\n", "\r\n"])
def test_sync_icm_repairs_drift_preserving_prose_and_line_endings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], newline_seq: str
) -> None:
    """init --sync-icm surgically updates bounded block while preserving outer prose and EOL style."""
    eol_kind = "crlf" if newline_seq == "\r\n" else "lf"
    ws = tmp_path / f"eol_{eol_kind}"
    rig_dir = ws / ".stigmerig"
    db = StigmeRigDB(rig_dir=rig_dir)
    db.init_rig(scaffold_icm=True)

    stale_hash = hashlib.sha256(b"stale-body").hexdigest()[:16]
    prose_above = (
        f"# Custom Workspace Header{newline_seq}{newline_seq}"
        f"Team notes above the managed block.{newline_seq}{newline_seq}"
    )
    prose_below = (
        f"{newline_seq}## Team Footer{newline_seq}"
        f"- Keep custom operational rules intact.{newline_seq}"
    )
    stale_doc = (
        f"{prose_above}"
        f"<!-- stigmerig:begin sha256={stale_hash} -->{newline_seq}"
        f"## Outdated Rules{newline_seq}Old directive text.{newline_seq}"
        f"<!-- stigmerig:end -->{newline_seq}"
        f"{prose_below}"
    )
    agents_file = ws / "AGENTS.md"
    agents_file.write_bytes(stale_doc.encode("utf-8"))

    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 1
    capsys.readouterr()

    assert main(["--rig-dir", str(rig_dir), "init", "--sync-icm"]) == 0
    capsys.readouterr()

    raw_after = agents_file.read_bytes()
    if newline_seq == "\r\n":
        assert b"\r\n" in raw_after
        assert b"\r\r\n" not in raw_after
        assert b"\n" not in raw_after.replace(b"\r\n", b"")
    else:
        assert b"\r\n" not in raw_after
        assert b"\n" in raw_after

    text_after = raw_after.decode("utf-8")
    assert text_after.startswith(prose_above)
    assert text_after.endswith(prose_below)
    assert "Outdated Rules" not in text_after
    assert f"<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->" in text_after
    assert "Autonomous Operation Invariants" in text_after

    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 0
    capsys.readouterr()


def test_markerless_agents_sidecar_fallback_and_sync_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Markerless AGENTS.md falls back to sidecar; --sync-icm refreshes stale sidecar only."""
    ws = tmp_path / "sidecar_ws"
    ws.mkdir()
    rig_dir = ws / ".stigmerig"
    agents_file = ws / "AGENTS.md"
    custom_bytes = (
        b"# Project Agents\n\n"
        b"- Reference note: `<!-- stigmerig:begin sha256=1111111111111111 -->` in docs.\n"
    )
    agents_file.write_bytes(custom_bytes)

    # First init writes canonical sidecar without modifying AGENTS.md.
    assert main(["--rig-dir", str(rig_dir), "init"]) == 0
    capsys.readouterr()
    assert agents_file.read_bytes() == custom_bytes
    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 0
    capsys.readouterr()

    # Stale or markerless sidecar fails --check.
    sidecar = rig_dir / "AGENTS.stigmerig.md"
    sidecar.write_bytes(b"# Legacy sidecar without markers\n")
    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 1
    assert "stigmerig init --sync-icm" in capsys.readouterr().err

    sidecar.write_bytes(
        DEFAULT_AGENTS_TEMPLATE.replace("Worktree Isolation", "Stale Isolation").encode("utf-8")
    )
    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 1
    capsys.readouterr()

    # --sync-icm overwrites stale sidecar, leaves AGENTS.md untouched, and prints embed snippet.
    assert main(["init", "--path", str(rig_dir), "--sync-icm"]) == 0
    out = capsys.readouterr().out
    assert "AGENTS.stigmerig.md" in out
    assert f"<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->" in out
    assert "<!-- stigmerig:end -->" in out
    assert agents_file.read_bytes() == custom_bytes
    assert sidecar.read_bytes().decode("utf-8") == DEFAULT_AGENTS_TEMPLATE

    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 0
    capsys.readouterr()

    # Missing sidecar with markerless AGENTS.md fails --check.
    sidecar.unlink()
    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 1
    capsys.readouterr()


@pytest.mark.parametrize(
    "bad_content",
    [
        # Unclosed begin marker
        f"# AGENTS\n<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->\nbody\n",
        # Reversed markers (end before begin)
        f"# AGENTS\n<!-- stigmerig:end -->\n<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->\n",
        # Nested begin markers
        (
            f"# AGENTS\n<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->\n"
            f"<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->\n"
            "<!-- stigmerig:end -->\n"
        ),
        # Duplicate marker pair
        (
            f"# AGENTS\n<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->\n"
            "<!-- stigmerig:end -->\n"
            f"<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->\n"
            "<!-- stigmerig:end -->\n"
        ),
        # Duplicate end marker
        (
            f"# AGENTS\n<!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} -->\n"
            "<!-- stigmerig:end -->\n"
            "<!-- stigmerig:end -->\n"
        ),
        # Malformed begin attributes
        "# AGENTS\n<!-- stigmerig:begin sha256=short -->\n<!-- stigmerig:end -->\n",
        "# AGENTS\n<!-- stigmerig:begin -->\n<!-- stigmerig:end -->\n",
        "# AGENTS\n<!-- stigmerig:unknown -->\n",
        f"# AGENTS\n  <!-- stigmerig:begin sha256={CANONICAL_AGENTS_SHA256} --> trailing\n<!-- stigmerig:end -->\n",
    ],
)
def test_malformed_duplicate_nested_reversed_unclosed_markers_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_content: str
) -> None:
    """Invalid marker topologies raise ValueError in Python API and exit 1 via CLI."""
    ws = tmp_path / "bad_markers_ws"
    ws.mkdir()
    rig_dir = ws / ".stigmerig"
    (ws / "AGENTS.md").write_bytes(bad_content.encode("utf-8"))

    # Even with a valid sidecar, malformed AGENTS.md must never fall back to sidecar.
    rig_dir.mkdir()
    (rig_dir / "AGENTS.stigmerig.md").write_bytes(DEFAULT_AGENTS_TEMPLATE.encode("utf-8"))

    db = StigmeRigDB(rig_dir=rig_dir)
    with pytest.raises(ValueError):
        db.check_icm()
    with pytest.raises(ValueError):
        db.scaffold_icm(sync_icm=True)
    with pytest.raises(ValueError):
        db.init_rig(scaffold_icm=True, sync_icm=True)

    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 1
    assert "[ERROR]" in capsys.readouterr().err
    assert main(["--rig-dir", str(rig_dir), "init", "--sync-icm"]) == 1
    assert "[ERROR]" in capsys.readouterr().err
    assert not (rig_dir / "queue.db").exists()
    assert not (rig_dir / "audit.db").exists()


def test_inline_backtick_prose_markers_are_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inline prose backticks mentioning markers do not trigger malformed-marker errors."""
    ws = tmp_path / "prose_backticks_ws"
    ws.mkdir()
    rig_dir = ws / ".stigmerig"
    prose = (
        "# Team Agents\n\n"
        "- Embed `<!-- stigmerig:begin sha256=deadbeef01234567 -->` and `<!-- stigmerig:end -->` to sync.\n"
        "  - Indented bullet `<!-- stigmerig:unknown -->` stays prose.\n"
    )
    (ws / "AGENTS.md").write_bytes(prose.encode("utf-8"))

    assert main(["--rig-dir", str(rig_dir), "init"]) == 0
    capsys.readouterr()
    assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 0
    capsys.readouterr()
    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == prose


def test_mutually_exclusive_init_flags_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject --check --sync-icm, --no-icm --check, and --no-icm --sync-icm cleanly."""
    rig_dir = tmp_path / "exclusive_ws" / ".stigmerig"
    parser = cli.build_parser()

    for flags in (
        ["init", "--check", "--sync-icm"],
        ["init", "--no-icm", "--check"],
        ["init", "--no-icm", "--sync-icm"],
    ):
        argv = ["--rig-dir", str(rig_dir), *flags]
        with pytest.raises(ValueError):
            cli._validate_cli_args(parser.parse_args(argv))
        assert main(argv) == 1
        assert "[ERROR]" in capsys.readouterr().err

    assert not rig_dir.exists()
    db = StigmeRigDB(rig_dir=rig_dir)
    with pytest.raises(ValueError):
        db.init_rig(scaffold_icm=False, sync_icm=True)


def test_symlink_and_junction_rejection_across_icm_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject symlinks and junctions on AGENTS.md, sidecar, rig_dir, and parent_dir."""
    # 1. File symlink on AGENTS.md (live and dangling).
    for dangling in (False, True):
        ws = tmp_path / f"agents_link_{dangling}"
        ws.mkdir()
        rig_dir = ws / ".stigmerig"
        agents_link = ws / "AGENTS.md"
        _make_file_symlink_or_patch(agents_link, tmp_path / f"target_agents_{dangling}.md", monkeypatch, dangling=dangling)
        db = StigmeRigDB(rig_dir=rig_dir)
        with pytest.raises(ValueError):
            db.check_icm()
        with pytest.raises(ValueError):
            db.init_rig(scaffold_icm=True, sync_icm=False)
        assert main(["--rig-dir", str(rig_dir), "init", "--check"]) == 1
        assert "[ERROR]" in capsys.readouterr().err
        assert main(["--rig-dir", str(rig_dir), "init", "--sync-icm"]) == 1
        assert "[ERROR]" in capsys.readouterr().err
        assert not (rig_dir / "queue.db").exists()

    # 2. File symlink on sidecar (.stigmerig/AGENTS.stigmerig.md).
    ws_side = tmp_path / "sidecar_link_ws"
    ws_side.mkdir()
    rig_side = ws_side / ".stigmerig"
    rig_side.mkdir()
    (ws_side / "AGENTS.md").write_text("# Custom\n", encoding="utf-8")
    sidecar_link = rig_side / "AGENTS.stigmerig.md"
    _make_file_symlink_or_patch(sidecar_link, tmp_path / "target_sidecar.md", monkeypatch)
    db_side = StigmeRigDB(rig_dir=rig_side)
    with pytest.raises(ValueError):
        db_side.check_icm()
    with pytest.raises(ValueError):
        db_side.scaffold_icm(sync_icm=True)
    assert main(["--rig-dir", str(rig_side), "init", "--check"]) == 1
    assert "[ERROR]" in capsys.readouterr().err

    # 3. Directory symlink/junction on rig_dir.
    ws_rig_link = tmp_path / "rig_link_ws"
    ws_rig_link.mkdir()
    real_rig = tmp_path / "real_rig_target"
    rig_link = ws_rig_link / ".stigmerig"
    if not _make_dir_link(rig_link, real_rig):
        rig_link.mkdir()
        orig_is_sym = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == rig_link or orig_is_sym(self))
    db_rig = StigmeRigDB(rig_dir=rig_link)
    with pytest.raises(ValueError):
        db_rig.check_icm()
    with pytest.raises(ValueError):
        db_rig.init_rig(scaffold_icm=True, sync_icm=False)
    assert main(["--rig-dir", str(rig_link), "init", "--check"]) == 1
    assert "[ERROR]" in capsys.readouterr().err
    assert main(["--rig-dir", str(rig_link), "init", "--sync-icm"]) == 1
    assert "[ERROR]" in capsys.readouterr().err
    assert not (real_rig / "queue.db").exists()

    # 4. Directory symlink/junction on parent_dir.
    real_parent = tmp_path / "real_parent_target"
    parent_link = tmp_path / "parent_link_ws"
    if not _make_dir_link(parent_link, real_parent):
        parent_link.mkdir()
        orig_is_sym2 = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == parent_link or orig_is_sym2(self))
    parent_rig = parent_link / ".stigmerig"
    db_parent = StigmeRigDB(rig_dir=parent_rig)
    with pytest.raises(ValueError):
        db_parent.check_icm()
    with pytest.raises(ValueError):
        db_parent.init_rig(scaffold_icm=True, sync_icm=True)
    assert main(["--rig-dir", str(parent_rig), "init", "--check"]) == 1
    assert "[ERROR]" in capsys.readouterr().err
    assert not (real_parent / ".stigmerig").exists()

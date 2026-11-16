"""Polyglot epistemic and static trap invariant engine for StigmeRig."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
from functools import lru_cache
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


POLYGLOT_EXTENSIONS: frozenset[str] = frozenset({
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".kts",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".zig",
    ".sql",
    ".sh",
    ".bash",
    ".ps1",
    ".tf",
    ".hcl",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".svelte",
    ".vue",
})

DEFAULT_PRUNED_DIRS: frozenset[str] = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".stigmerig",
    ".worktrees",
    "node_modules",
    "target",
    "dist",
    "build",
    "out",
    "vendor",
    ".next",
    ".nuxt",
    "bin",
    "obj",
    ".gradle",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "coverage",
})

DEFAULT_EXCLUDED_FILENAMES: Tuple[str, ...] = ("traps.json", "stigmerig.traps.json", "Trap_Invariants.json")
MAX_PATTERN_LENGTH: int = 200
MAX_FILE_SIZE: int = 2 * 1024 * 1024

_NON_PRESCRIPTIVE_PLACEHOLDERS: frozenset[str] = frozenset({"n/a", "na", "none", "tbd"})
_BARE_ATOM_PATTERN: re.Pattern[str] = re.compile(r"^ATOM-[A-Za-z0-9_-]+$")
_TRAP_NUM_PATTERN: re.Pattern[str] = re.compile(r"^TRAP-(\d+)$")

_LINK_REPARSE_TAGS: frozenset[int] = frozenset({
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
})

_NON_TEST_CODE_GLOBS: Tuple[str, ...] = (
    "!tests/**",
    "!**/tests/**",
    "!**/__tests__/**",
    "!**/test_*.py",
    "!**/*_test.*",
    "!**/*.test.*",
    "!**/*.spec.*",
)

# Starter pack positive fixtures escape trigger prefixes (\x23 = '#', \x3c = '<',
# \x40 = '@') so scanning traps.py itself never produces self-referential matches.
STARTER_TRAP_PACKS: Dict[str, Tuple[Dict[str, Any], ...]] = {
    "generic": (
        {
            "trap_id": "TRAP-GEN-001",
            "domain": "vcs-hygiene",
            "title": "Unresolved Git Merge Conflict Marker",
            "trigger_pattern": r"^(?:<{7}|>{7}|\|{7})\s+\S+",
            "forbidden_claim_or_action": (
                "Leaving unresolved git merge conflict markers in source, configuration, or documentation files."
            ),
            "remediation": (
                "Resolve all merge conflict branches completely and run git diff --check before committing."
            ),
            "match_mode": "line",
            "target_globs": ["**", "!tests/**", "!**/tests/**"],
            "fixtures": {
                "positive": "\x3c\x3c\x3c\x3c\x3c\x3c\x3c HEAD\nconst port = 8080;\n=======\nconst port = 9090;\n\x3e\x3e\x3e\x3e\x3e\x3e\x3e main",
                "negative": "Summary\n=======\nconst port = 8080;",
            },
        },
    ),
    "python": (
        {
            "trap_id": "TRAP-PY-001",
            "domain": "python-hygiene",
            "title": "Silent Broad Exception Pass",
            "trigger_pattern": (
                r"^([ \t]*)except\s+(?:Exception|BaseException)(?:\s+as\s+[A-Za-z_]\w*)?\s*:"
                r"(?:[ \t]*pass\b|[ \t]*(?:#[^\r\n]*)?\r?\n\1[ \t]+pass\b)"
            ),
            "forbidden_claim_or_action": (
                "Swallowing broad Exception or BaseException blocks with a silent pass statement."
            ),
            "remediation": (
                "Catch narrow exception tuples (such as OSError or ValueError) and handle or log failures explicitly per ruff S110."
            ),
            "match_mode": "multiline",
            "target_globs": ["**/*.py", "**/*.pyi", *_NON_TEST_CODE_GLOBS],
            "fixtures": {
                "positive": "def read_cfg():\n    try:\n        return load()\n    ex" + "cept Exception:\n        pass",
                "negative": "def read_cfg():\n    try:\n        return load()\n    except (OSError, ValueError) as exc:\n        logger.warning('cfg error: %s', exc)",
            },
        },
        {
            "trap_id": "TRAP-PY-002",
            "domain": "python-hygiene",
            "title": "Unscoped Type Ignore Comment",
            "trigger_pattern": r"#\s*type:\s*ignore(?!\s*\x5b[A-Za-z0-9_,\s-]+\x5d)",
            "forbidden_claim_or_action": (
                "Using bare \x23 type: ignore comments without scoping the specific error code in brackets."
            ),
            "remediation": (
                "Resolve the static type mismatch or scope the suppression to an explicit code like \x23 type: ignore[attr-defined] per mypy ignore-without-code and ruff PGH003."
            ),
            "match_mode": "line",
            "target_globs": ["**/*.py", "**/*.pyi", *_NON_TEST_CODE_GLOBS],
            "fixtures": {
                "positive": "result = dynamic_dispatch(payload)  \x23 type: ignore",
                "negative": "result = dynamic_dispatch(payload)  \x23 type: ignore[no-any-return]",
            },
        },
    ),
    "typescript": (
        {
            "trap_id": "TRAP-TS-001",
            "domain": "typescript-hygiene",
            "title": "Unjustified TypeScript Directive Suppression",
            "trigger_pattern": r"//\s*@ts-(?:ignore|nocheck)\b(?![ \t]*(?::|--|-)?[ \t]*[A-Za-z0-9_]{3,})",
            "forbidden_claim_or_action": (
                "Suppressing TypeScript compiler diagnostics with bare // @ts-ignore or // @ts-nocheck without an inline rationale."
            ),
            "remediation": (
                "Fix the type error or replace with @ts-expect-error followed by a root-cause rationale per @typescript-eslint/ban-ts-comment."
            ),
            "match_mode": "line",
            "target_globs": [
                "**/*.ts",
                "**/*.tsx",
                "**/*.js",
                "**/*.jsx",
                "**/*.mjs",
                "**/*.cjs",
                "**/*.svelte",
                "**/*.vue",
                *_NON_TEST_CODE_GLOBS,
            ],
            "fixtures": {
                "positive": "// \x40ts-ignore\nconst unsafeVal: number = rawInput;",
                "negative": "// \x40ts-ignore -- vendor typedef omits optional signal parameter\nconst unsafeVal: number = rawInput;",
            },
        },
        {
            "trap_id": "TRAP-TS-002",
            "domain": "typescript-hygiene",
            "title": "Empty Promise Catch Handler",
            "trigger_pattern": (
                r"\.catch\(\s*(?:(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|function\s*\([^)]*\))\s*\{\s*\}\s*\)"
            ),
            "forbidden_claim_or_action": (
                "Swallowing rejected promises with an empty .catch(() => {}) callback."
            ),
            "remediation": (
                "Log diagnostic context, return a typed fallback value, or rethrow inside promise catch handlers per @typescript-eslint/no-empty-function."
            ),
            "match_mode": "multiline",
            "target_globs": [
                "**/*.ts",
                "**/*.tsx",
                "**/*.js",
                "**/*.jsx",
                "**/*.mjs",
                "**/*.cjs",
                "**/*.svelte",
                "**/*.vue",
                *_NON_TEST_CODE_GLOBS,
            ],
            "fixtures": {
                "positive": "await syncRemoteState().catch" + "(() => {});",
                "negative": "await syncRemoteState().catch((err) => { console.error('sync failed', err); throw err; });",
            },
        },
    ),
    "rust": (
        {
            "trap_id": "TRAP-RS-001",
            "domain": "rust-hygiene",
            "title": "Blanket Rust Warnings Suppression",
            "trigger_pattern": r"#!?\[\s*allow\(\s*warnings\s*\)\s*\]",
            "forbidden_claim_or_action": (
                "Disabling all rustc diagnostics across an item or crate with #[allow(warnings)] or #![allow(warnings)]."
            ),
            "remediation": (
                "Fix the underlying compiler warnings or scope a specific lint attribute such as \x23[expect(clippy::too_many_arguments)] per clippy."
            ),
            "match_mode": "line",
            "target_globs": ["**/*.rs", "!tests/**", "!**/tests/**", "!**/benches/**"],
            "fixtures": {
                "positive": "\x23![allow(warnings)]\npub fn compute() -> usize { 42 }",
                "negative": "\x23[allow(clippy::too_many_arguments)]\npub fn compute() -> usize { 42 }",
            },
        },
        {
            "trap_id": "TRAP-RS-002",
            "domain": "rust-hygiene",
            "title": "Empty Rust Result Err Block",
            "trigger_pattern": r"\bif\s+let\s+Err\s*\([^)]*\)\s*=\s*[^{;\n]+\{\s*\}",
            "forbidden_claim_or_action": (
                "Silently discarding Result::Err variants with an empty if let Err(...) = ... {} block."
            ),
            "remediation": (
                "Propagate failures with ?, emit structured error telemetry, or handle recovery explicitly per clippy."
            ),
            "match_mode": "multiline",
            "target_globs": ["**/*.rs", "!tests/**", "!**/tests/**", "!**/benches/**"],
            "fixtures": {
                "positive": "if let " + "Err(_err) = stream.flush() {}",
                "negative": "if let Err(err) = stream.flush() { eprintln!(\"flush failed: {err}\"); }",
            },
        },
    ),
    "go": (
        {
            "trap_id": "TRAP-GO-001",
            "domain": "go-hygiene",
            "title": "Empty Go Error Check Branch",
            "trigger_pattern": r"\bif\s+(?:[^{;\n]+;\s*)?err\s*!=\s*nil\s*\{\s*\}",
            "forbidden_claim_or_action": (
                "Ignoring non-nil Go errors inside an empty if err != nil {} branch."
            ),
            "remediation": (
                "Return wrapped error context via fmt.Errorf with %w or handle recovery explicitly per errcheck."
            ),
            "match_mode": "multiline",
            "target_globs": ["**/*.go", "!**/*_test.go", "!tests/**", "!**/tests/**"],
            "fixtures": {
                "positive": "if " + "err != nil {\n}",
                "negative": "if err != nil {\n\treturn fmt.Errorf(\"write frame: %w\", err)\n}",
            },
        },
    ),
}


def _is_link_or_junction(path: Path) -> bool:
    """Return True if path is a symbolic link or Windows junction/reparse mount."""
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or (getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text atomically via sibling temporary file and os.replace."""
    if _is_link_or_junction(path):
        raise OSError(f"Refusing to write trap registry through symbolic link or junction: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        for attempt in range(6):
            try:
                os.replace(tmp_name, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.01 * (attempt + 1))
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def resolve_traps_registry_path(registry_path: Optional[Path | str] = None) -> Path:
    """Resolve explicit trap registry path or default .stigmerig/traps.json path."""
    if registry_path is not None:
        return Path(registry_path)
    env_root = os.environ.get("STIGMERIG_ROOT")
    if env_root:
        return Path(env_root).resolve() / "traps.json"
    return (Path.cwd() / ".stigmerig").resolve() / "traps.json"


def discover_repo_trap_registries(repo_root: Optional[Path], rig_dir: Path) -> List[Path]:
    """Discover tracked team policy registries plus local rig traps.json in merge order."""
    candidates: List[Path] = []
    if repo_root is not None:
        candidates.append(repo_root / "stigmerig.traps.json")
        candidates.append(repo_root / ".github" / "traps.json")
    candidates.append(rig_dir / "traps.json")

    found: List[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        if candidate.is_file():
            seen.add(resolved)
            found.append(resolved)
    return found


@contextmanager
def trap_file_lock(
    registry_path: Path,
    timeout: float = 5.0,
    stale_after: float = 30.0,
) -> Iterator[Path]:
    """Acquire an exclusive stale-recoverable cross-process lockfile (<registry_path>.lock)."""
    reg_path = Path(registry_path)
    lock_path = reg_path.with_name(f"{reg_path.name}.lock")
    reg_path.parent.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + max(0.0, float(timeout))
    acquired = False

    while not acquired:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                payload = json.dumps({"pid": os.getpid(), "acquired_at": time.time()}) + "\n"
                os.write(fd, payload.encode("utf-8"))
            except BaseException:
                try:
                    os.close(fd)
                finally:
                    lock_path.unlink(missing_ok=True)
                raise
            os.close(fd)
            acquired = True
        except (FileExistsError, PermissionError) as exc:
            try:
                st = lock_path.stat()
                lock_age = time.time() - st.st_mtime
            except OSError:
                lock_age = 0.0

            if stale_after >= 0.0 and lock_age > stale_after:
                try:
                    lock_path.unlink()
                    continue
                except OSError:
                    pass

            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise TimeoutError(
                    f"Timed out acquiring trap registry lock {lock_path} after {timeout:.2f}s"
                ) from exc
            time.sleep(min(0.02, max(0.002, remaining)))

    try:
        yield lock_path
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


@lru_cache(maxsize=512)
def compile_glob_regex(glob_pat: str) -> re.Pattern[str]:
    """Compile a POSIX glob pattern into a segment-aware regular expression."""
    pat = glob_pat.strip().replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]

    if pat == "**":
        return re.compile(r"\A.*\Z")

    parts: List[str] = [r"\A"]
    i = 0
    n = len(pat)
    while i < n:
        if pat.startswith("**/", i):
            parts.append(r"(?:.+/)?")
            i += 3
        elif pat.startswith("/**", i) and i + 3 == n:
            parts.append(r"(?:/.*)?")
            i += 3
        elif pat.startswith("/**", i) and i + 3 < n and pat[i + 3] == "/":
            parts.append(r"/(?:.+/)?")
            i += 4
        elif pat.startswith("**", i):
            parts.append(r".*")
            i += 2
        elif pat[i] == "*":
            parts.append(r"[^/]*")
            i += 1
        elif pat[i] == "?":
            parts.append(r"[^/]")
            i += 1
        elif pat[i] == "[":
            j = i + 1
            if j < n and pat[j] in ("!", "^"):
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j < n:
                inner = pat[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                inner_escaped = inner.replace("\\", "\\\\")
                parts.append(f"[{inner_escaped}]")
                i = j + 1
            else:
                parts.append(re.escape("["))
                i += 1
        else:
            parts.append(re.escape(pat[i]))
            i += 1

    parts.append(r"\Z")
    return re.compile("".join(parts))


def _normalize_rel_prefixes(prefixes: Sequence[str]) -> Tuple[str, ...]:
    normalized: List[str] = []
    for raw in prefixes:
        clean = raw.strip().replace("\\", "/")
        while clean.startswith("./"):
            clean = clean[2:]
        clean = clean.strip("/")
        if clean:
            normalized.append(clean)
    return tuple(normalized)


def _matches_rel_prefix(rel_posix_str: str, norm_prefixes: Tuple[str, ...], *, is_dir: bool) -> bool:
    if not norm_prefixes or not rel_posix_str or rel_posix_str == ".":
        return False
    check_str = rel_posix_str if is_dir else (rel_posix_str.rsplit("/", 1)[0] if "/" in rel_posix_str else "")
    if not check_str:
        return False
    for prefix in norm_prefixes:
        if check_str == prefix or check_str.startswith(prefix + "/"):
            return True
    return False


def _has_sentinel_in_dir(dir_path: Path, sentinel_files: Sequence[str]) -> bool:
    if not sentinel_files:
        return False
    for sentinel in sentinel_files:
        try:
            if (dir_path / sentinel).exists():
                return True
        except OSError:
            continue
    return False


def _glob_matches(
    rel_posix: PurePosixPath,
    name: str,
    glob_pat: str,
    *,
    is_unanchored_abs: bool = False,
) -> bool:
    norm_pat = glob_pat.strip().replace("\\", "/")
    while norm_pat.startswith("./"):
        norm_pat = norm_pat[2:]

    if norm_pat == "AGENTS.md":
        return name == "AGENTS.md"
    if norm_pat == "CONTEXT.md":
        return name == "CONTEXT.md" and len(rel_posix.parts) == 1 and not is_unanchored_abs
    if norm_pat == "*/CONTEXT.md":
        return name == "CONTEXT.md" and len(rel_posix.parts) == 2 and not is_unanchored_abs

    rel_str = rel_posix.as_posix()
    if compile_glob_regex(norm_pat).match(rel_str) is not None:
        return True

    if is_unanchored_abs and not norm_pat.startswith("/"):
        stripped = rel_str.lstrip("/")
        if norm_pat.startswith("**/"):
            if compile_glob_regex(norm_pat).match(stripped) is not None:
                return True
        else:
            if compile_glob_regex("**/" + norm_pat).match(stripped) is not None:
                return True

    return False


def is_target_applicable(
    file_path: Path,
    target_globs: Sequence[str],
    repo_root: Optional[Path] = None,
    extra_pruned_dirs: Sequence[str] = (),
    extra_pruned_rel_prefixes: Sequence[str] = (),
    sentinel_files: Sequence[str] = (),
    excluded_filenames: Sequence[str] = DEFAULT_EXCLUDED_FILENAMES,
) -> bool:
    """Check whether file_path is eligible for scanning and matches target_globs."""
    f_path = Path(file_path)
    name = f_path.name
    if name in set(excluded_filenames):
        return False

    pruned_lower = {d.lower() for d in DEFAULT_PRUNED_DIRS}
    if extra_pruned_dirs:
        pruned_lower.update(d.lower() for d in extra_pruned_dirs)
    norm_prefixes = _normalize_rel_prefixes(extra_pruned_rel_prefixes)

    effective_root: Optional[Path] = None
    if repo_root is not None:
        try:
            effective_root = Path(repo_root).resolve()
        except OSError:
            effective_root = Path(repo_root)

    is_unanchored_abs = False
    check_parts: Tuple[str, ...] = ()

    if f_path.is_absolute():
        try:
            resolved_file = f_path.resolve()
        except OSError:
            resolved_file = f_path

        anchor_root = effective_root
        if anchor_root is None:
            try:
                cwd_resolved = Path.cwd().resolve()
                resolved_file.relative_to(cwd_resolved)
                anchor_root = cwd_resolved
            except (OSError, ValueError):
                anchor_root = None

        if anchor_root is not None:
            try:
                rel_str = resolved_file.relative_to(anchor_root).as_posix()
                rel_posix = PurePosixPath(rel_str)
                check_parts = rel_posix.parts[:-1]
                cur = resolved_file.parent
                while True:
                    if _has_sentinel_in_dir(cur, sentinel_files):
                        return False
                    if cur == anchor_root or cur.parent == cur:
                        break
                    cur = cur.parent
            except ValueError:
                rel_posix = PurePosixPath(f_path.as_posix())
                is_unanchored_abs = True
        else:
            rel_posix = PurePosixPath(f_path.as_posix())
            is_unanchored_abs = True

        if is_unanchored_abs:
            stop_dir: Optional[Path] = None
            try:
                temp_root = Path(tempfile.gettempdir()).resolve()
                rel_temp = resolved_file.relative_to(temp_root)
                check_parts = PurePosixPath(rel_temp.as_posix()).parts[:-1]
                stop_dir = temp_root
            except (OSError, ValueError):
                check_parts = rel_posix.parts[:-1]

            cur = resolved_file.parent
            while True:
                if _has_sentinel_in_dir(cur, sentinel_files):
                    return False
                if (stop_dir is not None and cur == stop_dir) or cur.parent == cur:
                    break
                cur = cur.parent
    else:
        raw_posix = f_path.as_posix()
        while raw_posix.startswith("./"):
            raw_posix = raw_posix[2:]
        rel_posix = PurePosixPath(raw_posix)
        check_parts = rel_posix.parts[:-1]

        if sentinel_files:
            base_dir = effective_root if effective_root is not None else Path.cwd()
            accum = base_dir
            if _has_sentinel_in_dir(accum, sentinel_files):
                return False
            for part in check_parts:
                accum = accum / part
                if _has_sentinel_in_dir(accum, sentinel_files):
                    return False

    if any(part.lower() in pruned_lower for part in check_parts):
        return False

    if norm_prefixes and not is_unanchored_abs:
        if _matches_rel_prefix(rel_posix.as_posix(), norm_prefixes, is_dir=False):
            return False

    if not target_globs:
        return True

    neg_globs = [g[1:] for g in target_globs if g.startswith("!")]
    pos_globs = [g for g in target_globs if not g.startswith("!")]

    if any(_glob_matches(rel_posix, name, ng, is_unanchored_abs=is_unanchored_abs) for ng in neg_globs):
        return False

    if not pos_globs:
        return True

    return any(_glob_matches(rel_posix, name, pg, is_unanchored_abs=is_unanchored_abs) for pg in pos_globs)


def _is_walk_dir_pruned(
    dir_path: Path,
    pruned_lower: set[str],
    norm_prefixes: Tuple[str, ...],
    sentinel_files: Sequence[str],
    prefix_roots: Sequence[Path],
) -> bool:
    if dir_path.name.lower() in pruned_lower:
        return True
    if _has_sentinel_in_dir(dir_path, sentinel_files):
        return True
    if norm_prefixes:
        try:
            resolved_dir = dir_path.resolve()
        except OSError:
            resolved_dir = dir_path
        for root in prefix_roots:
            try:
                rel = resolved_dir.relative_to(root).as_posix()
            except ValueError:
                continue
            if _matches_rel_prefix(rel, norm_prefixes, is_dir=True):
                return True
    return False


def collect_scannable_files(
    target_path: Path,
    extensions: Optional[Iterable[str]] = POLYGLOT_EXTENSIONS,
    extra_pruned_dirs: Sequence[str] = (),
    extra_pruned_rel_prefixes: Sequence[str] = (),
    sentinel_files: Sequence[str] = (),
    repo_root: Optional[Path] = None,
    excluded_filenames: Sequence[str] = DEFAULT_EXCLUDED_FILENAMES,
    max_file_size: int = MAX_FILE_SIZE,
) -> List[Path]:
    """Discover polyglot source files under target_path with pruning and link safety."""
    target = Path(target_path)
    if not target.exists() or _is_link_or_junction(target):
        return []

    excluded_set = set(excluded_filenames)
    norm_exts: Optional[frozenset[str]] = None
    if extensions is not None:
        norm_exts = frozenset(
            ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions
        )

    pruned_lower = {d.lower() for d in DEFAULT_PRUNED_DIRS}
    if extra_pruned_dirs:
        pruned_lower.update(d.lower() for d in extra_pruned_dirs)
    norm_prefixes = _normalize_rel_prefixes(extra_pruned_rel_prefixes)

    prefix_roots: List[Path] = []
    if repo_root is not None:
        try:
            prefix_roots.append(Path(repo_root).resolve())
        except OSError:
            prefix_roots.append(Path(repo_root))

    if target.is_file():
        if target.name in excluded_set:
            return []
        if norm_exts is not None and target.suffix.lower() not in norm_exts:
            return []
        try:
            if target.stat().st_size > max_file_size:
                return []
        except OSError:
            return []
        if not is_target_applicable(
            target,
            (),
            repo_root=repo_root,
            extra_pruned_dirs=extra_pruned_dirs,
            extra_pruned_rel_prefixes=extra_pruned_rel_prefixes,
            sentinel_files=sentinel_files,
            excluded_filenames=excluded_filenames,
        ):
            return []
        return [target]

    try:
        resolved_target = target.resolve()
        if resolved_target not in prefix_roots:
            prefix_roots.append(resolved_target)
    except OSError:
        pass

    if _is_walk_dir_pruned(target, pruned_lower, norm_prefixes, sentinel_files, prefix_roots):
        return []

    if repo_root is not None and sentinel_files:
        try:
            resolved_root = Path(repo_root).resolve()
            cur = target.resolve().parent
            while True:
                if _has_sentinel_in_dir(cur, sentinel_files):
                    return []
                if cur == resolved_root or cur.parent == cur:
                    break
                cur = cur.parent
        except OSError:
            pass

    scannable: List[Path] = []
    for root_str, dirs, files in os.walk(target, topdown=True, followlinks=False):
        cur_dir = Path(root_str)
        kept_dirs: List[str] = []
        for d in sorted(dirs):
            sub_dir = cur_dir / d
            if _is_link_or_junction(sub_dir):
                continue
            if _is_walk_dir_pruned(sub_dir, pruned_lower, norm_prefixes, sentinel_files, prefix_roots):
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for fname in sorted(files):
            if fname in excluded_set:
                continue
            f_path = cur_dir / fname
            if _is_link_or_junction(f_path):
                continue
            if norm_exts is not None and f_path.suffix.lower() not in norm_exts:
                continue
            try:
                if f_path.stat().st_size > max_file_size:
                    continue
            except OSError:
                continue
            scannable.append(f_path)

    return scannable


def is_prescriptive_remediation(text: str) -> bool:
    """Return True when remediation offers actionable guidance (>= 20 chars, non-stub)."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) < 20:
        return False
    if stripped.lower() in _NON_PRESCRIPTIVE_PLACEHOLDERS:
        return False
    if _BARE_ATOM_PATTERN.fullmatch(stripped) is not None:
        return False
    return True


def validate_trap_payload(
    pattern: str,
    pos_fixture: str,
    neg_fixture: str,
    match_mode: str = "line",
) -> Tuple[bool, str]:
    """Validate trigger regex length, compilation, and positive/negative fixtures."""
    if match_mode not in ("line", "multiline"):
        return False, f"Invalid match_mode: '{match_mode}'. Must be 'line' or 'multiline'."

    if not pattern:
        return False, "Trigger pattern must not be empty."

    if len(pattern) > MAX_PATTERN_LENGTH:
        return (
            False,
            f"Trigger pattern length ({len(pattern)}) exceeds maximum of {MAX_PATTERN_LENGTH} characters.",
        )

    try:
        flags = re.MULTILINE if match_mode == "multiline" else 0
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return False, f"Trigger pattern failed to compile: {exc}"

    if not compiled.search(pos_fixture):
        return False, f"Positive fixture failed to match trigger pattern '{pattern}'."

    if compiled.search(neg_fixture):
        return False, f"Negative fixture falsely matched trigger pattern '{pattern}'."

    return True, "Valid"


def _extract_trap_id(trap: Mapping[str, Any]) -> str:
    return str(trap.get("trap_id") or trap.get("id") or "")


def _extract_forbidden(trap: Mapping[str, Any]) -> str:
    return str(trap.get("forbidden_claim_or_action") or trap.get("reason") or "")


def _extract_remediation(trap: Mapping[str, Any]) -> str:
    rem = str(trap.get("remediation") or "").strip()
    rem_atom = str(trap.get("remediation_atom") or "").strip()
    if is_prescriptive_remediation(rem):
        return rem
    if is_prescriptive_remediation(rem_atom):
        return rem_atom
    return rem or rem_atom


def normalize_trap_record(trap: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy exposing both Tier A and Tier B field aliases."""
    normalized: Dict[str, Any] = dict(trap)
    t_id = _extract_trap_id(normalized)
    if t_id:
        normalized.setdefault("trap_id", t_id)
        normalized.setdefault("id", t_id)

    forbidden = _extract_forbidden(normalized)
    if forbidden or "forbidden_claim_or_action" in normalized or "reason" in normalized:
        normalized.setdefault("forbidden_claim_or_action", forbidden)
        normalized.setdefault("reason", forbidden)

    rem = _extract_remediation(normalized)
    if rem or "remediation" in normalized or "remediation_atom" in normalized:
        normalized.setdefault("remediation", rem)
        normalized.setdefault("remediation_atom", rem)

    return normalized


def verify_trap_fixtures(
    registry_path: Path,
    include_candidates: bool = True,
    require_prescriptive_on_promoted: bool = True,
) -> Tuple[bool, List[str], int]:
    """Verify regex patterns, fixtures, and promoted remediation rules across a registry."""
    reg_path = Path(registry_path)
    if not reg_path.exists():
        return False, [f"Registry not found: {reg_path}"], 0

    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        return False, [f"Failed to read/parse {reg_path}: {exc}"], 0

    if not isinstance(data, dict) or not isinstance(data.get("traps"), list):
        return False, [f"Invalid trap registry structure in {reg_path}: missing 'traps' list."], 0

    raw_traps = [t for t in data["traps"] if isinstance(t, dict)]
    if not include_candidates:
        traps = [t for t in raw_traps if t.get("status") == "promoted"]
    else:
        traps = raw_traps

    errors: List[str] = []
    for trap in traps:
        t_id = _extract_trap_id(trap) or "UNKNOWN"
        pattern = str(trap.get("trigger_pattern") or "")
        fixtures = trap.get("fixtures") if isinstance(trap.get("fixtures"), dict) else {}
        pos = str(fixtures.get("positive") or "")
        neg = str(fixtures.get("negative") or "")

        if not pattern:
            errors.append(f"[{t_id}] Missing trigger_pattern.")
        elif not pos:
            errors.append(f"[{t_id}] Missing positive fixture.")
        elif not neg:
            errors.append(f"[{t_id}] Missing negative fixture.")
        else:
            match_mode = str(trap.get("match_mode") or "line")
            valid, msg = validate_trap_payload(pattern, pos, neg, match_mode=match_mode)
            if not valid:
                errors.append(f"[{t_id}] Fixture validation failed: {msg}")

        if require_prescriptive_on_promoted and trap.get("status") == "promoted":
            rem = _extract_remediation(trap)
            if not is_prescriptive_remediation(rem):
                errors.append(
                    f"[{t_id}] Promoted trap remediation is not prescriptive (>= 20 chars, non-stub): {rem!r}"
                )

    return len(errors) == 0, errors, len(traps)


def load_promoted_traps(
    registry_path: Path,
    include_candidates: bool = False,
) -> List[Dict[str, Any]]:
    """Load and normalize traps from registry_path filtered by status."""
    reg_path = Path(registry_path)
    if not reg_path.exists():
        return []
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"[ERROR] Failed to load registry {reg_path}: {exc}", file=sys.stderr)
        return []

    if not isinstance(data, dict) or not isinstance(data.get("traps"), list):
        return []

    raw_traps = [t for t in data["traps"] if isinstance(t, dict)]
    selected = raw_traps if include_candidates else [t for t in raw_traps if t.get("status") == "promoted"]

    return [normalize_trap_record(t) for t in selected]


def audit_content_for_traps(
    content: str,
    traps: Sequence[Mapping[str, Any]],
    file_path: str = "",
    repo_root: Optional[Path] = None,
    extra_pruned_dirs: Sequence[str] = (),
    extra_pruned_rel_prefixes: Sequence[str] = (),
    sentinel_files: Sequence[str] = (),
    excluded_filenames: Sequence[str] = DEFAULT_EXCLUDED_FILENAMES,
) -> List[Dict[str, Any]]:
    """Scan text content against active traps with segment-aware glob and multiline support."""
    violations: List[Dict[str, Any]] = []
    lines = content.splitlines()
    f_path = Path(file_path) if file_path else None

    for trap in traps:
        raw_globs = trap.get("target_globs")
        target_globs: Sequence[str] = raw_globs if isinstance(raw_globs, list) else ()
        if f_path is not None and not is_target_applicable(
            f_path,
            target_globs,
            repo_root=repo_root,
            extra_pruned_dirs=extra_pruned_dirs,
            extra_pruned_rel_prefixes=extra_pruned_rel_prefixes,
            sentinel_files=sentinel_files,
            excluded_filenames=excluded_filenames,
        ):
            continue

        pattern = str(trap.get("trigger_pattern") or "")
        if not pattern:
            continue

        match_mode = str(trap.get("match_mode") or "line")
        try:
            flags = re.MULTILINE if match_mode == "multiline" else 0
            compiled = re.compile(pattern, flags)
        except re.error:
            continue

        trap_id = _extract_trap_id(trap) or "TRAP-UNKNOWN"
        title = str(trap.get("title") or "")
        domain = str(trap.get("domain") or "")
        forbidden = _extract_forbidden(trap)
        remediation = _extract_remediation(trap)

        if match_mode == "multiline":
            for match in compiled.finditer(content):
                line_no = content.count("\n", 0, match.start()) + 1
                snippet = re.sub(r"\s+", " ", match.group(0)).strip()[:100]
                violations.append({
                    "trap_id": trap_id,
                    "title": title,
                    "domain": domain,
                    "forbidden": forbidden,
                    "remediation": remediation,
                    "file": file_path,
                    "line": line_no,
                    "snippet": snippet,
                })
        else:
            for line_no, line in enumerate(lines, 1):
                if compiled.search(line):
                    violations.append({
                        "trap_id": trap_id,
                        "title": title,
                        "domain": domain,
                        "forbidden": forbidden,
                        "remediation": remediation,
                        "file": file_path,
                        "line": line_no,
                        "snippet": line.strip()[:100],
                    })

    return violations


def audit_file(
    file_path: Path,
    traps: Sequence[Mapping[str, Any]],
    repo_root: Optional[Path] = None,
    extra_pruned_dirs: Sequence[str] = (),
    extra_pruned_rel_prefixes: Sequence[str] = (),
    sentinel_files: Sequence[str] = (),
    excluded_filenames: Sequence[str] = DEFAULT_EXCLUDED_FILENAMES,
    max_file_size: int = MAX_FILE_SIZE,
) -> List[Dict[str, Any]]:
    """Audit a single file on disk against traps."""
    f_path = Path(file_path)
    if f_path.name in set(excluded_filenames) or _is_link_or_junction(f_path):
        return []
    try:
        if f_path.stat().st_size > max_file_size:
            return []
        content = f_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    return audit_content_for_traps(
        content,
        traps,
        str(f_path),
        repo_root=repo_root,
        extra_pruned_dirs=extra_pruned_dirs,
        extra_pruned_rel_prefixes=extra_pruned_rel_prefixes,
        sentinel_files=sentinel_files,
        excluded_filenames=excluded_filenames,
    )


def scan_paths(
    paths: Sequence[Path | str],
    traps: Sequence[Mapping[str, Any]],
    extensions: Optional[Iterable[str]] = POLYGLOT_EXTENSIONS,
    extra_pruned_dirs: Sequence[str] = (),
    extra_pruned_rel_prefixes: Sequence[str] = (),
    sentinel_files: Sequence[str] = (),
    repo_root: Optional[Path] = None,
    excluded_filenames: Sequence[str] = DEFAULT_EXCLUDED_FILENAMES,
    max_file_size: int = MAX_FILE_SIZE,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Scan a sequence of files or directories and return (scanned_count, violations)."""
    excluded_set = set(excluded_filenames)
    scanned_count = 0
    all_violations: List[Dict[str, Any]] = []

    for raw_path in paths:
        p = Path(raw_path)
        if p.is_file():
            if p.name in excluded_set or _is_link_or_junction(p):
                continue
            try:
                if p.stat().st_size > max_file_size:
                    continue
            except OSError:
                continue
            scanned_count += 1
            all_violations.extend(
                audit_file(
                    p,
                    traps,
                    repo_root=repo_root,
                    extra_pruned_dirs=extra_pruned_dirs,
                    extra_pruned_rel_prefixes=extra_pruned_rel_prefixes,
                    sentinel_files=sentinel_files,
                    excluded_filenames=excluded_filenames,
                    max_file_size=max_file_size,
                )
            )
        elif p.is_dir():
            files = collect_scannable_files(
                p,
                extensions=extensions,
                extra_pruned_dirs=extra_pruned_dirs,
                extra_pruned_rel_prefixes=extra_pruned_rel_prefixes,
                sentinel_files=sentinel_files,
                repo_root=repo_root,
                excluded_filenames=excluded_filenames,
                max_file_size=max_file_size,
            )
            for f_path in files:
                scanned_count += 1
                all_violations.extend(
                    audit_file(
                        f_path,
                        traps,
                        repo_root=repo_root,
                        extra_pruned_dirs=extra_pruned_dirs,
                        extra_pruned_rel_prefixes=extra_pruned_rel_prefixes,
                        sentinel_files=sentinel_files,
                        excluded_filenames=excluded_filenames,
                        max_file_size=max_file_size,
                    )
                )

    return scanned_count, all_violations


def _read_registry_unlocked(target_path: Path) -> Dict[str, Any]:
    if not target_path.exists():
        return {"version": "1.0.0", "traps": []}
    raw = json.loads(target_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid trap registry root in {target_path}: expected JSON object.")
    traps = raw.get("traps")
    if traps is None:
        raw["traps"] = []
    elif not isinstance(traps, list):
        raise ValueError(f"Invalid 'traps' field in {target_path}: expected JSON array.")
    raw.setdefault("version", "1.0.0")
    return raw


def _is_tier_a_registry(target_path: Path, existing_traps: Sequence[Mapping[str, Any]]) -> bool:
    if target_path.name == "Trap_Invariants.json":
        return False
    if target_path.name == "traps.json":
        return True
    return any("id" in t and "trap_id" not in t for t in existing_traps if isinstance(t, Mapping))


def _next_trap_id(existing_traps: Sequence[Mapping[str, Any]]) -> str:
    max_num = 0
    for entry in existing_traps:
        if not isinstance(entry, Mapping):
            continue
        for candidate_id in (entry.get("trap_id"), entry.get("id")):
            if not candidate_id:
                continue
            m = _TRAP_NUM_PATTERN.match(str(candidate_id))
            if m:
                max_num = max(max_num, int(m.group(1)))
    return f"TRAP-{max_num + 1:03d}"


def _safe_positive_int(raw_val: Any, default: int = 1) -> int:
    if isinstance(raw_val, bool):
        return default
    if isinstance(raw_val, int) and raw_val >= 0:
        return raw_val
    if isinstance(raw_val, float) and math.isfinite(raw_val) and raw_val >= 0:
        return int(raw_val)
    return default


def _merge_source_traces(entry: Mapping[str, Any], new_trace: str) -> List[str]:
    raw_traces = entry.get("source_traces")
    traces: List[str] = []
    if isinstance(raw_traces, list):
        for item in raw_traces:
            s_item = str(item).strip()
            if s_item and s_item not in traces:
                traces.append(s_item)
    else:
        seed = str(entry.get("source_trace") or "").strip()
        if seed:
            traces.append(seed)

    clean_new = new_trace.strip()
    if clean_new and clean_new not in traces:
        traces.append(clean_new)
    return traces[-10:]


def _apply_remediation_in_place(entry: Dict[str, Any], remediation_val: str, *, tier_a: bool) -> None:
    has_rem = "remediation" in entry
    has_atom = "remediation_atom" in entry
    if has_rem and not has_atom:
        entry["remediation"] = remediation_val
    elif has_atom and not has_rem:
        entry["remediation_atom"] = remediation_val
    elif tier_a:
        entry["remediation"] = remediation_val
        if has_atom:
            entry["remediation_atom"] = remediation_val
    else:
        entry["remediation_atom"] = remediation_val
        if has_rem:
            entry["remediation"] = remediation_val


def register_trap(
    domain: str,
    title: str,
    pattern: str,
    forbidden: str,
    source_trace: str,
    pos_fixture: str,
    neg_fixture: str,
    remediation: str = "",
    status: str = "candidate",
    target_globs: Optional[List[str]] = None,
    match_mode: str = "line",
    trap_id: Optional[str] = None,
    registry_path: Optional[Path] = None,
    enforce_prescriptive_remediation: bool = False,
) -> Dict[str, Any]:
    """Validate and register or deduplicate a trap invariant with lockfile protection."""
    target_path = resolve_traps_registry_path(registry_path)

    if match_mode not in ("line", "multiline"):
        raise ValueError(f"Invalid match_mode: '{match_mode}'. Must be 'line' or 'multiline'.")
    if status not in ("candidate", "promoted", "dismissed"):
        raise ValueError(f"Invalid status: '{status}'. Must be 'candidate', 'promoted', or 'dismissed'.")

    valid, err_msg = validate_trap_payload(pattern, pos_fixture, neg_fixture, match_mode=match_mode)
    if not valid:
        raise ValueError(err_msg)

    if enforce_prescriptive_remediation and not is_prescriptive_remediation(remediation):
        raise ValueError(
            "Remediation must be prescriptive (>= 20 chars, non-placeholder, and not a bare ATOM-ID)."
        )

    today = datetime.date.today().isoformat()
    norm_pattern = pattern.strip().lower()
    norm_title = re.sub(r"\s+", " ", title.strip().lower())

    with trap_file_lock(target_path):
        data = _read_registry_unlocked(target_path)
        existing_traps: List[Dict[str, Any]] = [t for t in data["traps"] if isinstance(t, dict)]
        tier_a = _is_tier_a_registry(target_path, existing_traps)

        for entry in existing_traps:
            if str(entry.get("status") or "").strip().lower() == "dismissed":
                continue
            existing_pat = str(entry.get("trigger_pattern") or "").strip().lower()
            existing_title = re.sub(
                r"\s+",
                " ",
                str(entry.get("title") or entry.get("reason") or "").strip().lower(),
            )
            if existing_pat == norm_pattern or existing_title == norm_title:
                entry["occurrences"] = _safe_positive_int(entry.get("occurrences"), 1) + 1
                entry["last_seen_at"] = today
                entry["source_traces"] = _merge_source_traces(entry, source_trace)
                entry["source_trace"] = source_trace
                if target_globs is not None:
                    entry["target_globs"] = list(target_globs)
                if match_mode != "line":
                    entry["match_mode"] = match_mode
                if remediation:
                    _apply_remediation_in_place(entry, remediation, tier_a=tier_a)
                data["traps"] = existing_traps
                _atomic_write_text(target_path, json.dumps(data, indent=2) + "\n")
                return normalize_trap_record(entry)

        if trap_id:
            for entry in existing_traps:
                if _extract_trap_id(entry) == trap_id:
                    raise ValueError(
                        f"Trap ID collision: {trap_id} is already in use by '{entry.get('title')}'."
                    )
            assigned_id = trap_id
        else:
            assigned_id = _next_trap_id(existing_traps)

        rem_value = remediation or "N/A"
        if tier_a:
            new_entry: Dict[str, Any] = {
                "id": assigned_id,
                "trap_id": assigned_id,
                "domain": domain,
                "title": title,
                "reason": forbidden,
                "forbidden_claim_or_action": forbidden,
                "trigger_pattern": pattern,
                "remediation": rem_value,
                "remediation_atom": rem_value,
                "source_trace": source_trace,
                "source_traces": [source_trace] if source_trace else [],
                "occurrences": 1,
                "status": status,
                "fixtures": {
                    "positive": pos_fixture,
                    "negative": neg_fixture,
                },
            }
        else:
            new_entry = {
                "trap_id": assigned_id,
                "domain": domain,
                "title": title,
                "trigger_pattern": pattern,
                "forbidden_claim_or_action": forbidden,
                "remediation_atom": rem_value,
                "source_trace": source_trace,
                "source_traces": [source_trace] if source_trace else [],
                "occurrences": 1,
                "status": status,
                "fixtures": {
                    "positive": pos_fixture,
                    "negative": neg_fixture,
                },
            }

        if target_globs:
            new_entry["target_globs"] = list(target_globs)
        if match_mode != "line":
            new_entry["match_mode"] = match_mode
        if status == "promoted":
            new_entry["promoted_at"] = today

        existing_traps.append(new_entry)
        data["traps"] = existing_traps
        _atomic_write_text(target_path, json.dumps(data, indent=2) + "\n")
        return normalize_trap_record(new_entry)


def promote_trap(
    trap_id: str,
    registry_path: Optional[Path] = None,
    pattern: Optional[str] = None,
    pos_fixture: Optional[str] = None,
    neg_fixture: Optional[str] = None,
    remediation: Optional[str] = None,
    target_globs: Optional[List[str]] = None,
    match_mode: Optional[str] = None,
    domain: Optional[str] = None,
    title: Optional[str] = None,
    forbidden: Optional[str] = None,
    enforce_prescriptive: bool = True,
) -> bool:
    """Promote a candidate trap invariant in-place while preserving provenance traces."""
    target_path = resolve_traps_registry_path(registry_path)
    if not target_path.exists():
        raise FileNotFoundError(f"Registry not found: {target_path}")

    clean_id = trap_id.strip()
    today = datetime.date.today().isoformat()

    with trap_file_lock(target_path):
        data = _read_registry_unlocked(target_path)
        existing_traps: List[Dict[str, Any]] = [t for t in data["traps"] if isinstance(t, dict)]
        tier_a = _is_tier_a_registry(target_path, existing_traps)

        matched: Optional[Dict[str, Any]] = None
        for entry in existing_traps:
            if _extract_trap_id(entry) == clean_id:
                matched = entry
                break

        if matched is None:
            return False

        eff_mode = match_mode if match_mode is not None else str(matched.get("match_mode") or "line")
        if eff_mode not in ("line", "multiline"):
            raise ValueError(f"Invalid match_mode: '{eff_mode}'. Must be 'line' or 'multiline'.")

        eff_pattern = pattern if pattern is not None else str(matched.get("trigger_pattern") or "")
        raw_fixtures = matched.get("fixtures")
        fix_dict = raw_fixtures if isinstance(raw_fixtures, dict) else {}
        eff_pos = pos_fixture if pos_fixture is not None else str(fix_dict.get("positive") or "")
        eff_neg = neg_fixture if neg_fixture is not None else str(fix_dict.get("negative") or "")
        eff_rem = remediation if remediation is not None else _extract_remediation(matched)

        if not eff_pattern:
            raise ValueError(f"Cannot promote {clean_id}: missing trigger_pattern.")
        if not eff_pos or not eff_neg:
            raise ValueError(f"Cannot promote {clean_id}: positive and negative fixtures are required.")

        valid, err_msg = validate_trap_payload(eff_pattern, eff_pos, eff_neg, match_mode=eff_mode)
        if not valid:
            raise ValueError(f"Cannot promote {clean_id}: {err_msg}")

        if enforce_prescriptive and not is_prescriptive_remediation(eff_rem):
            raise ValueError(
                f"Cannot promote {clean_id}: remediation must be prescriptive (>= 20 chars, non-placeholder, not a bare ATOM-ID)."
            )

        matched["trigger_pattern"] = eff_pattern
        matched["fixtures"] = {
            "positive": eff_pos,
            "negative": eff_neg,
        }
        if eff_rem:
            _apply_remediation_in_place(matched, eff_rem, tier_a=tier_a or ("id" in matched and "trap_id" not in matched))

        if domain is not None:
            matched["domain"] = domain
        if title is not None:
            matched["title"] = title
        if forbidden is not None:
            if "forbidden_claim_or_action" in matched:
                matched["forbidden_claim_or_action"] = forbidden
            if "reason" in matched or "forbidden_claim_or_action" not in matched:
                matched["reason"] = forbidden
        if target_globs is not None:
            matched["target_globs"] = list(target_globs)
        if match_mode is not None:
            if eff_mode != "line" or "match_mode" in matched:
                matched["match_mode"] = eff_mode

        if "promotion_candidate" in matched:
            matched["promotion_candidate"] = False

        matched["status"] = "promoted"
        matched["promoted_at"] = today

        data["traps"] = existing_traps
        _atomic_write_text(target_path, json.dumps(data, indent=2) + "\n")
        return True


def dismiss_trap(
    trap_id: str,
    reason: str = "Operator dismissed",
    registry_path: Optional[Path] = None,
) -> bool:
    """Dismiss a candidate or active trap invariant with lockfile protection."""
    target_path = resolve_traps_registry_path(registry_path)
    if not target_path.exists():
        raise FileNotFoundError(f"Registry not found: {target_path}")

    clean_id = trap_id.strip()
    today = datetime.date.today().isoformat()

    with trap_file_lock(target_path):
        data = _read_registry_unlocked(target_path)
        existing_traps: List[Dict[str, Any]] = [t for t in data["traps"] if isinstance(t, dict)]

        found = False
        for entry in existing_traps:
            if _extract_trap_id(entry) == clean_id:
                entry["status"] = "dismissed"
                entry["dismiss_reason"] = reason
                entry["dismissed_at"] = today
                if "promotion_candidate" in entry:
                    entry["promotion_candidate"] = False
                found = True
                break

        if not found:
            return False

        data["traps"] = existing_traps
        _atomic_write_text(target_path, json.dumps(data, indent=2) + "\n")
        return True


def seed_starter_traps(
    registry_path: Optional[Path] = None,
    pack: str = "all",
    status: str = "promoted",
) -> List[Dict[str, Any]]:
    """Seed opt-in polyglot starter trap packs idempotently into registry_path."""
    target_path = resolve_traps_registry_path(registry_path)
    norm_pack = pack.strip().lower()
    valid_packs = (*STARTER_TRAP_PACKS.keys(), "all")
    if norm_pack not in valid_packs:
        raise ValueError(
            f"Unknown starter pack: '{pack}'. Expected one of: {', '.join(valid_packs)}."
        )
    if status not in ("candidate", "promoted"):
        raise ValueError(f"Invalid starter status: '{status}'. Expected 'candidate' or 'promoted'.")

    if norm_pack == "all":
        templates: List[Dict[str, Any]] = [
            tpl for pack_items in STARTER_TRAP_PACKS.values() for tpl in pack_items
        ]
    else:
        templates = list(STARTER_TRAP_PACKS[norm_pack])

    for tpl in templates:
        t_id = tpl["trap_id"]
        t_mode = str(tpl.get("match_mode") or "line")
        valid, msg = validate_trap_payload(
            tpl["trigger_pattern"],
            tpl["fixtures"]["positive"],
            tpl["fixtures"]["negative"],
            match_mode=t_mode,
        )
        if not valid:
            raise ValueError(f"Invalid starter trap {t_id}: {msg}")
        if not is_prescriptive_remediation(tpl["remediation"]):
            raise ValueError(f"Starter trap {t_id} lacks prescriptive remediation.")

    today = datetime.date.today().isoformat()
    seeded: List[Dict[str, Any]] = []

    with trap_file_lock(target_path):
        data = _read_registry_unlocked(target_path)
        existing_traps: List[Dict[str, Any]] = [t for t in data["traps"] if isinstance(t, dict)]
        tier_a = _is_tier_a_registry(target_path, existing_traps)

        for tpl in templates:
            tpl_id = tpl["trap_id"]
            tpl_pat = tpl["trigger_pattern"].strip().lower()
            tpl_title = re.sub(r"\s+", " ", tpl["title"].strip().lower())

            matched: Optional[Dict[str, Any]] = None
            for entry in existing_traps:
                e_id = _extract_trap_id(entry)
                e_pat = str(entry.get("trigger_pattern") or "").strip().lower()
                e_title = re.sub(
                    r"\s+",
                    " ",
                    str(entry.get("title") or entry.get("reason") or "").strip().lower(),
                )
                if e_id == tpl_id or (
                    str(entry.get("status") or "").strip().lower() != "dismissed"
                    and (e_pat == tpl_pat or e_title == tpl_title)
                ):
                    matched = entry
                    break

            if matched is not None:
                seeded.append(normalize_trap_record(matched))
                continue

            source_ref = f"stigmerig:starter-pack:{norm_pack if norm_pack != 'all' else tpl['domain']}"
            forbidden = tpl["forbidden_claim_or_action"]
            rem = tpl["remediation"]
            t_mode = str(tpl.get("match_mode") or "line")

            if tier_a:
                new_entry: Dict[str, Any] = {
                    "id": tpl_id,
                    "trap_id": tpl_id,
                    "domain": tpl["domain"],
                    "title": tpl["title"],
                    "reason": forbidden,
                    "forbidden_claim_or_action": forbidden,
                    "trigger_pattern": tpl["trigger_pattern"],
                    "remediation": rem,
                    "remediation_atom": rem,
                    "source_trace": source_ref,
                    "source_traces": [source_ref],
                    "occurrences": 1,
                    "status": status,
                    "target_globs": list(tpl["target_globs"]),
                    "fixtures": {
                        "positive": tpl["fixtures"]["positive"],
                        "negative": tpl["fixtures"]["negative"],
                    },
                }
            else:
                new_entry = {
                    "trap_id": tpl_id,
                    "domain": tpl["domain"],
                    "title": tpl["title"],
                    "trigger_pattern": tpl["trigger_pattern"],
                    "forbidden_claim_or_action": forbidden,
                    "remediation_atom": rem,
                    "source_trace": source_ref,
                    "source_traces": [source_ref],
                    "occurrences": 1,
                    "status": status,
                    "target_globs": list(tpl["target_globs"]),
                    "fixtures": {
                        "positive": tpl["fixtures"]["positive"],
                        "negative": tpl["fixtures"]["negative"],
                    },
                }

            if t_mode != "line":
                new_entry["match_mode"] = t_mode
            if status == "promoted":
                new_entry["promoted_at"] = today

            existing_traps.append(new_entry)
            seeded.append(normalize_trap_record(new_entry))

        data["traps"] = existing_traps
        _atomic_write_text(target_path, json.dumps(data, indent=2) + "\n")

    return seeded


def _bad_trap_path_part(part: str) -> bool:
    clean = part.rstrip(". ").lower()
    return clean in {".git", ".env"} or clean.startswith(".env.")


def _safe_ancestor_roots(rig_dir: Optional[Path] = None) -> set[Path]:
    roots: set[Path] = set()
    candidates: List[Path] = [Path(tempfile.gettempdir())]
    try:
        candidates.append(Path.cwd())
    except OSError:
        pass
    if rig_dir is not None:
        candidates.append(Path(rig_dir).parent)
    for base in candidates:
        roots.update((base, *base.parents))
        try:
            res = base.resolve()
            roots.update((res, *res.parents))
        except OSError:
            continue
    return roots


def _resolve_validated_registry_path(
    raw_registry: Optional[str | os.PathLike[str]],
    rig_dir: Optional[Path] = None,
) -> Path:
    """Validate and resolve a trap registry path against traversal, link, and secret guards."""
    eff_rig = Path(rig_dir) if rig_dir is not None else resolve_traps_registry_path(None).parent
    safe_ancs = _safe_ancestor_roots(eff_rig)

    if raw_registry is None:
        target = eff_rig / "traps.json"
        abs_target = Path(os.path.abspath(str(target)))
        if _is_link_or_junction(target) or _is_link_or_junction(abs_target):
            raise ValueError(f"Registry path '{target}' must not be a symlink or junction")
        if any(
            _is_link_or_junction(anc)
            for anc in (*target.parents, *abs_target.parents)
            if anc not in safe_ancs
        ):
            raise ValueError(f"Registry path '{target}' must not traverse a symlink or junction")
        if target.exists() and target.is_dir():
            raise IsADirectoryError(f"Registry path '{target}' is an existing directory")
        return abs_target

    raw_str = str(os.fspath(raw_registry))
    if not raw_str.strip() or "\x00" in raw_str or any(not c.isprintable() for c in raw_str):
        raise ValueError("--registry path must not be empty, whitespace, or contain control characters")

    target = Path(raw_str)
    abs_target = Path(os.path.abspath(raw_str))
    if _is_link_or_junction(target) or _is_link_or_junction(abs_target):
        raise ValueError(f"Registry path '{target}' must not be a symlink or junction")
    if any(
        _is_link_or_junction(anc)
        for anc in (*target.parents, *abs_target.parents)
        if anc not in safe_ancs
    ):
        raise ValueError(f"Registry path '{target}' must not traverse a symlink or junction")

    resolved = target.resolve()
    parent_resolved = target.parent.resolve()
    checked_paths = (
        target,
        abs_target,
        resolved,
        parent_resolved,
        *(anc.resolve() for anc in target.parents),
    )
    if any(_bad_trap_path_part(part) for cand in checked_paths for part in cand.parts):
        raise ValueError(f"Registry path '{target}' must not target or traverse '.git' or '.env' paths")
    if resolved.name.lower().startswith(("queue.db", "audit.db")):
        raise ValueError(f"Registry path '{target}' must not target StigmeRig SQLite databases")
    if target.exists() and target.is_dir():
        raise IsADirectoryError(f"Registry path '{target}' is an existing directory")
    if any(
        (anc.exists() or _is_link_or_junction(anc)) and not anc.is_dir()
        for anc in (*target.parents, *abs_target.parents, *resolved.parents)
    ):
        raise ValueError(f"Registry path '{target}' traverses an existing non-directory file")
    return resolved


def validate_traps_cli_args(args: argparse.Namespace, rig_dir: Optional[Path] = None) -> Path:
    """Validate `stigmerig traps` CLI arguments before touching disk or state."""
    reg_path = _resolve_validated_registry_path(getattr(args, "registry", None), rig_dir)

    for text_field in (
        "pattern",
        "positive_fixture",
        "negative_fixture",
        "remediation",
        "forbidden",
        "domain",
        "source_trace",
    ):
        val = getattr(args, text_field, None)
        if val is not None and not str(val).strip():
            raise ValueError(f"--{text_field.replace('_', '-')} must not be empty or whitespace")

    target_globs = getattr(args, "target_globs", None)
    if target_globs is not None:
        if not target_globs or any(not str(g).strip() for g in target_globs):
            raise ValueError("--target-globs must not be empty or contain whitespace-only entries")

    if getattr(args, "subcommand", None) == "scan":
        raw_paths = getattr(args, "paths", None) or []
        safe_ancs = _safe_ancestor_roots(rig_dir)
        for raw_p in raw_paths:
            s_path = str(os.fspath(raw_p))
            if not s_path.strip() or "\x00" in s_path or any(not c.isprintable() for c in s_path):
                raise ValueError("scan path must not be empty, whitespace, or contain control characters")
            cand = Path(s_path)
            abs_cand = Path(os.path.abspath(s_path))
            if _is_link_or_junction(cand) or _is_link_or_junction(abs_cand):
                raise ValueError(f"Scan path '{s_path}' must not be a symlink or junction")
            if any(
                _is_link_or_junction(anc)
                for anc in (*cand.parents, *abs_cand.parents)
                if anc not in safe_ancs
            ):
                raise ValueError(f"Scan path '{s_path}' must not traverse a symlink or junction")
            if any(_bad_trap_path_part(part) for part in (*cand.parts, *abs_cand.parts)):
                raise ValueError(f"Scan path '{s_path}' must not target '.git' or '.env' paths")

    return reg_path


def add_traps_parser(subparsers: Any) -> None:
    """Register `stigmerig traps` subcommands on the root CLI parser."""
    p_traps = subparsers.add_parser(
        "traps",
        help="Scan, seed, verify, and manage polyglot trap invariants",
    )
    traps_sub = p_traps.add_subparsers(dest="subcommand", required=True)

    p_scan = traps_sub.add_parser(
        "scan",
        help="Scan polyglot source files against active trap invariants",
    )
    p_scan.add_argument(
        "paths",
        nargs="*",
        default=None,
        help="Files or directories to scan (defaults to '.' if omitted)",
    )
    p_scan.add_argument(
        "--registry",
        default=None,
        help="Path to trap registry JSON (defaults to <rig_dir>/traps.json)",
    )
    p_scan.add_argument(
        "--include-candidates",
        action="store_true",
        help="Include candidate traps in addition to promoted traps",
    )
    p_scan.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON scan report",
    )

    p_verify = traps_sub.add_parser(
        "verify",
        help="Verify trap regexes, positive/negative fixtures, and promoted remediations",
    )
    p_verify.add_argument(
        "--registry",
        default=None,
        help="Path to trap registry JSON (defaults to <rig_dir>/traps.json)",
    )
    p_verify.add_argument(
        "--include-candidates",
        action="store_true",
        help="Verify candidate traps in addition to promoted traps",
    )
    p_verify.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON verification report",
    )

    p_seed = traps_sub.add_parser(
        "seed",
        help="Seed opt-in polyglot starter trap packs",
    )
    p_seed.add_argument(
        "--pack",
        required=True,
        choices=[*STARTER_TRAP_PACKS.keys(), "all"],
        help="Starter pack to seed (generic, python, typescript, rust, go, all)",
    )
    p_seed.add_argument(
        "--status",
        choices=["candidate", "promoted"],
        default="promoted",
        help="Lifecycle status for newly seeded traps (default: promoted)",
    )
    p_seed.add_argument(
        "--registry",
        default=None,
        help="Path to trap registry JSON (defaults to <rig_dir>/traps.json)",
    )
    p_seed.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON seed report",
    )

    p_reg = traps_sub.add_parser(
        "register",
        help="Register a codified trap invariant with positive and negative fixtures",
    )
    p_reg.add_argument("--title", required=True, help="Human-readable trap title")
    p_reg.add_argument("--pattern", required=True, help="Trigger regex pattern (max 200 chars)")
    p_reg.add_argument(
        "--positive-fixture",
        required=True,
        dest="positive_fixture",
        help="Fixture text that must match the trigger pattern",
    )
    p_reg.add_argument(
        "--negative-fixture",
        required=True,
        dest="negative_fixture",
        help="Fixture text that must not match the trigger pattern",
    )
    p_reg.add_argument(
        "--forbidden",
        "--reason",
        dest="forbidden",
        default=None,
        help="Forbidden claim, code construct, or blocker rationale",
    )
    p_reg.add_argument(
        "--remediation",
        default=None,
        help="Prescriptive root-cause remediation (>= 20 chars required when promoted)",
    )
    p_reg.add_argument("--domain", default="general", help="Domain identifier (default: general)")
    p_reg.add_argument(
        "--source-trace",
        default="cli:manual",
        dest="source_trace",
        help="Lineage or provenance trace (default: cli:manual)",
    )
    p_reg.add_argument(
        "--status",
        choices=["candidate", "promoted"],
        default="candidate",
        help="Trap lifecycle status (default: candidate)",
    )
    p_reg.add_argument(
        "--target-globs",
        nargs="*",
        default=None,
        dest="target_globs",
        help="Optional POSIX glob patterns scoping applicable files",
    )
    p_reg.add_argument(
        "--match-mode",
        choices=["line", "multiline"],
        default="line",
        dest="match_mode",
        help="Regex matching mode (default: line)",
    )
    p_reg.add_argument(
        "--id",
        "--trap-id",
        dest="id",
        default=None,
        help="Explicit trap ID (auto-assigned if omitted)",
    )
    p_reg.add_argument(
        "--registry",
        default=None,
        help="Path to trap registry JSON (defaults to <rig_dir>/traps.json)",
    )
    p_reg.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON registration output",
    )

    p_prom = traps_sub.add_parser(
        "promote",
        help="Promote a candidate trap invariant in-place while preserving provenance traces",
    )
    p_prom.add_argument("--id", "--trap-id", dest="id", required=True, help="Trap ID to promote")
    p_prom.add_argument(
        "--registry",
        default=None,
        help="Path to trap registry JSON (defaults to <rig_dir>/traps.json)",
    )
    p_prom.add_argument(
        "--pattern",
        default=None,
        help="Source-code regex pattern (required when promoting raw harvested blockers)",
    )
    p_prom.add_argument(
        "--positive-fixture",
        default=None,
        dest="positive_fixture",
        help="Positive fixture text that must trigger the trap",
    )
    p_prom.add_argument(
        "--negative-fixture",
        default=None,
        dest="negative_fixture",
        help="Negative fixture text that must pass cleanly",
    )
    p_prom.add_argument(
        "--remediation",
        default=None,
        help="Prescriptive root-cause remediation (>= 20 chars, non-stub)",
    )
    p_prom.add_argument(
        "--forbidden",
        "--reason",
        dest="forbidden",
        default=None,
        help="Updated forbidden claim or failure rationale",
    )
    p_prom.add_argument("--domain", default=None, help="Optional domain identifier")
    p_prom.add_argument("--title", default=None, help="Optional updated trap title")
    p_prom.add_argument(
        "--target-globs",
        nargs="*",
        default=None,
        dest="target_globs",
        help="Optional target POSIX glob patterns",
    )
    p_prom.add_argument(
        "--match-mode",
        choices=["line", "multiline"],
        default=None,
        dest="match_mode",
        help="Optional regex matching mode",
    )
    p_prom.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON promotion result",
    )

    p_dis = traps_sub.add_parser(
        "dismiss",
        help="Dismiss a candidate or active trap invariant",
    )
    p_dis.add_argument("--id", "--trap-id", dest="id", required=True, help="Trap ID to dismiss")
    p_dis.add_argument(
        "--reason",
        default="Operator dismissed",
        help="Dismissal rationale (default: 'Operator dismissed')",
    )
    p_dis.add_argument(
        "--registry",
        default=None,
        help="Path to trap registry JSON (defaults to <rig_dir>/traps.json)",
    )
    p_dis.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON dismissal result",
    )


def _entry_lacks_code_fixtures(reg_path: Path, trap_id: str) -> bool:
    """Return True if trap_id exists in reg_path without prior positive/negative fixtures."""
    if not reg_path.exists():
        return False
    with trap_file_lock(reg_path):
        data = _read_registry_unlocked(reg_path)
    clean_id = trap_id.strip()
    for entry in data.get("traps", []):
        if isinstance(entry, dict) and _extract_trap_id(entry) == clean_id:
            fixtures = entry.get("fixtures")
            if not isinstance(fixtures, dict):
                return True
            return not (str(fixtures.get("positive") or "") and str(fixtures.get("negative") or ""))
    return False


def run_traps_command(args: argparse.Namespace, rig_dir: Path) -> int:
    """Execute a `stigmerig traps` subcommand and return an exit status code."""
    reg_path = validate_traps_cli_args(args, rig_dir)
    sub = args.subcommand

    if sub == "scan":
        ws_root: Optional[Path] = None
        try:
            if rig_dir.parent.exists():
                ws_root = rig_dir.parent.resolve()
        except OSError:
            ws_root = None

        if args.registry is not None:
            if not reg_path.exists():
                raise FileNotFoundError(f"Registry not found: {reg_path}")
            discovered_regs = [reg_path]
        else:
            discovered_regs = discover_repo_trap_registries(ws_root, rig_dir)

        merged_by_id: Dict[str, Dict[str, Any]] = {}
        ordered_traps: List[Dict[str, Any]] = []
        for r_path in discovered_regs:
            _read_registry_unlocked(r_path)
            loaded = load_promoted_traps(r_path, include_candidates=bool(args.include_candidates))
            for item in loaded:
                tid = str(item.get("trap_id") or item.get("id") or "")
                if tid and tid in merged_by_id:
                    idx = ordered_traps.index(merged_by_id[tid])
                    ordered_traps[idx] = item
                    merged_by_id[tid] = item
                else:
                    if tid:
                        merged_by_id[tid] = item
                    ordered_traps.append(item)
        traps = ordered_traps

        raw_targets = args.paths if args.paths else ["."]

        total_scanned = 0
        all_violations: List[Dict[str, Any]] = []
        for raw_target in raw_targets:
            target_p = Path(raw_target)
            if not target_p.exists():
                raise FileNotFoundError(f"Scan path not found: {raw_target}")
            try:
                resolved_p = target_p.resolve()
            except OSError:
                resolved_p = target_p
            if ws_root is not None and resolved_p.is_relative_to(ws_root):
                eff_root: Optional[Path] = ws_root
            elif target_p.is_dir():
                eff_root = resolved_p
            else:
                eff_root = None
            scanned_n, viols = scan_paths([target_p], traps, repo_root=eff_root)
            total_scanned += scanned_n
            all_violations.extend(viols)

        if args.json:
            report: Dict[str, Any] = {
                "ok": len(all_violations) == 0,
                "registry": str(discovered_regs[0] if len(discovered_regs) == 1 else reg_path),
                "registries": [str(p) for p in discovered_regs],
                "active_traps": len(traps),
                "scanned_files": total_scanned,
                "violations": all_violations,
            }
            print(json.dumps(report, indent=2))
            return 1 if all_violations else 0

        if all_violations:
            for v in all_violations:
                print(f"[VIOLATION] {v['trap_id']} - {v['title']} ({v['file']}:{v['line']})")
                if v.get("forbidden"):
                    print(f"  Forbidden: {v['forbidden']}")
                print(f"  Snippet: {v['snippet']}")
                if v.get("remediation"):
                    print(f"  Remediation: {v['remediation']}")
            print(
                f"[ERROR] Trap scan found {len(all_violations)} violation(s) across {total_scanned} file(s)",
                file=sys.stderr,
            )
            return 1

        print(
            f"[PASS] Scanned {total_scanned} file(s) against {len(traps)} active trap(s): zero violations."
        )
        return 0

    if sub == "verify":
        ok, errors, count = verify_trap_fixtures(
            reg_path,
            include_candidates=bool(args.include_candidates),
            require_prescriptive_on_promoted=True,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": ok,
                        "success": ok,
                        "registry": str(reg_path),
                        "verified_traps": count,
                        "errors": errors,
                    },
                    indent=2,
                )
            )
            return 0 if ok else 1
        if not ok:
            for err in errors:
                print(f"[ERROR] {err}", file=sys.stderr)
            return 1
        print(f"[PASS] Verified {count} trap(s) in {reg_path}.")
        return 0

    if sub == "seed":
        seeded = seed_starter_traps(
            registry_path=reg_path,
            pack=args.pack,
            status=args.status,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "pack": args.pack,
                        "status": args.status,
                        "registry": str(reg_path),
                        "seeded_count": len(seeded),
                        "traps": seeded,
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"Seeded {len(seeded)} trap(s) from pack '{args.pack}' ({args.status}) into {reg_path}"
            )
        return 0

    if sub == "register":
        entry = register_trap(
            domain=args.domain,
            title=args.title,
            pattern=args.pattern,
            forbidden=args.forbidden if args.forbidden is not None else args.title,
            source_trace=args.source_trace,
            pos_fixture=args.positive_fixture,
            neg_fixture=args.negative_fixture,
            remediation=args.remediation or "",
            status=args.status,
            target_globs=args.target_globs,
            match_mode=args.match_mode,
            trap_id=args.id,
            registry_path=reg_path,
            enforce_prescriptive_remediation=(args.status == "promoted"),
        )
        print(json.dumps(entry, indent=2))
        return 0

    if sub == "promote":
        if args.pattern is None and _entry_lacks_code_fixtures(reg_path, args.id):
            raise ValueError(
                f"Cannot promote harvested blocker {args.id}: explicit --pattern, --positive-fixture, --negative-fixture, and --remediation are required."
            )
        ok = promote_trap(
            trap_id=args.id,
            registry_path=reg_path,
            pattern=args.pattern,
            pos_fixture=args.positive_fixture,
            neg_fixture=args.negative_fixture,
            remediation=args.remediation,
            target_globs=args.target_globs,
            match_mode=args.match_mode,
            domain=args.domain,
            title=args.title,
            forbidden=args.forbidden,
            enforce_prescriptive=True,
        )
        if not ok:
            print(f"[ERROR] Trap {args.id} not found in {reg_path}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps({"ok": True, "trap_id": args.id, "status": "promoted", "registry": str(reg_path)}, indent=2))
        else:
            print(f"Promoted trap {args.id} in {reg_path}")
        return 0

    if sub == "dismiss":
        ok = dismiss_trap(
            trap_id=args.id,
            reason=args.reason,
            registry_path=reg_path,
        )
        if not ok:
            print(f"[ERROR] Trap {args.id} not found in {reg_path}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps({"ok": True, "trap_id": args.id, "status": "dismissed", "registry": str(reg_path)}, indent=2))
        else:
            print(f"Dismissed trap {args.id} in {reg_path}")
        return 0

    raise ValueError(f"Unknown traps subcommand: {sub}")


__all__ = [
    "DEFAULT_EXCLUDED_FILENAMES",
    "DEFAULT_PRUNED_DIRS",
    "MAX_FILE_SIZE",
    "MAX_PATTERN_LENGTH",
    "POLYGLOT_EXTENSIONS",
    "STARTER_TRAP_PACKS",
    "add_traps_parser",
    "audit_content_for_traps",
    "audit_file",
    "collect_scannable_files",
    "compile_glob_regex",
    "dismiss_trap",
    "is_prescriptive_remediation",
    "is_target_applicable",
    "load_promoted_traps",
    "normalize_trap_record",
    "promote_trap",
    "register_trap",
    "resolve_traps_registry_path",
    "run_traps_command",
    "scan_paths",
    "seed_starter_traps",
    "trap_file_lock",
    "validate_trap_payload",
    "validate_traps_cli_args",
    "verify_trap_fixtures",
]

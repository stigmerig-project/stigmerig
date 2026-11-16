"""Git worktree manager and wave execution runner for StigmeRig."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from stigmerig.db import StigmeRigDB, _is_self_approval, utc_now_iso

STIGMERIG_SWARM_NAME = os.environ.get("STIGMERIG_SWARM_NAME", "StigmeRig Swarm")
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TERMINAL_STATUSES = ("done", "cancelled", "canceled", "archived")
GIT_IN_PROGRESS_MARKERS = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
_REPO_LOCKS_GUARD = threading.Lock()
_REPO_LOCKS: Dict[str, threading.RLock] = {}


def _is_reparse_link(path: Path) -> bool:
    try:
        return path.is_symlink() or (path.is_junction() if hasattr(path, "is_junction") else getattr(path.lstat(), "st_reparse_tag", 0) == 0xA0000003)
    except OSError:
        return False


def _git_dir(repo_path: Path) -> Path:
    dot_git = repo_path / ".git"
    raw = dot_git.read_text(encoding="utf-8").strip() if dot_git.is_file() else ""
    return (repo_path / raw[7:].strip()).resolve() if raw.startswith("gitdir:") else dot_git.resolve()


def _git_common_dir(repo_path: Path) -> Path:
    dot_git = repo_path / ".git"
    if dot_git.is_dir() or dot_git.is_file():
        gd = _git_dir(repo_path)
        commondir = gd / "commondir"
        return (gd / commondir.read_text(encoding="utf-8").strip()).resolve() if commondir.is_file() else gd
    if repo_path.is_dir():
        try:
            res = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(repo_path), capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                return (repo_path / res.stdout.strip()).resolve()
        except OSError:
            return dot_git.resolve()
    return dot_git.resolve()


def get_git_repo_root(cwd: Optional[Path] = None) -> Optional[Path]:
    """Find the root of the current git repository using rev-parse."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd or Path.cwd()),
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(res.stdout.strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _proc_detail(exc: subprocess.CalledProcessError) -> str:
    for raw in (exc.stderr, exc.output):
        if txt := (raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")).strip():
            return txt
    return ""


class _GitProcessError(subprocess.CalledProcessError):
    def __str__(self) -> str:
        base, detail = super().__str__(), _proc_detail(self)
        return f"{base}: {detail}" if detail and detail not in base else base


def _run_checked(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise _GitProcessError(exc.returncode, exc.cmd, exc.output, exc.stderr) from exc


def get_active_branch(repo_root: Path) -> str:
    """Return active branch name."""
    res = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _invalid_meta(agent: Any, *extra: Any) -> bool:
    return (
        not isinstance(agent, str)
        or not agent.strip()
        or any(not isinstance(v, str) or not v.isprintable() for v in (agent, *(x for x in extra if x is not None)))
    )


def _symbolic_head(repo_path: Path) -> str:
    res = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=str(repo_path), capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else ""


def _in_progress_git_op(git_dir: Path) -> Optional[str]:
    return next((m for m in GIT_IN_PROGRESS_MARKERS if (git_dir / m).exists() or _is_reparse_link(git_dir / m)), None)


def _git_locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def _wrapped(self: WorktreeManager, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return fn(self, *args, **kwargs)

    return _wrapped


class WorktreeManager:
    """Manages isolated git worktrees for tasks."""

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        worktrees_dir: Optional[Path] = None,
        swarm_name: Optional[str] = None,
    ):
        self.repo_root = repo_root or get_git_repo_root()
        if not self.repo_root:
            raise RuntimeError("Not inside a git repository")
        raw_wt = Path(os.path.abspath(worktrees_dir)) if worktrees_dir is not None else (self.repo_root.resolve() / ".worktrees")
        self.repo_root = self.repo_root.resolve()
        self.worktrees_dir = raw_wt if _is_reparse_link(raw_wt) else raw_wt.resolve()
        self.swarm_name = swarm_name or os.environ.get("STIGMERIG_SWARM_NAME", STIGMERIG_SWARM_NAME)
        common_dir = _git_common_dir(self.repo_root)
        with _REPO_LOCKS_GUARD:
            self._lock = _REPO_LOCKS.setdefault(os.path.normcase(str(common_dir)), threading.RLock())

    def _is_valid_worktree(self, wt_path: Path) -> bool:
        if not wt_path.is_dir() or _is_reparse_link(wt_path / ".git") or not (wt_path / ".git").is_file():
            return False
        res = subprocess.run(["git", "rev-parse", "--show-toplevel", "--git-common-dir"], cwd=str(wt_path), capture_output=True, text=True)
        lines = res.stdout.strip().splitlines() if res.returncode == 0 else []
        common = _git_common_dir(self.repo_root)
        return len(lines) == 2 and Path(lines[0]).resolve() == wt_path.resolve() and (wt_path / lines[1]).resolve() == common

    def _resolve_worktree_target(self, task_id: str) -> Tuple[str, Path]:
        raw = task_id.strip() if isinstance(task_id, str) else ""
        if not TASK_ID_PATTERN.fullmatch(raw):
            raise ValueError(f"Invalid task_id '{task_id}' for worktree operation")
        clean_id = raw.upper()
        base_resolved = self.worktrees_dir.resolve()
        wt_path = (base_resolved / clean_id).resolve()
        if (
            wt_path == base_resolved
            or not wt_path.is_relative_to(base_resolved)
            or _is_reparse_link(self.worktrees_dir)
            or _is_reparse_link(base_resolved / clean_id)
        ):
            raise ValueError(f"Worktree path '{wt_path}' outside containment '{base_resolved}'")
        return clean_id, wt_path

    @_git_locked
    def create_worktree(self, task_id: str) -> Path:
        """Create a dedicated worktree and branch for the task."""
        clean_id, worktree_path = self._resolve_worktree_target(task_id)
        branch_name = f"task/{clean_id}"

        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

        if worktree_path.exists():
            if not self._is_valid_worktree(worktree_path):
                raise RuntimeError(f"Pre-existing path '{worktree_path}' is not a valid git worktree")
            if _symbolic_head(worktree_path) != f"refs/heads/{branch_name}":
                raise RuntimeError(f"Pre-existing worktree '{worktree_path}' is not on refs/heads/{branch_name}")
            if wt_op := _in_progress_git_op(_git_dir(worktree_path)):
                raise RuntimeError(f"Pre-existing worktree '{worktree_path}' has in-progress git operation: {wt_op}")
            return worktree_path

        _run_checked(["git", "worktree", "prune"], cwd=str(self.repo_root))

        # Check if branch exists
        branch_check = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )

        cmd = ["git", "worktree", "add"]
        if branch_check.returncode == 0:
            cmd.extend([str(worktree_path), branch_name])
        else:
            cmd.extend(["-b", branch_name, str(worktree_path)])

        _run_checked(cmd, cwd=str(self.repo_root))
        return worktree_path

    @_git_locked
    def remove_worktree(self, task_id: str, delete_branch: bool = False) -> None:
        """Prune and remove worktree directory."""
        clean_id, worktree_path = self._resolve_worktree_target(task_id)
        branch_name = f"task/{clean_id}"

        common_wt_dir = (_git_common_dir(self.repo_root) / "worktrees").resolve()
        if worktree_path.exists() or (common_wt_dir / clean_id).exists():
            subprocess.run(["git", "worktree", "remove", "--force", "--force", str(worktree_path)], cwd=str(self.repo_root), capture_output=True, text=True)
            if worktree_path.exists():
                if not _is_reparse_link(worktree_path / ".git") and (worktree_path / ".git").is_file() and (wt_gd := _git_dir(worktree_path)).is_relative_to(common_wt_dir):
                    (wt_gd / "locked").unlink(missing_ok=True)
                def _on_rm_err(fn: Callable[..., Any], target: str, _exc: Any) -> None:
                    tp = Path(target)
                    if tp.exists() or _is_reparse_link(tp):
                        if tp != worktree_path:
                            os.chmod(tp.parent, 0o700)
                        if not _is_reparse_link(tp):
                            os.chmod(tp, 0o700)
                        shutil.rmtree(tp, onerror=_on_rm_err) if fn in (os.scandir, os.listdir) else fn(target)
                shutil.rmtree(worktree_path, onerror=_on_rm_err)

        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )

        if delete_branch:
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
            )

    @_git_locked
    def merge_worktree(self, task_id: str, target_branch: Optional[str] = None) -> Tuple[bool, str]:
        """Merge task branch back into the target branch."""
        clean_id, worktree_path = self._resolve_worktree_target(task_id)
        branch_name = f"task/{clean_id}"
        if repo_op := _in_progress_git_op(_git_dir(self.repo_root)):
            return False, f"Repository at {self.repo_root} has in-progress git operation: {repo_op}"
        raw_target = get_active_branch(self.repo_root) if target_branch is None else target_branch
        target = raw_target.strip() if isinstance(raw_target, str) else ""
        if not target or target != raw_target or target.upper() == "HEAD" or target.startswith("-") or target == branch_name:
            return False, f"Invalid or detached target branch: {raw_target!r}"
        for ref, label in ((target, f"Target branch {target}"), (branch_name, f"Task branch {branch_name}")):
            if subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"], cwd=str(self.repo_root), capture_output=True).returncode != 0:
                return False, f"{label} does not exist"

        # Check clean worktree first
        if worktree_path.exists():
            if not self._is_valid_worktree(worktree_path):
                return False, f"Worktree at {worktree_path} is not a valid git worktree"
            if _symbolic_head(worktree_path) != f"refs/heads/{branch_name}":
                return False, f"Worktree at {worktree_path} is not on refs/heads/{branch_name}"
            if wt_op := _in_progress_git_op(_git_dir(worktree_path)):
                return False, f"Worktree at {worktree_path} has in-progress git operation: {wt_op}"
            status_res = subprocess.run(
                [
                    "git",
                    "status",
                    "--porcelain",
                    "--",
                    ".",
                    ":(exclude).stigmerig",
                    ":(exclude).stigmerig/**",
                    ":(exclude).worktrees",
                    ":(exclude).worktrees/**",
                    ":(exclude)*.db-wal",
                    ":(exclude)*.db-shm",
                ],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
            )
            if status_res.returncode != 0:
                return False, f"Failed to check git status in worktree {worktree_path}: {status_res.stderr.strip()}"
            if status_res.stdout.strip():
                return False, f"Worktree at {worktree_path} has uncommitted changes"

        # Ensure we are on target branch
        active = get_active_branch(self.repo_root)
        if active != target:
            checkout = subprocess.run(
                ["git", "checkout", target],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
            )
            if checkout.returncode != 0:
                return False, f"Failed to checkout {target}: {checkout.stderr}"

        # Perform merge with swarm orchestrator committer identity
        swarm_name = str(self.swarm_name or "").translate({0: None, 10: None, 13: None}).strip() or "StigmeRig Swarm"
        swarm_slug = re.sub(r"[^a-zA-Z0-9]", "", swarm_name.lower()) or "stigmerig"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": f"{swarm_name} [orchestrator]",
            "GIT_AUTHOR_EMAIL": f"swarm+orchestrator@{swarm_slug}.local",
            "GIT_COMMITTER_NAME": f"{swarm_name} [orchestrator]",
            "GIT_COMMITTER_EMAIL": f"swarm+orchestrator@{swarm_slug}.local",
        }
        merge_res = subprocess.run(
            ["git", "merge", "--no-ff", "--no-gpg-sign", "-m", f"Merge task {clean_id}", f"refs/heads/{branch_name}"],
            cwd=str(self.repo_root),
            env=env,
            capture_output=True,
            text=True,
        )

        if merge_res.returncode != 0:
            detail, has_mh = (merge_res.stderr or merge_res.stdout).strip(), (_git_dir(self.repo_root) / "MERGE_HEAD").exists()
            if has_mh:
                subprocess.run(["git", "merge", "--abort"], cwd=str(self.repo_root), capture_output=True)
            return False, f"{'Merge conflict' if has_mh else 'Merge failed'}: {detail}"

        self.remove_worktree(clean_id, delete_branch=True)
        return True, f"Successfully merged {branch_name} into {target}"


def _validate_wave_specs(task_specs: Any, has_worktree_mgr: bool) -> List[Dict[str, Any]]:
    if isinstance(task_specs, (str, bytes, dict)) or not hasattr(task_specs, "__iter__"):
        raise ValueError("task_specs must be an iterable of spec dicts")
    specs, seen_ids, seen_wt = list(task_specs), set(), set()
    for idx, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ValueError(f"Task spec at index {idx} must be a dict")
        tid, agent = spec.get("task_id"), spec.get("agent")
        if not isinstance(tid, str) or not TASK_ID_PATTERN.fullmatch(tid):
            raise ValueError(f"Task spec at index {idx} has missing or invalid task_id: {tid!r}")
        if _invalid_meta(agent, spec.get("verifier"), spec.get("archetype")):
            raise ValueError(f"Task spec at index {idx} has invalid or control-character metadata")
        if not isinstance(spec.get("allow_self_approval", False), bool):
            raise ValueError(f"Task spec at index {idx} has non-boolean allow_self_approval")
        if not callable(spec.get("action")):
            raise ValueError(f"Task spec at index {idx} has non-callable action")
        wt_key = tid.upper()
        if tid in seen_ids or (has_worktree_mgr and wt_key in seen_wt):
            raise ValueError(f"Duplicate or colliding task_id {tid!r} in task_specs")
        seen_ids.add(tid)
        seen_wt.add(wt_key)
    return specs


class WaveRunner:
    """Orchestrates rolling wave execution of tasks with worktree isolation."""

    def __init__(
        self,
        db: StigmeRigDB,
        worktree_mgr: Optional[WorktreeManager] = None,
        swarm_name: Optional[str] = None,
    ):
        self.db = db
        self.worktree_mgr = worktree_mgr
        self.swarm_name = (
            swarm_name
            or (worktree_mgr.swarm_name if worktree_mgr else None)
            or os.environ.get("STIGMERIG_SWARM_NAME", STIGMERIG_SWARM_NAME)
        )
        db_lock = getattr(db, "_lock", None)
        self._db_lock = db_lock if db_lock is not None else threading.RLock()

    def _pre_merge_error(self, task_id: str, agent: str, check_lease: bool = True) -> Optional[str]:
        with self._db_lock:
            kids = [c["id"] for c in self.db.list_tasks(parent_id=task_id) if c.get("status") not in TERMINAL_STATUSES] if isinstance(task_id, str) and TASK_ID_PATTERN.fullmatch(task_id) else []
            if kids or not check_lease:
                return f"Cannot complete {task_id}: open child tasks remain ({', '.join(kids)})" if kids else None
            task, now = self.db.get_task(task_id), utc_now_iso()
        if not task or task.get("status") != "working" or task.get("claimed_by") != agent:
            return f"Cannot merge {task_id}: active lease not held by '{agent}'"
        return f"Cannot merge {task_id}: lease expired at {task.get('lease_until')}" if not task.get("lease_until") or task["lease_until"] < now else None

    def run_task_in_worktree(
        self,
        task_id: str,
        agent: str,
        action_fn: Callable[[Path], bool],
        verifier: Optional[str] = None,
        archetype: Optional[str] = None,
        allow_self_approval: bool = False,
        evidence_paths: Optional[Sequence[str | Path]] = None,
    ) -> Tuple[bool, str]:
        """Execute a single task inside an isolated worktree."""
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            return False, f"Invalid task_id: {task_id!r}"
        if not isinstance(allow_self_approval, bool):
            return False, "allow_self_approval must be a boolean"
        if _invalid_meta(agent, verifier, archetype):
            return False, "Invalid or control-character metadata in agent, verifier, or archetype"
        if not allow_self_approval:
            clean_verifier = str(verifier).strip() if verifier is not None else ""
            if not clean_verifier or _is_self_approval(agent, clean_verifier):
                return False, "Independent verifier required unless allow_self_approval is True"

        with self._db_lock:
            if (child_err := self._pre_merge_error(task_id, agent, check_lease=False)) is not None:
                return False, child_err
            claimed = self.db.claim_task(task_id, agent=agent)
            task_record = (self.db.get_task(task_id) or {}) if claimed else {}
        if not claimed:
            return False, f"Could not claim task {task_id} for {agent}"

        db_arch = task_record.get("archetype")
        use_arg_arch = bool((archetype or "").strip())
        arch = (archetype or "").strip() if use_arg_arch else (str(db_arch or "").strip() or "diff_synthesizer")
        task_title = str(task_record.get("title") or "").translate({0: None, 10: None, 13: None}).strip() or f"Task {task_id}"

        worktree_path: Optional[Path] = None
        touched_paths: List[str] = []
        try:
            if (not use_arg_arch and db_arch is not None and _invalid_meta("ok", db_arch)) or _invalid_meta("ok", arch):
                raise ValueError("Invalid or control-character archetype")
            if self.worktree_mgr:
                with self.worktree_mgr._lock:
                    clean_id, _ = self.worktree_mgr._resolve_worktree_target(task_id)
                    if repo_op := _in_progress_git_op(_git_dir(self.worktree_mgr.repo_root)):
                        raise RuntimeError(f"Repository at {self.worktree_mgr.repo_root} has in-progress git operation: {repo_op}")
                    target_branch = get_active_branch(self.worktree_mgr.repo_root)
                    if not target_branch or target_branch != target_branch.strip() or target_branch.upper() == "HEAD" or target_branch.startswith("-") or target_branch == f"task/{clean_id}" or _symbolic_head(self.worktree_mgr.repo_root) != f"refs/heads/{target_branch}":
                        raise RuntimeError(f"Invalid or detached target branch: {target_branch!r}")
                    worktree_path = self.worktree_mgr.create_worktree(task_id)
                exec_dir = worktree_path
            else:
                exec_dir = self.db.rig_dir.parent

            success = action_fn(exec_dir)
            if not success:
                with self._db_lock:
                    self.db.fail_task(task_id, agent=agent, reason="Action function returned False")
                return False, f"Task {task_id} execution failed"

            if self.worktree_mgr and worktree_path:
                expected_ref = f"refs/heads/task/{clean_id}"
                merge_outcome: Optional[Tuple[bool, str]] = None
                with self.worktree_mgr._lock:
                    if not self.worktree_mgr._is_valid_worktree(worktree_path):
                        raise RuntimeError(f"Worktree '{worktree_path}' is not a valid git worktree")
                    if (lease_err := self._pre_merge_error(task_id, agent)) is not None:
                        return False, lease_err
                    if _symbolic_head(worktree_path) != expected_ref:
                        raise RuntimeError(f"Worktree '{worktree_path}' is not on {expected_ref}")
                    if wt_op := _in_progress_git_op(_git_dir(worktree_path)):
                        raise RuntimeError(f"Worktree '{worktree_path}' has in-progress git operation: {wt_op}")
                    subprocess.run(
                        ["git", "add", "--all", "--", ":(exclude).stigmerig", ":(exclude).stigmerig/**", ":(exclude).worktrees", ":(exclude).worktrees/**", ":(exclude)*.db-wal", ":(exclude)*.db-shm"],
                        cwd=str(worktree_path),
                        capture_output=True,
                        check=True,
                    )
                    cached_proc = subprocess.run(
                        ["git", "diff", "--cached", "--name-only"],
                        cwd=str(worktree_path),
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    committed_proc = subprocess.run(
                        ["git", "diff", "--name-only", f"refs/heads/{target_branch}..{expected_ref}"],
                        cwd=str(self.worktree_mgr.repo_root),
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    touched_paths = sorted({
                        line.strip()
                        for out in (cached_proc.stdout, committed_proc.stdout)
                        for line in out.splitlines()
                        if line.strip()
                    })
                    with self._db_lock:
                        friction_triggered = self.db._matches_intentional_friction(task_record, touched_paths)
                    if friction_triggered:
                        friction_err: Optional[str] = None
                        if allow_self_approval:
                            friction_err = f"Self-approval forbidden by intentional friction gate on task '{task_id}'."
                        elif not evidence_paths:
                            friction_err = f"Intentional friction gate requires non-empty SHA-256 evidence on task '{task_id}'."
                        else:
                            try:
                                self.db._validate_and_hash_evidence(evidence_paths)
                            except Exception as ev_exc:
                                friction_err = str(ev_exc)
                        if friction_err is not None:
                            with self._db_lock:
                                self.db.fail_task(task_id, agent=agent, reason=friction_err)
                            return False, friction_err
                    diff_proc = subprocess.run(
                        ["git", "diff", "--cached", "--quiet"],
                        cwd=str(worktree_path),
                        capture_output=True,
                    )
                    if diff_proc.returncode not in (0, 1):
                        diff_proc.check_returncode()
                    if diff_proc.returncode == 0:
                        rev_proc = subprocess.run(
                            ["git", "rev-list", "--count", f"refs/heads/{target_branch}..{expected_ref}"],
                            cwd=str(self.worktree_mgr.repo_root), capture_output=True, text=True, check=True,
                        )
                        if int(rev_proc.stdout.strip()) > 0:
                            merge_outcome = self.worktree_mgr.merge_worktree(task_id, target_branch=target_branch)
                        else:
                            self.worktree_mgr.remove_worktree(task_id, delete_branch=True)
                    else:
                        swarm_name = str(self.swarm_name or "").translate({0: None, 10: None, 13: None}).strip() or "StigmeRig Swarm"
                        swarm_slug = re.sub(r"[^a-zA-Z0-9]", "", swarm_name.lower()) or "stigmerig"
                        clean_arch = arch.lower().replace(" ", "_")
                        env = {
                            **os.environ,
                            "GIT_AUTHOR_NAME": f"{swarm_name} [{arch}]",
                            "GIT_AUTHOR_EMAIL": f"swarm+{clean_arch}@{swarm_slug}.local",
                            "GIT_COMMITTER_NAME": f"{swarm_name} [orchestrator]",
                            "GIT_COMMITTER_EMAIL": f"swarm+orchestrator@{swarm_slug}.local",
                        }
                        commit_msg = (
                            f"feat({task_id}): {task_title}\n\n"
                            f"Archetype: {arch}\n"
                            f"Claimant: {agent}\n"
                            f"Verifier: {(verifier or '').strip() or 'independent'}\n"
                            f"Task-ID: {task_id}\n"
                        )
                        subprocess.run(
                            ["git", "commit", "--no-gpg-sign", "-m", commit_msg],
                            cwd=str(worktree_path),
                            env=env,
                            capture_output=True,
                            check=True,
                        )
                        merge_outcome = self.worktree_mgr.merge_worktree(task_id, target_branch=target_branch)
                if merge_outcome is not None and not merge_outcome[0]:
                    with self._db_lock:
                        self.db.fail_task(task_id, agent=agent, reason=f"Merge failed: {merge_outcome[1]}")
                    return False, f"Merge failed: {merge_outcome[1]}"

            with self._db_lock:
                return self.db.complete_task(
                    task_id=task_id,
                    agent=agent,
                    verifier=verifier,
                    allow_self_approval=allow_self_approval,
                    evidence_paths=evidence_paths,
                    touched_paths=touched_paths,
                )
        except Exception as exc:
            err_msg = f"{exc}: {d}" if isinstance(exc, subprocess.CalledProcessError) and (d := _proc_detail(exc)) and d not in str(exc) else str(exc)
            try:
                with self._db_lock:
                    self.db.fail_task(task_id, agent=agent, reason=err_msg)
            except Exception as db_exc:
                return False, f"Task error: {err_msg} (fail_task error: {db_exc})"
            return False, f"Task error: {err_msg}"

    def run_wave(
        self,
        task_specs: List[Dict[str, Any]],
        max_concurrency: int = 5,
    ) -> Dict[str, Tuple[bool, str]]:
        """Run a wave of tasks. Limit concurrency to 3 to 5 tasks per wave."""
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError(f"max_concurrency must be an integer >= 1, got {max_concurrency!r}")
        task_specs = _validate_wave_specs(task_specs, self.worktree_mgr is not None)
        results: Dict[str, Tuple[bool, str]] = {}
        batch_size = min(max_concurrency, 5)

        for idx in range(0, len(task_specs), batch_size):
            batch = task_specs[idx : idx + batch_size]

            def _exec(spec: Dict[str, Any]) -> Tuple[str, Tuple[bool, str]]:
                t_id = spec["task_id"]
                agent = spec["agent"]
                action_fn = spec["action"]
                verifier = spec.get("verifier")
                arch = spec.get("archetype")
                allow_self_approval = spec.get("allow_self_approval", False)

                ok, msg = self.run_task_in_worktree(
                    task_id=t_id,
                    agent=agent,
                    action_fn=action_fn,
                    verifier=verifier,
                    archetype=arch,
                    allow_self_approval=allow_self_approval,
                    evidence_paths=spec.get("evidence_paths"),
                )
                return t_id, (ok, msg)

            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                results.update(pool.map(_exec, batch))

        return results

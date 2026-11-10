"""Git worktree manager and wave execution runner for StigmeRig."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from stigmerig.db import StigmeRigDB

STIGMERIG_SWARM_NAME = os.environ.get("STIGMERIG_SWARM_NAME", "StigmeRig Swarm")


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
        self.worktrees_dir = (worktrees_dir or (self.repo_root / ".worktrees")).resolve()
        self.swarm_name = swarm_name or os.environ.get("STIGMERIG_SWARM_NAME", STIGMERIG_SWARM_NAME)

    def create_worktree(self, task_id: str) -> Path:
        """Create a dedicated worktree and branch for the task."""
        clean_id = task_id.upper().strip()
        branch_name = f"task/{clean_id}"
        worktree_path = self.worktrees_dir / clean_id

        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

        if worktree_path.exists():
            return worktree_path

        # Check if branch exists
        branch_check = subprocess.run(
            ["git", "rev-parse", "--verify", branch_name],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )

        cmd = ["git", "worktree", "add"]
        if branch_check.returncode == 0:
            cmd.extend([str(worktree_path), branch_name])
        else:
            cmd.extend(["-b", branch_name, str(worktree_path)])

        subprocess.run(cmd, cwd=str(self.repo_root), capture_output=True, text=True, check=True)
        return worktree_path

    def remove_worktree(self, task_id: str, delete_branch: bool = False) -> None:
        """Prune and remove worktree directory."""
        clean_id = task_id.upper().strip()
        branch_name = f"task/{clean_id}"
        worktree_path = self.worktrees_dir / clean_id

        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
            )
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)

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

    def merge_worktree(self, task_id: str, target_branch: Optional[str] = None) -> Tuple[bool, str]:
        """Merge task branch back into the target branch."""
        clean_id = task_id.upper().strip()
        branch_name = f"task/{clean_id}"
        target = target_branch or get_active_branch(self.repo_root)

        # Check clean worktree first
        worktree_path = self.worktrees_dir / clean_id
        if worktree_path.exists():
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
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
        swarm_name = self.swarm_name
        swarm_slug = re.sub(r"[^a-zA-Z0-9]", "", swarm_name.lower()) or "stigmerig"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": f"{swarm_name} [orchestrator]",
            "GIT_AUTHOR_EMAIL": f"swarm+orchestrator@{swarm_slug}.local",
            "GIT_COMMITTER_NAME": f"{swarm_name} [orchestrator]",
            "GIT_COMMITTER_EMAIL": f"swarm+orchestrator@{swarm_slug}.local",
        }
        merge_res = subprocess.run(
            ["git", "merge", "--no-ff", "-m", f"Merge task {clean_id}", branch_name],
            cwd=str(self.repo_root),
            env=env,
            capture_output=True,
            text=True,
        )

        if merge_res.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=str(self.repo_root), capture_output=True)
            return False, f"Merge conflict: {merge_res.stderr}"

        self.remove_worktree(clean_id, delete_branch=True)
        return True, f"Successfully merged {branch_name} into {target}"


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

    def run_task_in_worktree(
        self,
        task_id: str,
        agent: str,
        action_fn: Callable[[Path], bool],
        verifier: Optional[str] = None,
        archetype: Optional[str] = None,
        allow_self_approval: bool = False,
    ) -> Tuple[bool, str]:
        """Execute a single task inside an isolated worktree."""
        if not allow_self_approval:
            if not (verifier and str(verifier).strip()) or verifier == agent:
                return False, "Independent verifier required unless allow_self_approval is True"

        claimed = self.db.claim_task(task_id, agent=agent)
        if not claimed:
            return False, f"Could not claim task {task_id} for {agent}"

        task_record = self.db.get_task(task_id) or {}
        arch = archetype or task_record.get("archetype") or "diff_synthesizer"
        task_title = task_record.get("title", f"Task {task_id}")

        worktree_path: Optional[Path] = None
        if self.worktree_mgr:
            worktree_path = self.worktree_mgr.create_worktree(task_id)
            exec_dir = worktree_path
        else:
            exec_dir = self.db.rig_dir.parent

        try:
            success = action_fn(exec_dir)
            if not success:
                self.db.fail_task(task_id, agent=agent, reason="Action function returned False")
                return False, f"Task {task_id} execution failed"

            if self.worktree_mgr and worktree_path:
                # Commit any changes made inside worktree attributing to specific swarm archetype
                subprocess.run(["git", "add", "."], cwd=str(worktree_path), capture_output=True, check=True)
                diff_proc = subprocess.run(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=str(worktree_path),
                    capture_output=True,
                )
                if diff_proc.returncode == 0:
                    self.worktree_mgr.remove_worktree(task_id)
                else:
                    swarm_name = self.swarm_name
                    swarm_slug = re.sub(r"[^a-zA-Z0-9]", "", swarm_name.lower()) or "stigmerig"
                    clean_arch = arch.lower().replace(" ", "_")
                    author_name = f"{swarm_name} [{arch}]"
                    author_email = f"swarm+{clean_arch}@{swarm_slug}.local"

                    env = {
                        **os.environ,
                        "GIT_AUTHOR_NAME": author_name,
                        "GIT_AUTHOR_EMAIL": author_email,
                        "GIT_COMMITTER_NAME": f"{swarm_name} [orchestrator]",
                        "GIT_COMMITTER_EMAIL": f"swarm+orchestrator@{swarm_slug}.local",
                    }
                    commit_msg = (
                        f"feat({task_id}): {task_title}\n\n"
                        f"Archetype: {arch}\n"
                        f"Claimant: {agent}\n"
                        f"Verifier: {verifier or 'independent'}\n"
                        f"Task-ID: {task_id}\n"
                    )
                    subprocess.run(
                        ["git", "commit", "-m", commit_msg],
                        cwd=str(worktree_path),
                        env=env,
                        capture_output=True,
                        check=True,
                    )
                    merged, msg = self.worktree_mgr.merge_worktree(task_id)
                    if not merged:
                        self.db.fail_task(task_id, agent=agent, reason=f"Merge failed: {msg}")
                        return False, f"Merge failed: {msg}"

            ok, comp_msg = self.db.complete_task(
                task_id=task_id,
                agent=agent,
                verifier=verifier,
                allow_self_approval=allow_self_approval,
            )
            return ok, comp_msg
        except Exception as exc:
            self.db.fail_task(task_id, agent=agent, reason=str(exc))
            if self.worktree_mgr and worktree_path:
                self.worktree_mgr.remove_worktree(task_id)
            return False, f"Task error: {exc}"

    def run_wave(
        self,
        task_specs: List[Dict[str, Any]],
        max_concurrency: int = 5,
    ) -> Dict[str, Tuple[bool, str]]:
        """Run a wave of tasks. Limit concurrency to 3 to 5 tasks per wave."""
        results: Dict[str, Tuple[bool, str]] = {}
        batch_size = max(1, min(int(max_concurrency), 5))

        for idx in range(0, len(task_specs), batch_size):
            batch = task_specs[idx : idx + batch_size]
            for spec in batch:
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
                )
                results[t_id] = (ok, msg)

        return results

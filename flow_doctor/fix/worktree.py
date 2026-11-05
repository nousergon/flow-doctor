"""An owned, disposable git worktree for one auto-fix run (alpha-engine-config-I12898).

The fix is applied, tested, committed and (on failure or dry run) thrown away
in a detached worktree at the caller's resolved HEAD commit, never in the
caller's checkout. Each run gets its own temporary directory, so concurrent
runs never share a working tree, and removal touches only that directory and
its own ``.git/worktrees`` entry.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple


class OwnedWorktree:
    """A detached worktree this run created, and the only one it removes."""

    def __init__(self, repo_path: str, base: str, owned_dir: str, path: str) -> None:
        self.repo_path = repo_path
        self.base = base
        self.owned_dir = owned_dir
        self.path = path

    @classmethod
    def create(cls, repo_path: str) -> Tuple[Optional["OwnedWorktree"], str]:
        """Add a detached worktree at ``repo_path``'s HEAD commit.

        HEAD is resolved to a commit id first, so the base cannot move under
        the run. Returns (worktree, "") or (None, reason); nothing is left
        behind on failure.
        """
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        base = head.stdout.strip()
        if head.returncode != 0 or not base:
            return (None, "the checkout has no HEAD commit")
        owned_dir = tempfile.mkdtemp(prefix="flow-doctor-fix-")
        path = str(Path(owned_dir) / "tree")
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", path, base],
            cwd=repo_path, capture_output=True, text=True,
        )
        if added.returncode != 0:
            shutil.rmtree(owned_dir, ignore_errors=True)
            return (None, f"git worktree add failed: {added.stderr.strip()}")
        return (cls(repo_path, base, owned_dir, path), "")

    def remove(self) -> bool:
        """Remove this worktree and its temporary directory, nothing else.

        ``git worktree remove --force`` names this path only, so other
        worktrees and the caller's checkout are never touched. If it fails,
        the owned directory is deleted directly and Git's stale entry is left
        for its own ``worktree prune``. Returns True when the directory is gone.
        """
        removed = subprocess.run(
            ["git", "worktree", "remove", "--force", self.path],
            cwd=self.repo_path, capture_output=True, text=True,
        )
        if removed.returncode != 0:
            print(f"[flow-doctor] git worktree remove failed: {removed.stderr.strip()}",
                  file=sys.stderr)
        shutil.rmtree(self.owned_dir, ignore_errors=True)
        if Path(self.owned_dir).exists():
            print(f"[flow-doctor] Could not remove the fix worktree at {self.owned_dir}",
                  file=sys.stderr)
            return False
        return True

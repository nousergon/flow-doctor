"""PR creator: git operations and GitHub PR creation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from typing import List, Optional
from urllib.request import Request, urlopen


class PRCreator:
    """Creates branches, applies diffs, and opens PRs."""

    @staticmethod
    def create_branch(repo_path: str, flow_name: str) -> str:
        """Create a new branch for the fix.

        Returns:
            The branch name (e.g., flow-doctor/research-lambda/20260317-a1b2c3).
        """
        date_str = datetime.utcnow().strftime("%Y%m%d")
        # Short hash for uniqueness
        hash_input = f"{flow_name}-{datetime.utcnow().isoformat()}".encode()
        short_hash = hashlib.sha256(hash_input).hexdigest()[:6]
        branch = f"flow-doctor/{flow_name}/{date_str}-{short_hash}"

        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        return branch

    @staticmethod
    def apply_diff(repo_path: str, diff: str) -> bool:
        """Apply a unified diff via git apply.

        Returns:
            True if the diff applied cleanly.
        """
        result = subprocess.run(
            ["git", "apply", "--check", "-"],
            input=diff,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[flow-doctor] Diff check failed: {result.stderr}", file=sys.stderr)
            return False

        result = subprocess.run(
            ["git", "apply", "-"],
            input=diff,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[flow-doctor] Diff apply failed: {result.stderr}", file=sys.stderr)
            return False
        return True

    @staticmethod
    def reverse_diff(repo_path: str, diff: str) -> bool:
        """Undo exactly ``diff`` (``git apply -R``), leaving other edits alone."""
        result = subprocess.run(
            ["git", "apply", "-R", "-"],
            input=diff,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @staticmethod
    def commit_and_push(
        repo_path: str, branch: str, message: str, paths: Optional[List[str]] = None,
    ) -> bool:
        """Stage the change, commit, and push the branch.

        With ``paths`` (the approved patch inventory), only those paths are
        staged, and the staged set is read back and must be non-empty and
        inside ``paths`` before anything is committed — test residue or any
        other worktree change never rides the fix (alpha-engine-config-I12896).
        Without it, every change is staged (legacy behaviour).

        Returns:
            True on success.
        """
        try:
            if paths is None:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=repo_path, check=True, capture_output=True,
                )
            else:
                subprocess.run(
                    ["git", "add", "-A", "--", *(f":(literal){p}" for p in paths)],
                    cwd=repo_path, check=True, capture_output=True,
                )
                staged = subprocess.run(
                    ["git", "diff", "--cached", "--no-renames", "--name-only", "-z"],
                    cwd=repo_path, check=True, capture_output=True, text=True,
                )
                staged_paths = {p for p in staged.stdout.split("\0") if p}
                outside = sorted(staged_paths - set(paths))
                if not staged_paths or outside:
                    print(
                        "[flow-doctor] Staged change does not match the approved "
                        f"inventory (outside: {outside!r}, staged: {len(staged_paths)})",
                        file=sys.stderr,
                    )
                    return False
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=repo_path, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=repo_path, check=True, capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            print(f"[flow-doctor] Git push failed: {e.stderr}", file=sys.stderr)
            return False

    @staticmethod
    def create_pr(
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        token: str,
        labels: Optional[List[str]] = None,
        assignee: Optional[str] = None,
    ) -> Optional[str]:
        """Create a pull request via GitHub API.

        Returns:
            The PR URL, or None on failure.
        """
        payload: dict = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }

        url = f"https://api.github.com/repos/{repo}/pulls"
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=data,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=15) as resp:
                pr_data = json.loads(resp.read())
                pr_url = pr_data.get("html_url", "")
                pr_number = pr_data.get("number")

            # Add labels if specified
            if labels and pr_number:
                _add_labels(repo, pr_number, labels, token)

            # Add assignee if specified
            if assignee and pr_number:
                _add_assignee(repo, pr_number, assignee, token)

            return pr_url
        except Exception as e:
            print(f"[flow-doctor] PR creation failed: {e}", file=sys.stderr)
            return None


def _add_labels(repo: str, pr_number: int, labels: List[str], token: str) -> None:
    """Add labels to a PR/issue."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/labels"
    data = json.dumps(labels).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def _add_assignee(repo: str, pr_number: int, assignee: str, token: str) -> None:
    """Add an assignee to a PR/issue."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}"
    data = json.dumps({"assignees": [assignee]}).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    try:
        with urlopen(req, timeout=10):
            pass
    except Exception:
        pass

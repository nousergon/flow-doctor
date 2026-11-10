"""Git context loader: fetches recent commits and changed files."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Dict, List, Optional, Set, TypedDict, cast
from urllib.request import Request, urlopen


class _CommitMessage(TypedDict):
    message: str


class _Commit(TypedDict):
    sha: str
    commit: _CommitMessage


class _ChangedFile(TypedDict):
    filename: str


class _CommitDetail(TypedDict, total=False):
    files: List[_ChangedFile]


# Deploy-drift errors carry two SHAs and the LLM cannot infer ordering
# from the error string alone.  Extract them deterministically so we can
# inject commit-range context before the diagnosis call ever runs.
_DEPLOY_DRIFT_SHA_RE = re.compile(
    r"\b([0-9a-f]{7,40})\b", re.IGNORECASE
)


class GitContextLoader:
    """Loads recent git history for diagnosis context."""

    @staticmethod
    def load_local(repo_path: Optional[str] = None) -> Dict[str, str]:
        """Load git context from a local repository.

        Returns {"git_log": str, "changed_files": str} or empty dict on failure.
        """
        try:
            cwd = repo_path or "."

            # Recent commits (last 7 days)
            log_result = subprocess.run(
                ["git", "log", "--oneline", "-20", "--since=7 days ago"],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            git_log = log_result.stdout.strip() if log_result.returncode == 0 else ""

            # Changed files in recent commits
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~20", "--", "."],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            changed_files = diff_result.stdout.strip() if diff_result.returncode == 0 else ""

            if not git_log and not changed_files:
                return {}

            return {"git_log": git_log, "changed_files": changed_files}

        except Exception as e:
            print(f"[flow-doctor] Git context load failed: {e}", file=sys.stderr)
            return {}

    @staticmethod
    def load_sha_range(
        repo_path: str,
        old_sha: str,
        new_sha: str,
    ) -> Dict[str, str]:
        """Load commit-range context between two SHAs.

        Determines which SHA is newer via ``git merge-base --is-ancestor``,
        then returns the log between them.  The caller injects the result
        into the diagnosis prompt so the LLM can produce a remediation
        that points to the correct direction.

        Returns a dict with keys ``sha_range_log``, ``newer_sha``,
        ``older_sha``, or an empty dict on any failure.
        """
        try:
            cwd = repo_path or "."

            # --- Determine ordering ----------------------------------------
            newer: Optional[str] = None
            older: Optional[str] = None

            # Is old_sha an ancestor of new_sha?
            anc1 = subprocess.run(
                ["git", "merge-base", "--is-ancestor", old_sha, new_sha],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            if anc1.returncode == 0:
                newer, older = new_sha, old_sha

            # Try the reverse?
            if newer is None:
                anc2 = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", new_sha, old_sha],
                    capture_output=True, text=True, cwd=cwd, timeout=10,
                )
                if anc2.returncode == 0:
                    newer, older = old_sha, new_sha

            if newer is None or older is None:
                # Not in a direct ancestor relationship — don't guess
                return {}

            # --- Commit range log ------------------------------------------
            log_result = subprocess.run(
                ["git", "log", "--oneline", f"{older}..{newer}"],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            sha_range_log = (
                log_result.stdout.strip()
                if log_result.returncode == 0
                else ""
            )

            if not sha_range_log:
                return {}

            return {
                "sha_range_log": sha_range_log,
                "newer_sha": newer,
                "older_sha": older,
            }

        except Exception as e:
            print(
                f"[flow-doctor] SHA range context load failed: {e}",
                file=sys.stderr,
            )
            return {}

    @classmethod
    def detect_and_load_sha_range(
        cls,
        error_message: str,
        repo_path: str = ".",
    ) -> Optional[Dict[str, str]]:
        """If *error_message* contains ≥2 SHAs, load the commit-range context.

        Returns the dict from :meth:`load_sha_range`, or ``None`` when the
        error doesn't look like a deploy-drift message or the range can't be
        resolved.
        """
        shas = _DEPLOY_DRIFT_SHA_RE.findall(error_message)
        if len(shas) < 2:
            return None
        # Try the first two distinct SHAs
        unique: List[str] = []
        for s in shas:
            if s not in unique:
                unique.append(s)
        if len(unique) < 2:
            return None
        result = cls.load_sha_range(repo_path, unique[0], unique[1])
        return result if result else None

    @staticmethod
    def load_github(repo: str, token: str) -> Dict[str, str]:
        """Load git context from GitHub API.

        Args:
            repo: "owner/repo" format
            token: GitHub personal access token

        Returns {"git_log": str, "changed_files": str} or empty dict on failure.
        """
        try:
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            }

            # Fetch recent commits
            url = f"https://api.github.com/repos/{repo}/commits?per_page=20"
            req = Request(url, headers=headers)
            with urlopen(req, timeout=10) as resp:
                # JSON has no static schema; GitHub defines this response shape.
                commits = cast(List[_Commit], json.loads(resp.read().decode()))

            git_log_lines = []
            for commit in commits[:20]:
                sha = commit["sha"][:7]
                msg = commit["commit"]["message"].split("\n")[0]
                git_log_lines.append(f"{sha} {msg}")
            git_log = "\n".join(git_log_lines)

            # Get changed files from most recent commits (first 5)
            changed_files_set: Set[str] = set()
            for commit in commits[:5]:
                sha = commit["sha"]
                detail_url = f"https://api.github.com/repos/{repo}/commits/{sha}"
                detail_req = Request(detail_url, headers=headers)
                with urlopen(detail_req, timeout=10) as resp:
                    # As above, use the GitHub commit-detail response contract.
                    detail = cast(_CommitDetail, json.loads(resp.read().decode()))
                for f in detail.get("files", []):
                    changed_files_set.add(f["filename"])

            changed_files = "\n".join(sorted(changed_files_set))

            return {"git_log": git_log, "changed_files": changed_files}

        except Exception as e:
            print(f"[flow-doctor] GitHub context load failed: {e}", file=sys.stderr)
            return {}

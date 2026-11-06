"""Test validator: runs test command and reports results."""

from __future__ import annotations

import os
import subprocess
from typing import Optional, Tuple


class TestValidator:
    """Runs a test command and returns pass/fail with output."""

    def run(
        self,
        test_command: str,
        repo_path: str,
        timeout: int = 300,
        source_path: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Run the test command in the given repo path.

        ``cwd`` alone does not make Python import the code under test: an
        editable install or an inherited ``PYTHONPATH`` resolves the package
        to the caller's original checkout, so a patched worktree would be
        "validated" by testing the unpatched source. ``repo_path`` (and its
        ``src/`` when present) therefore go first on ``PYTHONPATH``, and any
        inherited entry inside ``source_path`` is remapped into ``repo_path``.

        Args:
            test_command: Shell command to run (e.g., "python -m pytest tests/ -x -q").
            repo_path: Working directory for the command.
            timeout: Max seconds to wait.
            source_path: The checkout ``repo_path`` was cut from, if any.

        Returns:
            (passed, output) — passed is True if exit code is 0.
        """
        try:
            result = subprocess.run(
                test_command,
                shell=True,
                cwd=repo_path,
                env=_isolated_env(repo_path, source_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout
            if result.stderr:
                output += "\n" + result.stderr
            return (result.returncode == 0, output.strip())
        except subprocess.TimeoutExpired:
            return (False, f"Test command timed out after {timeout}s")
        except Exception as e:
            return (False, f"Test command failed to execute: {e}")


def _isolated_env(repo_path: str, source_path: Optional[str]) -> dict:
    """``os.environ`` with ``PYTHONPATH`` pointed at ``repo_path`` first."""
    work = os.path.abspath(repo_path)
    src = os.path.abspath(source_path) if source_path else None
    entries = [work]
    if os.path.isdir(os.path.join(work, "src")):
        entries.append(os.path.join(work, "src"))
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if not entry:
            continue
        absolute = os.path.abspath(entry)
        if src and (absolute == src or absolute.startswith(src + os.sep)):
            absolute = os.path.join(work, os.path.relpath(absolute, src))
        if absolute not in entries:
            entries.append(absolute)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env

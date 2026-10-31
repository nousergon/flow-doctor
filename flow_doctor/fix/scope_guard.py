"""Scope guard: the read and write boundary for auto-fix.

Two boundaries live here (alpha-engine-config-I12896 / I12897):

- **Write:** :func:`patch_inventory` lists EVERY path a patch touches — both
  sides of a rename or copy, deletions, binary and quoted names — with Git as
  the authority, so :class:`ScopeGuard` sees what the patch actually does
  rather than only its ``+++ b/`` destinations. :func:`changed_paths` lets the
  caller check the resulting worktree against that inventory.
- **Read:** :func:`resolve_confined` resolves a diagnosis-named path against
  the repository root and refuses anything that is not a regular, tracked,
  in-scope, non-secret file inside it, before the file is opened.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Basenames never read into model context or written by an auto-fix,
# whatever the scope config says.
SECRET_PATTERNS: Tuple[str, ...] = (
    ".env", ".env.*", "*.env", ".netrc", ".pypirc", ".npmrc", ".git-credentials",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*credential*", "*secret*",
)


def path_problem(path: str) -> Optional[str]:
    """Return why ``path`` is not a safe repository-relative path, or None."""
    if not path or "\0" in path:
        return "empty or NUL path"
    if "\\" in path:
        return "backslash in path"
    if path.startswith("/") or os.path.isabs(path) or (len(path) > 1 and path[1] == ":"):
        return "absolute path"
    parts = path.split("/")
    if ".." in parts:
        return "parent-directory component"
    if any(p.lower() == ".git" for p in parts):
        return ".git path"
    return None


def normalize_path(path: str) -> str:
    """Collapse ``./`` and duplicate slashes; never resolves ``..``."""
    return posixpath.normpath(path) if path else path


def is_secret_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS)


class ScopeGuard:
    """Validates that every path is safe, within allow and not in deny."""

    def __init__(self, allow: List[str], deny: List[str]) -> None:
        self.allow = allow
        self.deny = deny

    def check(self, diff_files: List[str]) -> Tuple[bool, List[str]]:
        """Check if all diff files are in scope.

        Returns:
            (passed, violations) — passed is True if all files are in scope,
            violations lists any out-of-scope files.
        """
        violations: List[str] = []

        for raw in diff_files:
            problem = path_problem(raw)
            if problem:
                violations.append(f"{problem}: {raw!r}")
                continue
            path = normalize_path(raw)
            if not self._is_allowed(path):
                violations.append(f"not in allow list: {path}")
            elif self._is_denied(path):
                violations.append(f"in deny list: {path}")
            elif is_secret_path(path):
                violations.append(f"secret-shaped path: {path}")

        return (len(violations) == 0, violations)

    def _is_allowed(self, path: str) -> bool:
        if not self.allow:
            return True
        return any(self._matches(pattern, path) for pattern in self.allow)

    def _is_denied(self, path: str) -> bool:
        if not self.deny:
            return False
        return any(self._matches(pattern, path) for pattern in self.deny)

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        """Match a pattern against a path. Supports glob and prefix matching."""
        # Direct glob match
        if fnmatch.fnmatch(path, pattern):
            return True
        # Prefix match (e.g., "data/" matches "data/scanner.py")
        if pattern.endswith("/") and path.startswith(pattern):
            return True
        # Prefix match without trailing slash
        if not pattern.endswith("/") and "/" not in pattern and "*" not in pattern:
            if path.startswith(pattern + "/") or path == pattern:
                return True
        return False


# --- write boundary: what does a patch touch? ---------------------------------

_C_ESCAPES: Dict[str, int] = {
    "a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92,
}


def _unquote_git_path(token: str) -> str:
    """Decode Git's C-style quoted path (``"b/w\\"q.py"``); pass others through."""
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        return token
    body, out, i = token[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            octal = body[i + 1:i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            out.append(_C_ESCAPES.get(nxt, ord(nxt)))
            i += 2
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="surrogateescape")


def _strip_side(path: str) -> str:
    return path[2:] if path[:2] in ("a/", "b/") else path


def header_paths(diff: str) -> List[str]:
    """Every path named by a patch's headers, both sides, textually.

    A second, independent reading beside Git's: ``---``/``+++`` lines (so a
    deletion's ``--- a/x`` counts), ``rename from/to``, ``copy from/to``.
    """
    paths: List[str] = []
    for line in diff.split("\n"):
        value: Optional[str] = None
        if line.startswith(("--- ", "+++ ")):
            value = line[4:].split("\t", 1)[0].rstrip("\r")
            value = _strip_side(_unquote_git_path(value))
        else:
            for prefix in ("rename from ", "rename to ", "copy from ", "copy to "):
                if line.startswith(prefix):
                    value = _unquote_git_path(line[len(prefix):].rstrip("\r"))
                    break
        if value and value != "/dev/null" and value not in paths:
            paths.append(value)
    return paths


def _git(repo_path: str, args: List[str], stdin: Optional[str] = None,
         env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo_path, input=stdin, capture_output=True,
        text=True, env=env,
    )


def _split_z(out: str) -> List[str]:
    return [p for p in out.split("\0") if p]


def git_patch_paths(repo_path: str, diff: str) -> Optional[Set[str]]:
    """Paths the patch changes per Git, applied to a throwaway index.

    The patch is applied with ``--cached`` to a temporary index built from
    HEAD, so the worktree is untouched, and the index is diffed against HEAD
    with renames off: a rename reports its deleted source AND its new
    destination. Returns None when Git cannot apply or list it.
    """
    with tempfile.TemporaryDirectory(prefix="fd-inventory-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        if _git(repo_path, ["read-tree", "HEAD"], env=env).returncode != 0:
            return None
        if _git(repo_path, ["apply", "--cached", "-"], stdin=diff, env=env).returncode != 0:
            return None
        listed = _git(
            repo_path,
            ["diff-index", "--cached", "--no-renames", "--name-only", "-z", "HEAD"],
            env=env,
        )
        if listed.returncode != 0:
            return None
        return set(_split_z(listed.stdout))


def patch_inventory(repo_path: str, diff: str) -> Tuple[Optional[List[str]], str]:
    """Every path ``diff`` touches, or (None, reason) when it cannot be known.

    The union of Git's own reading and the header reading: a path either one
    names is checked. An empty or unparseable inventory is refused rather
    than passed as "nothing to check".
    """
    git_paths = git_patch_paths(repo_path, diff)
    if git_paths is None:
        return (None, "git could not apply the patch to a scratch index")
    paths = git_paths | set(header_paths(diff))
    if not paths:
        return (None, "patch touches no paths")
    return (sorted(paths), "")


def changed_paths(repo_path: str) -> Optional[Set[str]]:
    """Worktree paths that differ from HEAD (tracked or untracked), per Git."""
    out = _git(
        repo_path,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
    )
    if out.returncode != 0:
        return None
    return {entry[3:] for entry in _split_z(out.stdout) if len(entry) > 3}


# --- read boundary: may this path enter model context? ------------------------

def tracked_files(repo_path: str) -> Optional[Set[str]]:
    """Paths Git tracks in ``repo_path``; None if it is not a Git checkout."""
    out = _git(repo_path, ["ls-files", "-z"])
    if out.returncode != 0:
        return None
    return set(_split_z(out.stdout))


def resolve_confined(
    repo_path: str,
    path: str,
    tracked: Optional[Set[str]],
    guard: ScopeGuard,
) -> Tuple[Optional[Path], str]:
    """Resolve ``path`` inside ``repo_path``, or return (None, reason).

    Refuses, before any open: absolute and parent-escaping paths, ``.git``,
    secret-shaped names, anything ``guard`` refuses, untracked files, any
    symlink on the way (in or out of the repo), and non-regular files. The
    reason names the rule, never file contents.
    """
    problem = path_problem(path)
    if problem:
        return (None, problem)
    rel = normalize_path(path)
    passed, violations = guard.check([rel])
    if not passed:
        return (None, violations[0])
    if tracked is None:
        return (None, "repository file list unavailable")
    if rel not in tracked:
        return (None, "not a tracked file")
    root = Path(repo_path).resolve()
    candidate = root / rel
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return (None, "does not exist")
    if resolved != candidate:
        return (None, "symlink in path")
    if not resolved.is_file():
        return (None, "not a regular file")
    return (resolved, "")

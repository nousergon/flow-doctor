"""Auto-fix read and write boundaries (alpha-engine-config-I12896 / I12897).

Write: a patch's inventory is every path it touches — deletions and both sides
of a rename or copy, binary and quoted names — and ScopeGuard sees all of it.
Read: diagnosis-named paths are confined to tracked, in-scope repository files
before anything is opened.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest

from flow_doctor.fix.cli import FixOutcome, _find_test_files, _read_file_contents, generate_fix
from flow_doctor.fix.generator import FixGenerator
from flow_doctor.fix.pr_creator import PRCreator
from flow_doctor.fix.scope_guard import (
    ScopeGuard,
    changed_paths,
    patch_inventory,
    resolve_confined,
    tracked_files,
)

_GIT_ID = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


def _repo(tmp_path: Path, files: Dict[str, bytes]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _staged_patch(repo: Path, *ops: tuple[str, ...], flags: tuple[str, ...] = ()) -> str:
    """Make ``ops`` in the index, capture ``git diff --cached``, reset."""
    for op in ops:
        if op[0] == "write":
            p = repo / op[1]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(op[2].encode("latin-1"))
            _git(repo, "add", "--", op[1])
        else:
            if op[0] == "mv":
                (repo / op[-1]).parent.mkdir(parents=True, exist_ok=True)
            _git(repo, *op)
    diff = _git(repo, "diff", "--cached", *flags)
    _git(repo, "reset", "-q", "--hard")
    _git(repo, "clean", "-fdq")
    return diff


GUARD = ScopeGuard(allow=["src/"], deny=["protected/"])


# --- write boundary ----------------------------------------------------------

def test_deletion_of_denied_file_is_in_inventory_and_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"protected/guard.py": b"a\n", "src/a.py": b"x\n"})
    diff = _staged_patch(repo, ("rm", "-q", "protected/guard.py"))
    assert "+++ /dev/null" in diff

    inventory, why = patch_inventory(str(repo), diff)
    assert inventory == ["protected/guard.py"], why
    passed, violations = GUARD.check(inventory)
    assert not passed and "protected/guard.py" in violations[0]
    # The textual reader no longer drops deletions either.
    assert FixGenerator.extract_files_from_diff(diff) == ["protected/guard.py"]


def test_deletion_outside_allow_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"other/x.py": b"a\n"})
    diff = _staged_patch(repo, ("rm", "-q", "other/x.py"))
    inventory, _ = patch_inventory(str(repo), diff)
    assert inventory == ["other/x.py"]
    passed, violations = GUARD.check(inventory)
    assert not passed and "not in allow list" in violations[0]


def test_rename_out_of_denied_source_checks_the_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"protected/k.py": b"secret logic\n"})
    diff = _staged_patch(repo, ("mv", "protected/k.py", "src/k.py"), flags=("-M",))
    assert "rename from protected/k.py" in diff and "+++" not in diff

    inventory, _ = patch_inventory(str(repo), diff)
    assert inventory == ["protected/k.py", "src/k.py"]
    assert not GUARD.check(inventory)[0]


def test_rename_into_denied_destination_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/k.py": b"x\n"})
    diff = _staged_patch(repo, ("mv", "src/k.py", "protected/k.py"), flags=("-M",))
    inventory, _ = patch_inventory(str(repo), diff)
    assert inventory is not None and "protected/k.py" in inventory
    assert not GUARD.check(inventory)[0]


def test_copy_from_denied_source_is_refused() -> None:
    diff = (
        "diff --git a/protected/k.py b/src/k.py\n"
        "similarity index 100%\n"
        "copy from protected/k.py\n"
        "copy to src/k.py\n"
    )
    files = FixGenerator.extract_files_from_diff(diff)
    assert files == ["protected/k.py", "src/k.py"]
    assert not GUARD.check(files)[0]


def test_quoted_unusual_names_are_decoded_and_enforced(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/we ird.py": b"x\n"})
    diff = _staged_patch(
        repo, ("mv", "src/we ird.py", 'protected/w"q\tz.py'), flags=("-M",),
    )
    assert '"protected/w\\"q\\tz.py"' in diff
    inventory, _ = patch_inventory(str(repo), diff)
    assert inventory == ['protected/w"q\tz.py', "src/we ird.py"]
    assert not GUARD.check(inventory)[0]


def test_binary_patch_paths_are_enforced(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"protected/blob.bin": bytes(range(256))})
    diff = _staged_patch(
        repo, ("write", "protected/blob.bin", bytes(range(255, -1, -1)).decode("latin-1")),
        flags=("--binary",),
    )
    assert "GIT binary patch" in diff
    inventory, _ = patch_inventory(str(repo), diff)
    assert inventory == ["protected/blob.bin"]
    assert not GUARD.check(inventory)[0]


@pytest.mark.parametrize("diff", ["", "not a patch\n", "--- a/x\n+++ b/x\n@@ garbage\n"])
def test_empty_or_unparseable_patch_has_no_inventory(tmp_path: Path, diff: str) -> None:
    repo = _repo(tmp_path, {"src/a.py": b"x\n"})
    inventory, why = patch_inventory(str(repo), diff)
    assert inventory is None and why


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside.py", "src/../../x.py", "src\\..\\x.py", "C:/x.py", ".git/config"],
)
def test_scope_guard_refuses_unsafe_paths_even_with_empty_allow(path: str) -> None:
    assert not ScopeGuard(allow=[], deny=[]).check([path])[0]


def test_commit_stages_only_the_inventory_and_reads_it_back(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": b"x\n", "protected/guard.py": b"a\n"})
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "checkout", "-qb", "fix")
    (repo / "src/a.py").write_text("y\n")
    (repo / "residue.log").write_text("test output\n")

    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    with patch.dict(os.environ, env):
        assert PRCreator.commit_and_push(str(repo), "fix", "m", paths=["src/a.py"])
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["src/a.py"]
    assert (repo / "residue.log").exists()  # left alone, not committed

    # An inventory naming nothing that changed stages nothing: refused.
    with patch.dict(os.environ, env):
        assert not PRCreator.commit_and_push(str(repo), "fix", "m", paths=["src/a.py"])


def test_changed_paths_sees_deletions_and_untracked(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": b"x\n"})
    (repo / "src/a.py").unlink()
    (repo / "new dir").mkdir()
    (repo / "new dir/n.py").write_text("n\n")
    assert changed_paths(str(repo)) == {"src/a.py", "new dir/n.py"}


# --- read boundary -----------------------------------------------------------

@pytest.fixture
def read_repo(tmp_path: Path) -> Path:
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-CONTENT")
    repo = _repo(tmp_path, {
        "src/main.py": b"def run(): pass\n",
        "src/.env": b"TOKEN=x\n",
        "protected/p.py": b"p\n",
        "tests/test_main.py": b"def test(): pass\n",
        "lib/other.py": b"o\n",
    })
    os.symlink(outside, repo / "src/escape.py")
    os.symlink(repo / "src/main.py", repo / "src/inner.py")
    _git(repo, "add", "src/escape.py", "src/inner.py")
    _git(repo, "commit", "-qm", "links")
    (repo / "src/untracked.py").write_text("u\n")
    return repo


@pytest.mark.parametrize("name, reason", [
    ("{outside}", "absolute path"),
    ("../outside.txt", "parent-directory component"),
    ("src/../../outside.txt", "parent-directory component"),
    ("src/escape.py", "symlink in path"),
    ("src/inner.py", "symlink in path"),
    ("src/untracked.py", "not a tracked file"),
    ("src/.env", "secret-shaped path"),
    ("protected/p.py", "in deny list"),
    ("lib/other.py", "not in allow list"),
    (".git/config", ".git path"),
    ("src/missing.py", "not a tracked file"),
])
def test_read_boundary_refuses_before_open(read_repo: Path, name: str, reason: str) -> None:
    name = name.format(outside=read_repo.parent / "outside.txt")
    refusals: list[str] = []
    guard = ScopeGuard(allow=["src/", "tests/", "protected/"], deny=["protected/"])
    with patch("pathlib.Path.read_text", side_effect=AssertionError("opened")):
        contents = _read_file_contents(str(read_repo), [name], guard, refusals)
    assert contents == {}
    assert len(refusals) == 1 and reason in refusals[0]
    assert "OUTSIDE-CONTENT" not in refusals[0]


def test_read_boundary_allows_tracked_in_scope_source_and_its_test(read_repo: Path) -> None:
    guard = ScopeGuard(allow=["src/"], deny=["protected/"])
    contents = _read_file_contents(str(read_repo), ["src/main.py:5", "./src/main.py"], guard)
    assert contents == {"src/main.py": "def run(): pass\n", "./src/main.py": "def run(): pass\n"}
    tests = _find_test_files(str(read_repo), ["src/main.py"], ScopeGuard(allow=[], deny=[]))
    assert tests == {"tests/test_main.py": "def test(): pass\n"}


def test_test_file_symlink_is_not_read(read_repo: Path) -> None:
    os.symlink(read_repo.parent / "outside.txt", read_repo / "tests/test_escape.py")
    _git(read_repo, "add", "tests/test_escape.py")
    assert _find_test_files(str(read_repo), ["src/escape.py"]) == {}


def test_non_git_directory_reads_nothing(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a\n")
    assert tracked_files(str(tmp_path)) is None
    path, reason = resolve_confined(str(tmp_path), "a.py", None, ScopeGuard([], []))
    assert path is None and "unavailable" in reason


# --- end to end through generate_fix -----------------------------------------

def _issue(affected: str) -> dict[str, object]:
    meta = {
        "report_id": "r1", "diagnosis_id": "d1", "flow_name": "test", "category": "CODE",
        "confidence": "0.95", "root_cause": "Bug", "remediation": "Fix",
        "affected_files": affected, "error_signature": "sig",
    }
    body = "<!-- flow-doctor-metadata\n" + "\n".join(f"{k}: {v}" for k, v in meta.items()) + "\n-->"
    return {"body": body, "labels": [{"name": "flow-doctor:fix"}]}


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "fd.yaml"
    cfg.write_text(
        f"flow_name: test\nstore:\n  type: sqlite\n  path: {tmp_path / 'fd.db'}\n"
        "diagnosis:\n  provider: router\n  model_group: med\n"
        "auto_fix:\n  enabled: true\n  scope:\n    allow: [src/]\n    deny: [protected/]\n"
    )
    return cfg


def test_generate_fix_refuses_escape_path_before_the_model_call(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/main.py": b"x\n"})
    gen = MagicMock()
    with patch("flow_doctor.fix.cli.fetch_issue", return_value=_issue("../outside.txt")), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"), \
         patch("flow_doctor.fix.cli.FixGenerator.generate", gen):
        outcome, msg = generate_fix(42, "o/r", "tok", config_path=str(_config(tmp_path)),
                                    dry_run=True, repo_path=str(repo))
    assert outcome is FixOutcome.SKIPPED and "read boundary" in msg
    gen.assert_not_called()


def test_generate_fix_refuses_denied_deletion_before_mutation(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/main.py": b"x\n", "protected/guard.py": b"a\n"})
    diff = _staged_patch(repo, ("rm", "-q", "protected/guard.py"))
    with patch("flow_doctor.fix.cli.fetch_issue", return_value=_issue("src/main.py")), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"), \
         patch("flow_doctor.fix.cli.FixGenerator.generate", return_value=diff), \
         patch("flow_doctor.fix.cli.PRCreator.apply_diff") as apply:
        outcome, msg = generate_fix(42, "o/r", "tok", config_path=str(_config(tmp_path)),
                                    dry_run=True, repo_path=str(repo))
    assert outcome is FixOutcome.SKIPPED and "Scope guard violations" in msg
    assert "protected/guard.py" in msg
    apply.assert_not_called()
    assert (repo / "protected/guard.py").exists()

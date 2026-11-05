"""Auto-fix runs in an owned worktree, never in the caller's checkout (alpha-engine-config-I12898).

The success path used to ``git checkout -b`` and commit in the caller's
``repo_path``, so a fix PR moved the caller's branch, and every run mutated the
same checkout. These drive the real ``generate_fix`` against a real temporary
repository with a real (bare) ``origin`` and assert:

- the caller's files, index, HEAD and branches are unchanged after success,
  apply failure and test failure;
- only the patch inventory reaches the pushed commit, which sits on the
  caller's HEAD;
- concurrent runs use distinct worktrees and branches, and cleanup removes
  only the run's own worktree.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from flow_doctor.fix.cli import FixOutcome, generate_fix
from flow_doctor.fix.pr_creator import PRCreator
from flow_doctor.fix.worktree import OwnedWorktree

_GIT_ID = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
UNRELATED_TRACKED = b"UNRELATED_UNCOMMITTED_WORK\n"
UNRELATED_UNTRACKED = b"scratch notes\n"
STAGED = b"staged but not committed\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


@pytest.fixture
def repo(tmp_path: Path, origin: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    # The commit identity comes from repo config, as on a CI runner.
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    files = {"src/main.py": b"x = 1\n", "src/other.py": b"y = 1\n", "src/staged.py": b"s = 1\n"}
    for rel, data in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    return repo


def _diff(repo: Path, writes: Dict[str, str]) -> str:
    for rel, text in writes.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        _git(repo, "add", "--", rel)
    diff = _git(repo, "diff", "--cached")
    _git(repo, "reset", "-q", "--hard")
    _git(repo, "clean", "-fdq")
    return diff


def _dirty(repo: Path) -> None:
    """Unrelated work: a tracked edit, a staged edit and an untracked file."""
    (repo / "src/other.py").write_bytes(UNRELATED_TRACKED)
    (repo / "src/staged.py").write_bytes(STAGED)
    _git(repo, "add", "--", "src/staged.py")
    (repo / "notes.txt").write_bytes(UNRELATED_UNTRACKED)


def _snapshot(repo: Path) -> Tuple[Dict[str, bytes], str, str, str, str]:
    """Every working-tree file, the index entries, HEAD, refs and config."""
    files = {
        str(p.relative_to(repo)): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }
    return (
        files,
        _git(repo, "ls-files", "-s"),
        _git(repo, "symbolic-ref", "HEAD") + _git(repo, "rev-parse", "HEAD"),
        _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
        (repo / ".git" / "config").read_text(),
    )


def _worktrees(repo: Path) -> List[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


def _fix_branches(origin: Path) -> List[str]:
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/flow-doctor/"],
        cwd=origin, check=True, capture_output=True, text=True,
    ).stdout
    return out.split()


def _issue() -> dict:
    meta = {
        "report_id": "r1", "diagnosis_id": "d1", "flow_name": "test", "category": "CODE",
        "confidence": "0.95", "root_cause": "Bug", "remediation": "Fix",
        "affected_files": "src/main.py", "error_signature": "sig",
    }
    body = "<!-- flow-doctor-metadata\n" + "\n".join(f"{k}: {v}" for k, v in meta.items()) + "\n-->"
    return {"body": body, "labels": [{"name": "flow-doctor:fix"}]}


def _config(tmp_path: Path, test_command: str) -> Path:
    cfg = tmp_path / "fd.yaml"
    cfg.write_text(
        f"flow_name: test\nstore:\n  type: sqlite\n  path: {tmp_path / 'fd.db'}\n"
        "diagnosis:\n  provider: router\n  model_group: med\n"
        # auto_fix.dry_run defaults on; these runs push for real (to a bare origin).
        "auto_fix:\n  enabled: true\n  dry_run: false\n"
        f"  test_command: {test_command!r}\n"
        "  scope:\n    allow: [src/]\n    deny: [protected/]\n"
    )
    return cfg


def _generate(tmp_path: Path, repo: Path, test_command: str, dry_run: bool = False):
    return generate_fix(42, "o/r", "tok", config_path=str(_config(tmp_path, test_command)),
                        dry_run=dry_run, repo_path=str(repo))


def _patched(diff: str):
    """Everything external to git mocked; git itself is real."""
    stack = [
        patch("flow_doctor.fix.cli.fetch_issue", return_value=_issue()),
        patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"),
        patch("flow_doctor.fix.cli.FixGenerator.generate", return_value=diff),
        patch("flow_doctor.fix.cli.PRCreator.create_pr",
              return_value="https://github.com/o/r/pull/1"),
        patch("flow_doctor.fix.cli._notify_telegram_pr"),
    ]
    return stack


class _Patches:
    def __init__(self, diff: str) -> None:
        self._stack = _patched(diff)
        self.mocks: List[MagicMock] = []

    def __enter__(self) -> "_Patches":
        self.mocks = [p.start() for p in self._stack]
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._stack):
            p.stop()


def test_success_leaves_the_dirty_caller_untouched_and_pushes_only_the_patch(
    tmp_path: Path, repo: Path, origin: Path,
) -> None:
    diff = _diff(repo, {"src/main.py": "x = 2\n", "src/new.py": "z = 1\n"})
    base = _git(repo, "rev-parse", "HEAD").strip()
    other = tmp_path / "users-own-worktree"
    _git(repo, "worktree", "add", "-q", str(other), "-b", "users-own")
    _dirty(repo)
    before = _snapshot(repo)
    seen = tmp_path / "test-cwd.txt"

    # The test command leaves residue in its tree; it must not reach the commit.
    with _Patches(diff) as p:
        outcome, msg = _generate(tmp_path, repo, f"pwd > {seen} && echo junk > residue.txt")
    assert outcome is FixOutcome.CREATED, msg

    assert _snapshot(repo) == before
    test_cwd = Path(seen.read_text().strip())
    assert test_cwd.resolve() != repo.resolve()
    assert not test_cwd.exists()
    assert not (repo / "residue.txt").exists()
    # Cleanup removed only its own worktree.
    assert [Path(w).resolve() for w in _worktrees(repo)] == [repo.resolve(), other.resolve()]
    assert other.is_dir()

    branches = _fix_branches(origin)
    assert len(branches) == 1 and branches[0].startswith("flow-doctor/test/")
    pr_kwargs = p.mocks[3].call_args.kwargs
    assert pr_kwargs["head"] == branches[0]

    def bare(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=origin, check=True,
                              capture_output=True, text=True).stdout

    assert bare("rev-parse", f"{branches[0]}^").strip() == base
    changed = bare("diff", "--name-only", "--no-renames", base, branches[0]).split()
    assert sorted(changed) == ["src/main.py", "src/new.py"]
    assert bare("show", f"{branches[0]}:src/main.py") == "x = 2\n"
    assert bare("show", f"{branches[0]}:src/other.py") == "y = 1\n"


@pytest.mark.parametrize("dry_run,test_command,expected", [
    (True, "true", FixOutcome.CREATED),
    (False, "echo residue >> src/main.py; false", FixOutcome.SKIPPED),
])
def test_dry_run_and_failed_tests_leave_caller_and_origin_untouched(
    tmp_path: Path, repo: Path, origin: Path, dry_run: bool, test_command: str,
    expected: FixOutcome,
) -> None:
    diff = _diff(repo, {"src/main.py": "x = 2\n"})
    _dirty(repo)
    before = _snapshot(repo)
    with _Patches(diff):
        outcome, msg = _generate(tmp_path, repo, test_command, dry_run=dry_run)
    assert outcome is expected, msg
    assert _snapshot(repo) == before
    assert [Path(w).resolve() for w in _worktrees(repo)] == [repo.resolve()]
    assert _fix_branches(origin) == []


def test_apply_failure_in_the_worktree_is_reported_and_cleaned_up(
    tmp_path: Path, repo: Path, origin: Path,
) -> None:
    diff = _diff(repo, {"src/main.py": "x = 2\n"})
    _dirty(repo)
    before = _snapshot(repo)
    with _Patches(diff), \
         patch("flow_doctor.fix.cli.PRCreator.apply_diff", return_value=False) as apply:
        outcome, msg = _generate(tmp_path, repo, "true")
    assert outcome is FixOutcome.FAILED and "apply" in msg
    applied_in = Path(apply.call_args.args[0])
    assert applied_in.resolve() != repo.resolve()
    assert not applied_in.exists()
    assert _snapshot(repo) == before
    assert [Path(w).resolve() for w in _worktrees(repo)] == [repo.resolve()]


def test_concurrent_runs_use_distinct_worktrees_and_branches(
    tmp_path: Path, repo: Path, origin: Path,
) -> None:
    diff = _diff(repo, {"src/main.py": "x = 2\n"})
    _dirty(repo)
    before = _snapshot(repo)
    log = tmp_path / "cwds.txt"
    # Each run's tests hold its worktree open long enough for the other to start.
    cmd = f"pwd >> {log} && sleep 1"
    results: List[Tuple[FixOutcome, str]] = []
    cfg = str(_config(tmp_path, cmd))  # written once: both runs read it

    def run() -> None:
        results.append(generate_fix(42, "o/r", "tok", config_path=cfg, repo_path=str(repo)))

    with _Patches(diff):
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert [o for o, _ in results] == [FixOutcome.CREATED, FixOutcome.CREATED], results
    cwds = log.read_text().split()
    assert len(cwds) == 2 and len(set(cwds)) == 2
    assert all(Path(c).resolve() != repo.resolve() and not Path(c).exists() for c in cwds)
    assert len(set(_fix_branches(origin))) == 2
    assert _snapshot(repo) == before
    assert [Path(w).resolve() for w in _worktrees(repo)] == [repo.resolve()]


def test_owned_worktree_refuses_a_checkout_with_no_commit(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    worktree, why = OwnedWorktree.create(str(empty))
    assert worktree is None and "no HEAD commit" in why


def test_owned_worktree_is_detached_at_the_resolved_head(repo: Path) -> None:
    worktree, why = OwnedWorktree.create(str(repo))
    assert worktree is not None, why
    try:
        wt = Path(worktree.path)
        assert worktree.base == _git(repo, "rev-parse", "HEAD").strip()
        assert _git(wt, "rev-parse", "HEAD").strip() == worktree.base
        detached = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=wt)
        assert detached.returncode != 0
    finally:
        assert worktree.remove()
    assert not Path(worktree.owned_dir).exists()
    assert [Path(w).resolve() for w in _worktrees(repo)] == [repo.resolve()]


def test_detached_push_uses_an_explicit_refspec_from_the_caller() -> None:
    with patch("flow_doctor.fix.pr_creator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
        ok = PRCreator.commit_and_push("/tmp/wt", "flow-doctor/x/1", "m", push_from="/tmp/repo")
    assert ok
    push = mock_run.call_args_list[-1]
    assert push.args[0] == ["git", "push", "origin", "abc123:refs/heads/flow-doctor/x/1"]
    assert push.kwargs["cwd"] == "/tmp/repo"
    assert not any(c.args[0][:2] == ["git", "checkout"] for c in mock_run.call_args_list)


def test_branch_names_differ_within_one_instant() -> None:
    names = {PRCreator.branch_name("flow") for _ in range(50)}
    assert len(names) == 50
    assert all(n.startswith("flow-doctor/flow/") and len(n.split("/")) == 3 for n in names)

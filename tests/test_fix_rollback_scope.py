"""The auto-fix rollback undoes its own patch and nothing else (alpha-engine-config-I12898).

``_revert_changes`` used to run ``git checkout .`` over the whole checkout, so a
dry run or a failed validation discarded every unrelated tracked edit in the
caller's working tree. These drive the real ``generate_fix`` against a real
temporary repository holding unrelated tracked and untracked work, and assert
that work is byte-for-byte unchanged on every path that rolls back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest

from flow_doctor.fix.cli import FixOutcome, _revert_changes, generate_fix
from flow_doctor.fix.scope_guard import patch_inventory

_GIT_ID = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
UNRELATED_TRACKED = b"UNRELATED_UNCOMMITTED_WORK\n"
UNRELATED_UNTRACKED = b"scratch notes\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, data in {"src/main.py": b"x = 1\n", "src/other.py": b"y = 1\n"}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _diff(repo: Path, writes: Dict[str, str]) -> str:
    """A patch making ``writes``, captured from the index and then undone."""
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
    """The caller's own unrelated work: a tracked edit and an untracked file."""
    (repo / "src/other.py").write_bytes(UNRELATED_TRACKED)
    (repo / "notes.txt").write_bytes(UNRELATED_UNTRACKED)


def _assert_untouched(repo: Path) -> None:
    assert (repo / "src/other.py").read_bytes() == UNRELATED_TRACKED
    assert (repo / "notes.txt").read_bytes() == UNRELATED_UNTRACKED


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
        "auto_fix:\n  enabled: true\n"
        f"  test_command: {test_command!r}\n"
        "  scope:\n    allow: [src/]\n    deny: [protected/]\n"
    )
    return cfg


def _run(tmp_path: Path, repo: Path, diff: str, test_command: str):
    with patch("flow_doctor.fix.cli.fetch_issue", return_value=_issue()), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"), \
         patch("flow_doctor.fix.cli.FixGenerator.generate", return_value=diff), \
         patch("flow_doctor.fix.cli.PRCreator.create_branch") as branch:
        result = generate_fix(42, "o/r", "tok", config_path=str(_config(tmp_path, test_command)),
                              dry_run=True, repo_path=str(repo))
    branch.assert_not_called()
    return result


def test_dry_run_rolls_back_only_the_patch(tmp_path: Path, repo: Path) -> None:
    diff = _diff(repo, {"src/main.py": "x = 2\n", "src/new.py": "z = 1\n"})
    _dirty(repo)
    outcome, msg = _run(tmp_path, repo, diff, "true")
    assert outcome is FixOutcome.CREATED, msg
    assert (repo / "src/main.py").read_bytes() == b"x = 1\n"
    assert not (repo / "src/new.py").exists()
    _assert_untouched(repo)


def test_failed_validation_rolls_back_only_the_patch(tmp_path: Path, repo: Path) -> None:
    diff = _diff(repo, {"src/main.py": "x = 2\n"})
    _dirty(repo)
    outcome, msg = _run(tmp_path, repo, diff, "false")
    assert outcome is FixOutcome.SKIPPED and "tests failed" in msg.lower()
    assert (repo / "src/main.py").read_bytes() == b"x = 1\n"
    _assert_untouched(repo)


def test_tests_that_edit_a_patched_file_still_roll_back_only_the_inventory(
    tmp_path: Path, repo: Path,
) -> None:
    """The reverse no longer applies; the fallback restores exactly the inventory."""
    diff = _diff(repo, {"src/main.py": "x = 2\n", "src/new.py": "z = 1\n"})
    _dirty(repo)
    cmd = "echo residue >> src/main.py && echo residue >> src/new.py && false"
    outcome, _ = _run(tmp_path, repo, diff, cmd)
    assert outcome is FixOutcome.SKIPPED
    assert (repo / "src/main.py").read_bytes() == b"x = 1\n"
    assert not (repo / "src/new.py").exists()
    _assert_untouched(repo)


def test_a_patch_touching_a_dirty_path_is_refused_before_apply(
    tmp_path: Path, repo: Path,
) -> None:
    diff = _diff(repo, {"src/other.py": "y = 2\n"})
    _dirty(repo)
    with patch("flow_doctor.fix.cli.PRCreator.apply_diff") as apply:
        outcome, msg = _run(tmp_path, repo, diff, "true")
    assert outcome is FixOutcome.SKIPPED and "uncommitted changes" in msg
    apply.assert_not_called()
    _assert_untouched(repo)


def test_a_patch_that_will_not_apply_changes_nothing(tmp_path: Path, repo: Path) -> None:
    diff = _diff(repo, {"src/main.py": "x = 2\n"})
    (repo / "src/main.py").write_bytes(b"x = 3\n")  # main moves on: the patch is stale
    _git(repo, "commit", "-qam", "move main")
    _dirty(repo)
    outcome, msg = _run(tmp_path, repo, diff, "true")
    assert outcome is not FixOutcome.CREATED, msg
    assert (repo / "src/main.py").read_bytes() == b"x = 3\n"
    _assert_untouched(repo)


def test_revert_changes_never_runs_a_whole_tree_checkout(repo: Path) -> None:
    diff = _diff(repo, {"src/main.py": "x = 2\n"})
    inventory, _ = patch_inventory(str(repo), diff)
    subprocess.run(["git", "apply", "-"], input=diff, cwd=repo, check=True, text=True)
    _dirty(repo)
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real(cmd, *a, **kw)

    with patch("subprocess.run", spy):
        assert _revert_changes(str(repo), diff, inventory)
    assert ["git", "checkout", "."] not in calls
    assert (repo / "src/main.py").read_bytes() == b"x = 1\n"
    _assert_untouched(repo)

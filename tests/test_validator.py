"""Tests for test validator."""

import subprocess
from unittest.mock import patch, MagicMock

from flow_doctor.fix.validator import TestValidator


def test_run_passing_tests():
    validator = TestValidator()

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "3 passed"
    mock_result.stderr = ""

    with patch("flow_doctor.fix.validator.subprocess.run", return_value=mock_result):
        passed, output = validator.run("pytest", "/tmp/repo")

    assert passed is True
    assert "3 passed" in output


def test_run_failing_tests():
    validator = TestValidator()

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "1 failed, 2 passed"
    mock_result.stderr = "FAILED test_main.py::test_foo"

    with patch("flow_doctor.fix.validator.subprocess.run", return_value=mock_result):
        passed, output = validator.run("pytest", "/tmp/repo")

    assert passed is False
    assert "failed" in output


def test_run_timeout():
    validator = TestValidator()

    with patch(
        "flow_doctor.fix.validator.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=60),
    ):
        passed, output = validator.run("pytest", "/tmp/repo", timeout=60)

    assert passed is False
    assert "timed out" in output


def test_run_execution_error():
    validator = TestValidator()

    with patch(
        "flow_doctor.fix.validator.subprocess.run",
        side_effect=FileNotFoundError("command not found"),
    ):
        passed, output = validator.run("nonexistent", "/tmp/repo")

    assert passed is False
    assert "failed to execute" in output.lower()


def test_run_combines_stdout_stderr():
    validator = TestValidator()

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "output"
    mock_result.stderr = "warnings"

    with patch("flow_doctor.fix.validator.subprocess.run", return_value=mock_result):
        passed, output = validator.run("pytest", "/tmp/repo")

    assert passed is True
    assert "output" in output
    assert "warnings" in output


def test_imports_resolve_to_the_worktree_not_the_source_checkout(tmp_path, monkeypatch):
    """codex dc27d7e2cef227b9: cwd alone let an inherited PYTHONPATH (or an
    editable install) import the caller's unpatched checkout."""
    import sys
    for root, value in (("source", 1), ("work", 2)):
        pkg = tmp_path / root / "src" / "fdprobe"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(f"VALUE = {value}\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "source" / "src"))
    cmd = f'"{sys.executable}" -c "import fdprobe, sys; sys.exit(fdprobe.VALUE != 2)"'
    passed, output = TestValidator().run(
        cmd, str(tmp_path / "work"), source_path=str(tmp_path / "source"),
    )
    assert passed, output

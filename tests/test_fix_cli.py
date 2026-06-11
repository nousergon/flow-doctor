"""Tests for fix CLI: metadata parsing, gate checks, orchestration."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from flow_doctor.fix.cli import (
    parse_issue_metadata,
    _is_config_credentials_issue,
    generate_fix,
    FixOutcome,
)


# --- Metadata parsing ---

def test_parse_issue_metadata():
    body = """\
Some issue text here.

<!-- flow-doctor-metadata
report_id: abc123
diagnosis_id: def456
flow_name: research-lambda
category: CODE
confidence: 0.92
error_signature: sig789
root_cause: Logic error in scanner
remediation: Fix the loop
affected_files: scanner.py,utils.py
-->
"""
    meta = parse_issue_metadata(body)
    assert meta is not None
    assert meta["report_id"] == "abc123"
    assert meta["diagnosis_id"] == "def456"
    assert meta["flow_name"] == "research-lambda"
    assert meta["category"] == "CODE"
    assert meta["confidence"] == "0.92"
    assert meta["affected_files"] == "scanner.py,utils.py"
    assert meta["root_cause"] == "Logic error in scanner"


def test_parse_issue_metadata_missing():
    body = "Just a regular issue body"
    assert parse_issue_metadata(body) is None


def test_parse_issue_metadata_empty_values():
    body = """\
<!-- flow-doctor-metadata
report_id: abc123
diagnosis_id: def456
flow_name: test
category: CODE
confidence: 0.5
error_signature:
root_cause: Something
remediation:
affected_files:
-->
"""
    meta = parse_issue_metadata(body)
    assert meta is not None
    assert meta["error_signature"] == ""
    assert meta["affected_files"] == ""


# --- Credentials gate ---

def test_credentials_issue_detected():
    assert _is_config_credentials_issue("Missing API key for service") is True
    assert _is_config_credentials_issue("Invalid credentials in config") is True
    assert _is_config_credentials_issue("password expired") is True
    assert _is_config_credentials_issue("secret not found in vault") is True


def test_non_credentials_issue():
    assert _is_config_credentials_issue("Wrong timeout value") is False
    assert _is_config_credentials_issue("Invalid format in config.yaml") is False


# --- Generate fix orchestration ---

def _mock_issue(metadata: dict) -> dict:
    """Create a mock GitHub issue response with metadata."""
    meta_lines = "\n".join(f"{k}: {v}" for k, v in metadata.items())
    body = f"Issue text\n\n<!-- flow-doctor-metadata\n{meta_lines}\n-->"
    return {"body": body, "number": 42}


def test_generate_fix_loads_config_with_unset_notifier_vars(tmp_path, monkeypatch):
    """Regression (alpha-engine-data #391): the fix CLI must load a config whose
    UNUSED notify/github blocks reference unset ${VAR}s (e.g. ${EMAIL_SENDER},
    ${FLOW_DOCTOR_GITHUB_TOKEN} on a CI runtime with no email creds). Previously
    this aborted at load_config with ConfigError before any fix work. The CLI now
    skips those sections; resolution stays strict for what it uses
    (diagnosis.api_key, set here), so it proceeds past config load to the gates.
    """
    cfg = """
flow_name: test
notify:
  - type: email
    sender: ${UNSET_EMAIL_SENDER}
    recipients: ${UNSET_EMAIL_RECIPIENTS}
    smtp_password: ${UNSET_GMAIL_APP_PASSWORD}
github:
  token: ${UNSET_FLOW_DOCTOR_GITHUB_TOKEN}
store:
  type: sqlite
  path: %s
diagnosis:
  enabled: true
  model: claude-haiku-4-5
  api_key: ${ANTHROPIC_API_KEY}
auto_fix:
  enabled: true
  model: claude-haiku-4-5
  confidence_threshold: 0.90
""" % (tmp_path / "fd.db")
    cfg_file = tmp_path / "flow-doctor.yaml"
    cfg_file.write_text(cfg)

    # No affected_files -> the run returns at that gate, which is AFTER
    # load_config. Reaching the gate at all proves the load no longer raises.
    issue = _mock_issue({
        "report_id": "r1", "diagnosis_id": "d1", "flow_name": "test",
        "category": "CODE", "confidence": "0.95",
        "root_cause": "Bug", "remediation": "Fix",
        "affected_files": "", "error_signature": "sig",
    })

    for var in ("UNSET_EMAIL_SENDER", "UNSET_EMAIL_RECIPIENTS",
                "UNSET_GMAIL_APP_PASSWORD", "UNSET_FLOW_DOCTOR_GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    with patch("flow_doctor.fix.cli.fetch_issue", return_value=issue), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"):
        outcome, msg = generate_fix(
            issue_number=42, repo="owner/repo", token="tok",
            config_path=str(cfg_file), dry_run=True, repo_path=str(tmp_path),
        )

    # Loaded past the unset notify/github ${VAR}s (previously ConfigError) and
    # reached the affected-files gate (a working-as-intended skip).
    assert outcome is FixOutcome.SKIPPED
    assert not outcome.is_error
    assert "affected files" in msg.lower()


def test_generate_fix_unfixable_category():
    issue = _mock_issue({
        "report_id": "r1",
        "diagnosis_id": "d1",
        "flow_name": "test",
        "category": "EXTERNAL",
        "confidence": "0.95",
        "root_cause": "API down",
        "remediation": "Wait",
        "affected_files": "client.py",
        "error_signature": "sig",
    })

    with patch("flow_doctor.fix.cli.fetch_issue", return_value=issue), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"):
        outcome, msg = generate_fix(
            issue_number=42, repo="owner/repo", token="tok",
            config_path=None, dry_run=True,
        )

    # EXTERNAL (provider outage) is a working-as-intended skip, NOT an error —
    # this is the exact case that was painting CI runs red. Must exit 0.
    assert outcome is FixOutcome.SKIPPED
    assert not outcome.is_error
    assert "not auto-fixable" in msg


def test_generate_fix_low_confidence():
    issue = _mock_issue({
        "report_id": "r1",
        "diagnosis_id": "d1",
        "flow_name": "test",
        "category": "CODE",
        "confidence": "0.5",
        "root_cause": "Bug",
        "remediation": "Fix",
        "affected_files": "main.py",
        "error_signature": "sig",
    })

    with patch("flow_doctor.fix.cli.fetch_issue", return_value=issue), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"):
        outcome, msg = generate_fix(
            issue_number=42, repo="owner/repo", token="tok",
            config_path=None, dry_run=True,
        )

    assert outcome is FixOutcome.SKIPPED
    assert not outcome.is_error
    assert "below threshold" in msg


def test_generate_fix_no_metadata():
    issue = {"body": "No metadata here", "number": 42}

    with patch("flow_doctor.fix.cli.fetch_issue", return_value=issue), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"):
        outcome, msg = generate_fix(
            issue_number=42, repo="owner/repo", token="tok",
            config_path=None, dry_run=True,
        )

    # Missing metadata on a fix-labelled issue is a genuine fault → FAILED.
    assert outcome is FixOutcome.FAILED
    assert outcome.is_error
    assert "No flow-doctor metadata" in msg


def test_generate_fix_config_credentials():
    issue = _mock_issue({
        "report_id": "r1",
        "diagnosis_id": "d1",
        "flow_name": "test",
        "category": "CONFIG",
        "confidence": "0.95",
        "root_cause": "Missing API key for external service",
        "remediation": "Add key",
        "affected_files": "config.py",
        "error_signature": "sig",
    })

    with patch("flow_doctor.fix.cli.fetch_issue", return_value=issue), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"):
        outcome, msg = generate_fix(
            issue_number=42, repo="owner/repo", token="tok",
            config_path=None, dry_run=True,
        )

    assert outcome is FixOutcome.SKIPPED
    assert not outcome.is_error
    assert "credentials" in msg.lower()


def test_generate_fix_no_affected_files():
    issue = _mock_issue({
        "report_id": "r1",
        "diagnosis_id": "d1",
        "flow_name": "test",
        "category": "CODE",
        "confidence": "0.95",
        "root_cause": "Bug",
        "remediation": "Fix",
        "affected_files": "",
        "error_signature": "sig",
    })

    with patch("flow_doctor.fix.cli.fetch_issue", return_value=issue), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue"):
        outcome, msg = generate_fix(
            issue_number=42, repo="owner/repo", token="tok",
            config_path=None, dry_run=True,
        )

    assert outcome is FixOutcome.SKIPPED
    assert not outcome.is_error
    assert "No affected files" in msg


# --- Outcome -> exit-code / comment semantics ---

def test_fix_outcome_is_error_mapping():
    """Only FAILED drives a non-zero exit; CREATED and SKIPPED stay green."""
    assert FixOutcome.FAILED.is_error is True
    assert FixOutcome.SKIPPED.is_error is False
    assert FixOutcome.CREATED.is_error is False


def test_skip_posts_informational_not_failure_comment():
    """A working-as-intended skip (EXTERNAL) must comment as a notification,
    not as a failure — this is what stops the issue from looking like an error.
    """
    issue = _mock_issue({
        "report_id": "r1", "diagnosis_id": "d1", "flow_name": "test",
        "category": "EXTERNAL", "confidence": "0.95",
        "root_cause": "Provider outage", "remediation": "Wait",
        "affected_files": "client.py", "error_signature": "sig",
    })

    captured = {}

    def _capture(repo, issue_number, body, token):
        captured["body"] = body

    with patch("flow_doctor.fix.cli.fetch_issue", return_value=issue), \
         patch("flow_doctor.fix.cli.GitHubNotifier.comment_on_issue", _capture):
        outcome, _ = generate_fix(
            issue_number=42, repo="owner/repo", token="tok",
            config_path=None, dry_run=True,
        )

    assert outcome is FixOutcome.SKIPPED
    body = captured["body"]
    assert "not an error" in body.lower()
    # Must NOT carry the failure framing reserved for genuine malfunctions.
    assert "fix generation failed" not in body.lower()

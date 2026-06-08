"""Regression tests for the v0.3.0 action.target capture.

Pins the new behavior introduced in flow-doctor 0.3.0: the Notifier.send()
return type changed from bool to Optional[str], and the dispatcher now
persists the returned target identifier in actions.target. This lets
operators link back from the DB to the filed GitHub issue, the email
recipients, or the Slack channel — answering the 2026-04-10 incident's
observation that actions.target was always None.
"""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from flow_doctor import FlowDoctor


@pytest.fixture
def sqlite_store(tmp_path):
    return {"type": "sqlite", "path": str(tmp_path / "flow_doctor.db")}


def test_github_notifier_returns_issue_url_on_success():
    """GitHubNotifier.send() should return the html_url from the API response."""
    from flow_doctor.notify.github import GitHubNotifier
    from flow_doctor.core.models import Report

    notifier = GitHubNotifier(repo="owner/repo", token="ghp_test")
    report = Report(
        flow_name="test",
        error_message="boom",
        severity="error",
        error_type="RuntimeError",
    )

    mock_resp = MagicMock()
    mock_resp.status = 201
    mock_resp.read.return_value = json.dumps(
        {"html_url": "https://github.com/owner/repo/issues/123", "number": 123}
    ).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("flow_doctor.notify.github.urlopen", return_value=mock_resp):
        target = notifier.send(report, "test-flow")

    assert target == "https://github.com/owner/repo/issues/123"


def test_github_notifier_returns_none_on_failure():
    """GitHubNotifier.send() should return None when the API call raises."""
    from flow_doctor.notify.github import GitHubNotifier
    from flow_doctor.core.models import Report

    notifier = GitHubNotifier(repo="owner/repo", token="ghp_test")
    report = Report(
        flow_name="test", error_message="boom", severity="error",
    )

    with patch("flow_doctor.notify.github.urlopen", side_effect=Exception("network")):
        target = notifier.send(report, "test-flow")

    assert target is None


def test_github_notifier_fallback_url_when_html_url_missing():
    """If GitHub response lacks html_url, fall back to a generic repo issues URL."""
    from flow_doctor.notify.github import GitHubNotifier
    from flow_doctor.core.models import Report

    notifier = GitHubNotifier(repo="owner/repo", token="ghp_test")
    report = Report(flow_name="test", error_message="boom", severity="error")

    mock_resp = MagicMock()
    mock_resp.status = 201
    mock_resp.read.return_value = json.dumps({}).encode("utf-8")  # no html_url
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("flow_doctor.notify.github.urlopen", return_value=mock_resp):
        target = notifier.send(report, "test-flow")

    # Fallback URL should still be a non-empty string so action.target is set
    assert target == "https://github.com/owner/repo/issues"


def test_email_notifier_returns_recipients_on_success():
    """EmailNotifier.send() should return the recipients string on success."""
    from flow_doctor.notify.email import EmailNotifier
    from flow_doctor.core.models import Report

    notifier = EmailNotifier(
        sender="alerts@example.com",
        recipients="oncall@example.com, backup@example.com",
        smtp_password="pw",
    )
    report = Report(flow_name="test", error_message="boom", severity="error")

    mock_smtp_instance = MagicMock()
    mock_smtp_cm = MagicMock()
    mock_smtp_cm.__enter__ = lambda s: mock_smtp_instance
    mock_smtp_cm.__exit__ = lambda s, *a: None

    with patch("flow_doctor.notify.email.smtplib.SMTP", return_value=mock_smtp_cm):
        target = notifier.send(report, "test-flow")

    assert target == "oncall@example.com, backup@example.com"


def test_slack_notifier_returns_channel_on_success():
    """SlackNotifier.send() should return the channel (not the webhook secret)."""
    from flow_doctor.notify.slack import SlackNotifier
    from flow_doctor.core.models import Report

    notifier = SlackNotifier(
        webhook_url="https://hooks.slack.com/secret-token-xyz",
        channel="#alerts",
    )
    report = Report(flow_name="test", error_message="boom", severity="error")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("flow_doctor.notify.slack.urlopen", return_value=mock_resp):
        target = notifier.send(report, "test-flow")

    assert target == "#alerts"
    # Must NOT leak the webhook URL (security)
    assert "secret-token-xyz" not in (target or "")


def test_dispatcher_persists_target_in_action_table(sqlite_store, monkeypatch):
    """The _send_notifications dispatcher should persist the URL in actions.target."""
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "ghp_test")

    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "cipher813/test"}],
    )

    # Patch the github notifier's send to return a known URL
    fake_url = "https://github.com/cipher813/test/issues/42"
    with patch.object(
        type(fd._notifiers[0]), "send", return_value=fake_url
    ):
        fd.report(
            RuntimeError("test error"),
            severity="error",
            context={"site": "test"},
        )

    # Query sqlite directly to confirm action.target was persisted
    conn = sqlite3.connect(sqlite_store["path"])
    rows = list(
        conn.execute(
            "SELECT action_type, status, target FROM actions ORDER BY id DESC LIMIT 1"
        )
    )
    assert len(rows) == 1
    action_type, status, target = rows[0]
    assert action_type == "github_issue"
    assert status == "sent"
    assert target == fake_url


def test_dispatcher_persists_none_target_on_failure(sqlite_store, monkeypatch):
    """If send() returns None (failure), action.target should be None."""
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "ghp_test")

    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "cipher813/test"}],
    )

    with patch.object(type(fd._notifiers[0]), "send", return_value=None):
        fd.report(RuntimeError("test"), severity="error")

    conn = sqlite3.connect(sqlite_store["path"])
    rows = list(
        conn.execute(
            "SELECT status, target FROM actions ORDER BY id DESC LIMIT 1"
        )
    )
    assert len(rows) == 1
    status, target = rows[0]
    assert status == "failed"
    assert target is None

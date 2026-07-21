"""Tests for notification backends."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import tempfile

from flow_doctor.core.models import Report
from flow_doctor.notify.slack import SlackNotifier
from flow_doctor.notify.email import EmailNotifier


class _SlackHandler(BaseHTTPRequestHandler):
    """Mock Slack webhook handler."""
    received = []

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        _SlackHandler.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass  # Silence test output


def test_slack_notifier_format():
    """Test Slack message formatting without actually sending."""
    report = Report(
        flow_name="test-flow",
        error_message="something broke",
        severity="error",
        error_type="ValueError",
        traceback="Traceback (most recent call last):\n  File 'test.py', line 1\nValueError: something broke",
    )
    msg = SlackNotifier._format_message(report, "test-flow")
    assert "test-flow" in msg
    assert "ValueError" in msg
    assert "something broke" in msg


def test_slack_notifier_cascade_format():
    report = Report(
        flow_name="predictor-training",
        error_message="training failed",
        severity="error",
        cascade_source="research-lambda",
    )
    msg = SlackNotifier._format_message(report, "predictor-training")
    assert "research-lambda" in msg


def test_slack_notifier_renders_logs_body():
    """notify_event()'s ``body`` is stored as Report.logs — the formatter
    must render it, or callers using notify_event(body=...) for detail
    (e.g. trade alerts) silently lose that detail in the delivered message."""
    report = Report(
        flow_name="executor",
        error_message="REDUCE COIN",
        severity="info",
        logs="Shares: 12 @ $151.23\nRealized P&L: +$340.11\nTrigger: atr_trail",
    )
    msg = SlackNotifier._format_message(report, "executor")
    assert "Realized P&L: +$340.11" in msg
    assert "Shares: 12 @ $151.23" in msg


def test_slack_notifier_sends():
    """Test actual HTTP sending to a mock server."""
    _SlackHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _SlackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    notifier = SlackNotifier(f"http://127.0.0.1:{port}/webhook")
    report = Report(
        flow_name="test",
        error_message="boom",
        severity="error",
    )
    target = notifier.send(report, "test")
    thread.join(timeout=5)
    server.server_close()

    # send() now returns Optional[str] — target identifier on success,
    # None on failure. Truthy string means the notifier delivered.
    assert target is not None
    assert isinstance(target, str)
    assert len(_SlackHandler.received) == 1
    assert "boom" in _SlackHandler.received[0]["text"]


def test_slack_notifier_handles_failure():
    """Slack notifier should return None on connection error, not raise."""
    notifier = SlackNotifier("http://127.0.0.1:1/nonexistent")
    report = Report(flow_name="test", error_message="boom", severity="error")
    result = notifier.send(report, "test")
    assert result is None


def test_email_notifier_format():
    """Test email body formatting."""
    report = Report(
        flow_name="test-flow",
        error_message="something broke",
        severity="critical",
        error_type="RuntimeError",
        traceback="Traceback line 1\nTraceback line 2",
        logs="2024-01-01 INFO Starting...\n2024-01-01 ERROR Failed",
    )
    body = EmailNotifier._format_body(report, "test-flow")
    assert "test-flow" in body
    assert "CRITICAL" in body
    assert "RuntimeError" in body
    assert "Traceback line 1" in body
    assert "Starting..." in body


def test_email_notifier_handles_failure():
    """Email notifier should return None on connection error, not raise."""
    notifier = EmailNotifier(
        sender="test@example.com",
        recipients="admin@example.com",
        smtp_host="localhost",
        smtp_port=1,  # Invalid port
    )
    report = Report(flow_name="test", error_message="boom", severity="error")
    result = notifier.send(report, "test")
    assert result is None

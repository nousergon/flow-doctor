"""Tests for the 0.5.0rc3 remediation → TelegramNotifier migration.

Covers:
- TelegramNotifier.send_raw() — adjacent subsystems (remediation,
  custom success pings) firing arbitrary text through the same bot
  + chat + thread routing.
- RemediationExecutor consuming a TelegramNotifier instance (the new
  preferred path) — verifies the executor calls send_raw() with the
  remediation-formatted body.
- RemediationExecutor legacy telegram_webhook_url path — verifies the
  bespoke urllib.urlopen POST still works for 0.4.x back-compat.
- _init_remediation in core/client.py building a TelegramNotifier from
  RemediationConfig.telegram_bot_token + telegram_chat_id fields.
"""

from __future__ import annotations

import json
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from flow_doctor.core.client import FlowDoctor
from flow_doctor.core.config import FlowDoctorConfig, RemediationConfig, StoreConfig
from flow_doctor.notify.telegram import TelegramNotifier
from flow_doctor.remediation.decision_gate import (
    Decision,
    DecisionType,
)
from flow_doctor.remediation.executor import ExecutionResult, RemediationExecutor


def _fake_urlopen_response(body: dict, status: int = 200):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    return resp


# ---------------------------------------------------------------------------
# TelegramNotifier.send_raw()
# ---------------------------------------------------------------------------


def test_send_raw_posts_text_to_chat_returns_target_id():
    notifier = TelegramNotifier(bot_token="t", chat_id=-100)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        target = notifier.send_raw("hello remediation")
    assert target == "telegram:-100"
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert payload["text"] == "hello remediation"
    assert payload["chat_id"] == -100
    assert payload["parse_mode"] == "Markdown"  # default


def test_send_raw_includes_message_thread_id_when_set():
    notifier = TelegramNotifier(bot_token="t", chat_id=1, message_thread_id=99)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        target = notifier.send_raw("threaded")
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert payload["message_thread_id"] == 99
    assert target == "telegram:1:99"


def test_send_raw_override_parse_mode_per_call():
    """parse_mode arg overrides the instance default — useful for the
    remediation pings where we may want plain text instead of Markdown."""
    notifier = TelegramNotifier(bot_token="t", chat_id=1, parse_mode="Markdown")
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        notifier.send_raw("plain", parse_mode=None)
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "parse_mode" not in payload


def test_send_raw_returns_none_on_api_failure():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response(
            {"ok": False, "description": "Forbidden"}
        )
        target = notifier.send_raw("nope")
    assert target is None


def test_send_raw_swallows_network_failures():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    with patch(
        "flow_doctor.notify.telegram.urlopen",
        side_effect=ConnectionError("offline"),
    ):
        # Must NOT raise — adjacent subsystems rely on this for
        # never-crash-the-caller semantics.
        target = notifier.send_raw("never raises")
    assert target is None


def test_send_raw_truncates_at_telegram_4096_limit():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    long = "x" * 5000
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        notifier.send_raw(long)
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert len(payload["text"]) == 4096
    assert payload["text"].endswith("[truncated]")


def test_send_raw_http_non_200_returns_none():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response(
            {"ok": True}, status=502
        )
        target = notifier.send_raw("x")
    assert target is None


# ---------------------------------------------------------------------------
# RemediationExecutor — first-class TelegramNotifier path (rc3+)
# ---------------------------------------------------------------------------


def _make_decision(success: bool = True) -> tuple[Decision, ExecutionResult]:
    """Build a minimal Decision + ExecutionResult fixture for the
    remediation notification path. The remediation pipeline normally
    populates these — here we hand-build them with just the fields the
    notification formatter actually reads."""
    decision = MagicMock(spec=Decision)
    decision.decision_type = DecisionType.AUTO_REMEDIATE
    decision.playbook_match = MagicMock()
    decision.playbook_match.name = "service_down"
    decision.diagnosis = MagicMock()
    decision.diagnosis.flow_name = "alpha-engine-predictor"
    decision.diagnosis.root_cause = "Lambda init exceeded 10s on cold-start"

    result = ExecutionResult(
        success=success,
        action_type="restart_service",
        dry_run=False,
        error="" if success else "ssm send_command failed",
    )
    return decision, result


def test_executor_with_telegram_notifier_invokes_send_raw():
    notifier = TelegramNotifier(bot_token="t", chat_id=-100)
    executor = RemediationExecutor(
        dry_run=False,
        store=MagicMock(),
        telegram_notifier=notifier,
    )
    decision, result = _make_decision(success=True)

    with patch.object(notifier, "send_raw", wraps=notifier.send_raw) as spy:
        with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
            executor._notify_telegram(decision, result)

    assert spy.call_count == 1
    sent_text = spy.call_args[0][0]
    assert "service_down" in sent_text
    assert "restart_service" in sent_text
    assert "alpha-engine-predictor" in sent_text
    assert "✅" in sent_text  # success emoji
    assert "DRY RUN" not in sent_text


def test_executor_failure_message_includes_error_and_red_emoji():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    executor = RemediationExecutor(
        store=MagicMock(), telegram_notifier=notifier
    )
    decision, result = _make_decision(success=False)

    with patch.object(notifier, "send_raw") as mock_send:
        executor._notify_telegram(decision, result)

    sent_text = mock_send.call_args[0][0]
    assert "❌" in sent_text
    assert "ssm send_command failed" in sent_text


def test_executor_dry_run_message_includes_dry_run_tag():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    executor = RemediationExecutor(
        store=MagicMock(), telegram_notifier=notifier
    )
    decision, result = _make_decision()
    result.dry_run = True

    with patch.object(notifier, "send_raw") as mock_send:
        executor._notify_telegram(decision, result)

    sent_text = mock_send.call_args[0][0]
    assert "[DRY RUN]" in sent_text


def test_executor_notifier_path_swallows_send_raw_exceptions():
    """send_raw already swallows + logs failures internally, but the
    executor adds a belt-and-suspenders try/except around it so an
    unexpected raise can't crash the remediation pipeline."""
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    executor = RemediationExecutor(
        store=MagicMock(), telegram_notifier=notifier
    )
    decision, result = _make_decision()

    with patch.object(
        notifier, "send_raw", side_effect=RuntimeError("synthetic")
    ):
        # Must NOT raise.
        executor._notify_telegram(decision, result)


# ---------------------------------------------------------------------------
# RemediationExecutor — legacy webhook URL path
# ---------------------------------------------------------------------------


def test_executor_legacy_webhook_url_still_posts():
    """Back-compat: 0.4.x configs only had telegram_webhook_url. The
    legacy code path must still POST the same body shape it did before."""
    executor = RemediationExecutor(
        store=MagicMock(),
        telegram_webhook_url="https://example.com/some-webhook",
    )
    decision, result = _make_decision()

    with patch("urllib.request.urlopen") as mock_urlopen:
        executor._notify_telegram(decision, result)

    assert mock_urlopen.call_count == 1
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://example.com/some-webhook"
    payload = json.loads(req.data.decode("utf-8"))
    assert "text" in payload
    assert "service_down" in payload["text"]


def test_executor_with_neither_telegram_path_is_noop():
    executor = RemediationExecutor(store=MagicMock())
    decision, result = _make_decision()

    with patch("urllib.request.urlopen") as mock_urlopen:
        executor._notify_telegram(decision, result)

    assert mock_urlopen.call_count == 0


def test_executor_prefers_notifier_over_legacy_url():
    """When both are configured (transitional config), the notifier
    wins. The legacy URL is the fallback for installs that haven't
    moved over yet."""
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    executor = RemediationExecutor(
        store=MagicMock(),
        telegram_notifier=notifier,
        telegram_webhook_url="https://legacy.example.com/hook",
    )
    decision, result = _make_decision()

    with patch.object(notifier, "send_raw") as mock_send_raw:
        with patch("urllib.request.urlopen") as mock_urlopen:
            executor._notify_telegram(decision, result)

    assert mock_send_raw.call_count == 1
    assert mock_urlopen.call_count == 0  # legacy path NOT touched


# ---------------------------------------------------------------------------
# _init_remediation in core/client.py — builds TelegramNotifier from
# RemediationConfig.telegram_bot_token + telegram_chat_id
# ---------------------------------------------------------------------------


def _build_fd_with_remediation_telegram(
    *,
    bot_token=None,
    chat_id=None,
    thread_id=None,
    webhook_url=None,
    db_path: str = ":memory:",
) -> FlowDoctor:
    config = FlowDoctorConfig(
        flow_name="rem-test",
        store=StoreConfig(type="sqlite", path=db_path),
        remediation=RemediationConfig(
            enabled=True,
            dry_run=True,
            telegram_bot_token=bot_token,
            telegram_chat_id=chat_id,
            telegram_message_thread_id=thread_id,
            telegram_webhook_url=webhook_url,
        ),
    )
    return FlowDoctor(config)


def test_init_remediation_builds_telegram_notifier_from_config(monkeypatch):
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _build_fd_with_remediation_telegram(
            bot_token="123:abc",
            chat_id=-100,
            thread_id=7,
            db_path=f.name,
        )
    executor = fd._remediation_executor
    assert executor is not None
    assert executor._telegram_notifier is not None
    assert executor._telegram_notifier.bot_token == "123:abc"
    assert executor._telegram_notifier.chat_id == -100
    assert executor._telegram_notifier.message_thread_id == 7


def test_init_remediation_pulls_telegram_creds_from_env(monkeypatch):
    """Same env-var contract as the standalone TelegramNotifier wiring."""
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", "-1001234")
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _build_fd_with_remediation_telegram(db_path=f.name)
    notifier = fd._remediation_executor._telegram_notifier
    assert notifier is not None
    assert notifier.bot_token == "env-token"
    assert notifier.chat_id == -1001234  # coerced from str


def test_init_remediation_no_telegram_when_only_legacy_url(monkeypatch):
    """If only the legacy webhook URL is set (and no bot creds via
    config or env), the new notifier is None and the executor falls
    through to the legacy POST path."""
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _build_fd_with_remediation_telegram(
            webhook_url="https://legacy.example/hook",
            db_path=f.name,
        )
    executor = fd._remediation_executor
    assert executor._telegram_notifier is None
    assert executor._telegram_url == "https://legacy.example/hook"

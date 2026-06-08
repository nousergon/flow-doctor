"""Tests for the Telegram Bot API notifier.

Covers the typed config, builder integration, init wiring (env-var
fallbacks + missing-field errors), payload shape, target-id return,
preflight handling, and length truncation.
"""

from __future__ import annotations

import io
import json
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from flow_doctor import (
    FlowDoctor,
    TelegramNotifierConfig,
)
from flow_doctor.core.config import NotifyChannelConfig
from flow_doctor.core.errors import ConfigError
from flow_doctor.core.models import ActionType, Report, Severity
from flow_doctor.notify.telegram import (
    _MAX_MESSAGE_LEN,
    TelegramNotifier,
    _truncate,
)


# ---------------------------------------------------------------------------
# TelegramNotifierConfig (typed)
# ---------------------------------------------------------------------------


def test_telegram_config_to_channel_config_round_trip():
    cfg = TelegramNotifierConfig(
        bot_token="123:abc",
        chat_id=-1001234567890,
        message_thread_id=42,
        parse_mode="MarkdownV2",
        disable_notification=True,
    )
    legacy = cfg.to_channel_config()
    assert isinstance(legacy, NotifyChannelConfig)
    assert legacy.type == "telegram"
    assert legacy.bot_token == "123:abc"
    assert legacy.chat_id == -1001234567890
    assert legacy.message_thread_id == 42
    assert legacy.parse_mode == "MarkdownV2"
    assert legacy.disable_notification is True


def test_telegram_config_accepts_string_chat_id_for_public_channels():
    cfg = TelegramNotifierConfig(bot_token="t", chat_id="@my_public_channel")
    assert cfg.chat_id == "@my_public_channel"


def test_telegram_config_default_parse_mode_is_markdown():
    cfg = TelegramNotifierConfig(bot_token="t", chat_id=1)
    assert cfg.parse_mode == "Markdown"
    assert cfg.disable_notification is False
    assert cfg.message_thread_id is None


# ---------------------------------------------------------------------------
# Builder integration
# ---------------------------------------------------------------------------


def test_builder_accepts_telegram_notifier_config():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = (
            FlowDoctor.builder("morning-signal")
            .with_store(path=f.name)
            .add_notifier(
                TelegramNotifierConfig(bot_token="123:abc", chat_id=-100)
            )
            .build()
        )
        assert len(fd.config.notify) == 1
        assert fd.config.notify[0].type == "telegram"
        assert fd.config.notify[0].bot_token == "123:abc"


# ---------------------------------------------------------------------------
# Init wiring — missing fields surface as ConfigError
# ---------------------------------------------------------------------------


def test_init_rejects_telegram_without_bot_token_or_chat_id(monkeypatch):
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        with pytest.raises(ConfigError) as exc:
            FlowDoctor.from_config(
                store={"type": "sqlite", "path": f.name},
                notify=[{"type": "telegram"}],
            )
    msg = str(exc.value)
    assert "bot_token" in msg
    assert "chat_id" in msg
    assert "@BotFather" in msg


def test_init_picks_telegram_creds_from_env(monkeypatch):
    """Numeric env chat_id coerces to int so the bot API receives the
    correct JSON type (negative ints for supergroups / channels)."""
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "123:env-token")
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", "-1001234567890")
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = FlowDoctor.from_config(
            store={"type": "sqlite", "path": f.name},
            notify=[{"type": "telegram"}],
        )
    assert len(fd._notifiers) == 1
    notifier = fd._notifiers[0]
    assert isinstance(notifier, TelegramNotifier)
    assert notifier.bot_token == "123:env-token"
    assert notifier.chat_id == -1001234567890  # coerced to int


def test_init_keeps_at_channel_chat_id_as_string(monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", "@my_channel")
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = FlowDoctor.from_config(
            store={"type": "sqlite", "path": f.name},
            notify=[{"type": "telegram"}],
        )
    assert fd._notifiers[0].chat_id == "@my_channel"


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


def _make_report(**overrides) -> Report:
    defaults = dict(
        flow_name="morning-signal",
        error_message="boom",
        severity=Severity.ERROR.value,
        error_type="ValueError",
        traceback=None,
    )
    defaults.update(overrides)
    return Report(**defaults)


def _fake_urlopen_response(body: dict, status: int = 200):
    """Build a context-manager-shaped fake for urlopen()."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    return resp


def test_send_posts_to_correct_bot_api_url():
    notifier = TelegramNotifier(bot_token="123:abc", chat_id=-100)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        target = notifier.send(_make_report(), "morning-signal")
    assert target == "telegram:-100"
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert req.get_method() == "POST"


def test_send_payload_includes_chat_id_text_and_parse_mode():
    notifier = TelegramNotifier(bot_token="t", chat_id=42)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        notifier.send(_make_report(), "morning-signal")
    req = mock_urlopen.call_args[0][0]
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["chat_id"] == 42
    assert payload["parse_mode"] == "Markdown"
    assert "boom" in payload["text"]
    assert "morning-signal" in payload["text"]


def test_send_payload_includes_message_thread_id_when_set():
    notifier = TelegramNotifier(
        bot_token="t", chat_id=1, message_thread_id=99
    )
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        target = notifier.send(_make_report(), "morning-signal")
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert payload["message_thread_id"] == 99
    # message_thread_id appears in the target id so operators can see
    # which forum topic an alert landed in.
    assert target == "telegram:1:99"


def test_send_payload_omits_message_thread_id_when_unset():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        notifier.send(_make_report(), "morning-signal")
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "message_thread_id" not in payload


def test_send_disable_notification_flag_passes_through():
    notifier = TelegramNotifier(
        bot_token="t", chat_id=1, disable_notification=True
    )
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        notifier.send(_make_report(), "morning-signal")
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert payload["disable_notification"] is True


def test_send_with_parse_mode_none_omits_field():
    notifier = TelegramNotifier(bot_token="t", chat_id=1, parse_mode=None)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
        notifier.send(_make_report(), "morning-signal")
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "parse_mode" not in payload


# ---------------------------------------------------------------------------
# Error paths — must NEVER raise upward
# ---------------------------------------------------------------------------


def test_send_returns_none_when_api_returns_ok_false():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response(
            {"ok": False, "description": "Forbidden: bot was kicked"}
        )
        target = notifier.send(_make_report(), "morning-signal")
    assert target is None


def test_send_returns_none_when_urlopen_raises():
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    with patch(
        "flow_doctor.notify.telegram.urlopen",
        side_effect=ConnectionError("network down"),
    ):
        target = notifier.send(_make_report(), "morning-signal")
    assert target is None


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_truncate_passes_short_text_unchanged():
    assert _truncate("hello") == "hello"


def test_truncate_caps_long_text_at_telegram_limit():
    long = "x" * (_MAX_MESSAGE_LEN + 500)
    out = _truncate(long)
    assert len(out) == _MAX_MESSAGE_LEN
    assert out.endswith("[truncated]")


# ---------------------------------------------------------------------------
# Action type dispatch
# ---------------------------------------------------------------------------


def test_send_notifications_dispatches_to_telegram_action_type(monkeypatch):
    """Verify the dispatch loop in _send_notifications correctly maps
    a TelegramNotifier instance to ActionType.TELEGRAM_ALERT so the
    persisted action row reflects the channel that actually fired."""
    import sqlite3

    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", "1")

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = FlowDoctor.from_config(
            store={"type": "sqlite", "path": f.name},
            notify=[{"type": "telegram"}],
        )

        with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
            report_id = fd.report(ValueError("boom"))

        assert report_id is not None
        # Inspect the actions table directly — the public storage API
        # doesn't expose a per-report fetch but the dispatch decision
        # lives in core/client.py and is the thing we want to verify.
        with sqlite3.connect(f.name) as conn:
            rows = conn.execute(
                "SELECT action_type, target FROM actions WHERE report_id = ?",
                (report_id,),
            ).fetchall()
        action_types = {row[0] for row in rows}
        assert ActionType.TELEGRAM_ALERT.value in action_types
        # And the persisted target should be the non-secret identifier
        # the notifier returns (telegram:chat_id), never the bot token.
        targets = {row[1] for row in rows if row[1] is not None}
        assert any("telegram:" in t for t in targets)
        assert not any("t" == t for t in targets)  # never the raw token


def test_successful_dispatch_emits_info_log_line(monkeypatch, caplog):
    """Symmetric observability: when a notifier successfully delivers a
    failure report, flow-doctor MUST log an INFO line so operators can
    confirm from journalctl that the alert fired. Without this, a
    successful dispatch is indistinguishable from a silent swallow.

    Surfaced 2026-05-26 — morning-signal's 5/26 AM (and 5/25 PM) cron
    firings failed and the operator received no Telegram. Investigation
    found flow-doctor was running and the failure path was entered, but
    there was no journal evidence that the dispatch actually fired. The
    success-side path already logs ``notify: success -> telegram:...``
    from the caller; this commit adds the symmetric log on the failure-
    report dispatch path.
    """
    import logging

    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", "1")

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = FlowDoctor.from_config(
            store={"type": "sqlite", "path": f.name},
            notify=[{"type": "telegram"}],
        )

        caplog.set_level(logging.INFO, logger="flow_doctor")
        with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _fake_urlopen_response({"ok": True})
            report_id = fd.report(ValueError("boom"))

    assert report_id is not None
    dispatch_logs = [
        r for r in caplog.records
        if r.levelno == logging.INFO
        and "dispatched report" in r.getMessage()
        and ActionType.TELEGRAM_ALERT.value in r.getMessage()
    ]
    assert dispatch_logs, (
        "Expected an INFO log line on successful notifier dispatch — "
        "this is the symmetric counterpart to the existing CRITICAL "
        "log on dispatch failure. Without it, operators can't tell "
        "from journalctl whether a failure report actually fired."
    )
    assert "telegram:" in dispatch_logs[0].getMessage()


# ---------------------------------------------------------------------------
# Preflight — bypassed by FLOW_DOCTOR_SKIP_PREFLIGHT
# ---------------------------------------------------------------------------


def test_validate_noops_when_skip_preflight_env_set(monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    # Should not touch the network.
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        notifier.validate()
    assert mock_urlopen.call_count == 0


def test_validate_raises_on_unauthorized_bot_token(monkeypatch):
    monkeypatch.delenv("FLOW_DOCTOR_SKIP_PREFLIGHT", raising=False)
    notifier = TelegramNotifier(bot_token="bad", chat_id=1)
    with patch("flow_doctor.notify.telegram.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _fake_urlopen_response(
            {"ok": False, "description": "Unauthorized"}, status=200
        )
        with pytest.raises(ConfigError) as exc:
            notifier.validate()
    assert "Unauthorized" in str(exc.value)
    assert "BotFather" in str(exc.value)

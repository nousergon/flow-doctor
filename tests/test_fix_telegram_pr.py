"""0.6.0rc3: the fix CLI pings Telegram when it opens an auto-fix PR, so the
PR is as visible as the original issue alert. Best-effort — a telegram failure
never raises (the PR already exists)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from flow_doctor.fix.cli import _notify_telegram_pr


def _telegram_channel(**kw):
    base = dict(
        type="telegram",
        bot_token="BOT",
        chat_id=-100123,
        message_thread_id=None,
        parse_mode="Markdown",
        disable_notification=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_uses_configured_telegram_notifier():
    config = SimpleNamespace(notify=[_telegram_channel()])
    with patch("flow_doctor.notify.telegram.TelegramNotifier.send_raw") as send_raw:
        send_raw.return_value = "telegram:-100123"
        _notify_telegram_pr(config, "data-collector", "https://gh/pr/7", 42)
    assert send_raw.call_count == 1
    msg = send_raw.call_args.args[0]
    assert "data-collector" in msg and "https://gh/pr/7" in msg and "#42" in msg


def test_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "BOT")
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", "-100999")
    config = SimpleNamespace(notify=[])  # nothing configured -> env path
    with patch("flow_doctor.notify.telegram.TelegramNotifier.send_raw") as send_raw:
        send_raw.return_value = "telegram:-100999"
        _notify_telegram_pr(config, "predictor", "https://gh/pr/9", 3)
    assert send_raw.call_count == 1


def test_skips_quietly_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)
    config = SimpleNamespace(notify=[])
    with patch("flow_doctor.notify.telegram.TelegramNotifier.send_raw") as send_raw:
        _notify_telegram_pr(config, "research", "https://gh/pr/1", 1)
    assert send_raw.call_count == 0  # no creds -> no send, no raise


def test_never_raises_on_telegram_failure():
    config = SimpleNamespace(notify=[_telegram_channel()])
    with patch("flow_doctor.notify.telegram.TelegramNotifier.send_raw") as send_raw:
        send_raw.side_effect = RuntimeError("telegram down")
        # Must not raise — the PR already exists; this is best-effort.
        _notify_telegram_pr(config, "backtester", "https://gh/pr/5", 8)

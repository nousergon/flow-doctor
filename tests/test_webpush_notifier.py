"""Tests for the Web Push (VAPID) notifier.

Covers the typed config, builder integration, init wiring (missing-krepis
ConfigError, subscription resolution from config/env, malformed-JSON
ConfigError), payload shape (title/body/target-id), and error paths that
must never raise. ``krepis.webpush`` is mocked via ``sys.modules``
injection throughout (mirrors ``test_telegram_notifier.py``'s krepis-mock
pattern) so the suite is deterministic regardless of whether the real
``krepis[webpush]`` package happens to be installed locally.
"""

from __future__ import annotations

import json
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from flow_doctor import (
    FlowDoctor,
    WebPushNotifierConfig,
)
from flow_doctor.core.config import NotifyChannelConfig
from flow_doctor.core.errors import ConfigError
from flow_doctor.core.models import Report, Severity
from flow_doctor.notify.webpush import WebPushNotifier

SUBSCRIPTION = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "keys": {"p256dh": "test-p256dh", "auth": "test-auth"},
}


def _mock_krepis_webpush(send_push_return=True, send_push_side_effect=None):
    """Build a fake krepis.webpush module + register it in sys.modules
    (also registers a bare `krepis` package module, since `import
    krepis.webpush` needs the parent package importable too)."""
    mock_send_push = MagicMock(return_value=send_push_return, side_effect=send_push_side_effect)
    mock_webpush_module = MagicMock(send_push=mock_send_push)
    mock_krepis_pkg = MagicMock(webpush=mock_webpush_module)
    return mock_krepis_pkg, mock_webpush_module, mock_send_push


# ---------------------------------------------------------------------------
# WebPushNotifierConfig (typed)
# ---------------------------------------------------------------------------


def test_webpush_config_to_channel_config_round_trip():
    cfg = WebPushNotifierConfig(
        subscription=SUBSCRIPTION,
        url="/dashboard",
        vapid_private_key="explicit-key",
        vapid_subject="mailto:me@example.com",
    )
    legacy = cfg.to_channel_config()
    assert isinstance(legacy, NotifyChannelConfig)
    assert legacy.type == "webpush"
    assert legacy.webpush_subscription == SUBSCRIPTION
    assert legacy.webpush_url == "/dashboard"
    assert legacy.webpush_vapid_private_key == "explicit-key"
    assert legacy.webpush_vapid_subject == "mailto:me@example.com"


def test_webpush_config_defaults():
    cfg = WebPushNotifierConfig()
    assert cfg.subscription is None
    assert cfg.url is None
    assert cfg.vapid_private_key is None
    assert cfg.vapid_subject is None


# ---------------------------------------------------------------------------
# Builder integration
# ---------------------------------------------------------------------------


def test_builder_accepts_webpush_notifier_config():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        with patch.dict(sys.modules, {"krepis": _mock_krepis_webpush()[0], "krepis.webpush": _mock_krepis_webpush()[1]}):
            fd = (
                FlowDoctor.builder("morning-signal")
                .with_store(path=f.name)
                .add_notifier(WebPushNotifierConfig(subscription=SUBSCRIPTION))
                .build()
            )
        assert len(fd.config.notify) == 1
        assert fd.config.notify[0].type == "webpush"
        assert fd.config.notify[0].webpush_subscription == SUBSCRIPTION


# ---------------------------------------------------------------------------
# Init wiring
# ---------------------------------------------------------------------------


def test_init_rejects_webpush_without_krepis_installed():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        # Force `import krepis.webpush` to raise ImportError regardless of
        # whether the real package happens to be installed in this env.
        with patch.dict(sys.modules, {"krepis.webpush": None}):
            with pytest.raises(ConfigError) as exc:
                FlowDoctor.from_config(
                    store={"type": "sqlite", "path": f.name},
                    notify=[{"type": "webpush", "webpush_subscription": SUBSCRIPTION}],
                )
    assert "flow-doctor[webpush]" in str(exc.value)


def test_init_rejects_webpush_without_subscription(monkeypatch):
    monkeypatch.delenv("FLOW_DOCTOR_WEBPUSH_SUBSCRIPTION", raising=False)
    mock_krepis, mock_wp, _ = _mock_krepis_webpush()
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
            with pytest.raises(ConfigError) as exc:
                FlowDoctor.from_config(
                    store={"type": "sqlite", "path": f.name},
                    notify=[{"type": "webpush"}],
                )
    assert "subscription" in str(exc.value)


def test_init_picks_subscription_from_env(monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_WEBPUSH_SUBSCRIPTION", json.dumps(SUBSCRIPTION))
    mock_krepis, mock_wp, _ = _mock_krepis_webpush()
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
            fd = FlowDoctor.from_config(
                store={"type": "sqlite", "path": f.name},
                notify=[{"type": "webpush"}],
            )
    assert len(fd._notifiers) == 1
    notifier = fd._notifiers[0]
    assert isinstance(notifier, WebPushNotifier)
    assert notifier.subscription == SUBSCRIPTION


def test_init_rejects_malformed_json_env_subscription(monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_WEBPUSH_SUBSCRIPTION", "{not valid json")
    mock_krepis, mock_wp, _ = _mock_krepis_webpush()
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
            with pytest.raises(ConfigError) as exc:
                FlowDoctor.from_config(
                    store={"type": "sqlite", "path": f.name},
                    notify=[{"type": "webpush"}],
                )
    assert "not valid JSON" in str(exc.value)


def test_init_explicit_subscription_overrides_env(monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_WEBPUSH_SUBSCRIPTION", json.dumps({"endpoint": "wrong", "keys": {}}))
    mock_krepis, mock_wp, _ = _mock_krepis_webpush()
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
            fd = FlowDoctor.from_config(
                store={"type": "sqlite", "path": f.name},
                notify=[{"type": "webpush", "webpush_subscription": SUBSCRIPTION}],
            )
    assert fd._notifiers[0].subscription == SUBSCRIPTION


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


def test_send_calls_krepis_send_push_with_subscription():
    mock_krepis, mock_wp, mock_send_push = _mock_krepis_webpush()
    notifier = WebPushNotifier(subscription=SUBSCRIPTION)
    with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
        target = notifier.send(_make_report(), "morning-signal")
    mock_send_push.assert_called_once()
    assert mock_send_push.call_args.args[0] == SUBSCRIPTION
    assert target == "webpush:fcm.googleapis.com"


def test_send_title_includes_severity_and_flow_name():
    mock_krepis, mock_wp, mock_send_push = _mock_krepis_webpush()
    notifier = WebPushNotifier(subscription=SUBSCRIPTION)
    with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
        notifier.send(_make_report(severity=Severity.CRITICAL.value), "morning-signal")
    title = mock_send_push.call_args.kwargs["title"]
    assert "CRITICAL" in title
    assert "morning-signal" in title


def test_send_body_includes_error_type_and_message():
    mock_krepis, mock_wp, mock_send_push = _mock_krepis_webpush()
    notifier = WebPushNotifier(subscription=SUBSCRIPTION)
    with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
        notifier.send(_make_report(), "morning-signal")
    body = mock_send_push.call_args.kwargs["body"]
    assert "ValueError" in body
    assert "boom" in body


def test_send_passes_url_and_tag():
    mock_krepis, mock_wp, mock_send_push = _mock_krepis_webpush()
    notifier = WebPushNotifier(subscription=SUBSCRIPTION, url="/dashboard")
    with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
        notifier.send(_make_report(), "morning-signal")
    assert mock_send_push.call_args.kwargs["url"] == "/dashboard"
    assert mock_send_push.call_args.kwargs["tag"] == "morning-signal"


def test_send_passes_explicit_vapid_overrides():
    mock_krepis, mock_wp, mock_send_push = _mock_krepis_webpush()
    notifier = WebPushNotifier(
        subscription=SUBSCRIPTION,
        vapid_private_key="k",
        vapid_subject="mailto:x@example.com",
    )
    with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
        notifier.send(_make_report(), "morning-signal")
    assert mock_send_push.call_args.kwargs["vapid_private_key"] == "k"
    assert mock_send_push.call_args.kwargs["vapid_subject"] == "mailto:x@example.com"


def test_target_id_redacts_full_endpoint_to_host_only():
    notifier = WebPushNotifier(subscription=SUBSCRIPTION)
    target = notifier._target_id()
    assert target == "webpush:fcm.googleapis.com"
    assert "abc123" not in target  # the capability path must never leak


def test_target_id_handles_missing_endpoint():
    notifier = WebPushNotifier(subscription={})
    assert notifier._target_id() == "webpush:unknown"


# ---------------------------------------------------------------------------
# Error paths — must NEVER raise upward
# ---------------------------------------------------------------------------


def test_send_returns_none_when_krepis_not_installed():
    notifier = WebPushNotifier(subscription=SUBSCRIPTION)
    with patch.dict(sys.modules, {"krepis.webpush": None}):
        assert notifier.send(_make_report(), "morning-signal") is None


def test_send_returns_none_when_send_push_returns_false():
    mock_krepis, mock_wp, _ = _mock_krepis_webpush(send_push_return=False)
    notifier = WebPushNotifier(subscription=SUBSCRIPTION)
    with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
        assert notifier.send(_make_report(), "morning-signal") is None


def test_send_returns_none_when_send_push_raises():
    mock_krepis, mock_wp, _ = _mock_krepis_webpush(send_push_side_effect=RuntimeError("boom"))
    notifier = WebPushNotifier(subscription=SUBSCRIPTION)
    with patch.dict(sys.modules, {"krepis": mock_krepis, "krepis.webpush": mock_wp}):
        assert notifier.send(_make_report(), "morning-signal") is None

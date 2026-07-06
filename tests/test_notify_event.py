"""Tests for notify_event() — intentional non-error fleet notifications."""

from __future__ import annotations

import tempfile
from typing import List, Optional

import pytest

from flow_doctor import DecisionReason, FlowDoctor
from flow_doctor.core.models import Report
from flow_doctor.notify.base import Notifier


class _RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.subjects: List[str] = []
        self.severities: List[str] = []

    def send(
        self, report: Report, flow_name: str, diagnosis=None
    ) -> Optional[str]:
        self.subjects.append(report.error_message)
        self.severities.append(report.severity)
        return "recording:ok"


@pytest.fixture
def fd():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        yield (
            FlowDoctor.builder("notify-event-test")
            .with_store(path=f.name)
            .with_dedup(cooldown_minutes=60)
            .build()
        )


def test_notify_event_delivers_with_custom_severity(fd):
    n = _RecordingNotifier()
    n.notify_on = {"critical", "error", "warning", "info"}
    fd._notifiers = [n]

    rid = fd.notify_event("trade alert", body="AAPL filled", severity="warning")
    assert rid is not None
    assert n.subjects == ["trade alert"]
    assert n.severities == ["warning"]


def test_notify_event_dedup_key_suppresses_repeat(fd):
    n = _RecordingNotifier()
    n.notify_on = {"info"}
    fd._notifiers = [n]

    first = fd.notify_event("heartbeat", dedup_key="fleet:heartbeat:daily")
    second = fd.notify_event("heartbeat", dedup_key="fleet:heartbeat:daily")

    assert first is not None
    assert second is None
    assert n.subjects == ["heartbeat"]


def test_notify_event_without_dedup_key_always_fires(fd):
    n = _RecordingNotifier()
    n.notify_on = {"info"}
    fd._notifiers = [n]

    first = fd.notify_event("ping one", severity="info")
    second = fd.notify_event("ping two", severity="info")

    assert first is not None
    assert second is not None
    assert n.subjects == ["ping one", "ping two"]


# --- last_dispatch_reason() / last_dispatched() (config#1813) ----------
#
# report id is non-None on every DecisionReason except `deduped` — a caller
# that treats "report id is not None" as "delivered" is wrong for
# severity_filtered / category_filtered / rate_limited / delivery_failed /
# no_notifiers. These accessors are the fix.


def test_last_dispatched_true_when_a_notifier_actually_receives_it(fd):
    n = _RecordingNotifier()
    n.notify_on = {"info"}
    fd._notifiers = [n]

    rid = fd.notify_event("trade filled", severity="info")

    assert rid is not None
    assert fd.last_dispatch_reason() == DecisionReason.FIRED.value
    assert fd.last_dispatched() is True


def test_last_dispatched_false_when_severity_filtered_despite_report_id(fd):
    """The exact config#1813 bug: a notifier configured for critical/error
    only (e.g. a stale override missing the trades Telegram topic) leaves
    an info-severity trade alert with NO eligible notifier. notify_event()
    still returns a report id (the event was seen + persisted), but
    last_dispatched() must be False — this is what a caller should check
    before logging "alert sent"."""
    n = _RecordingNotifier()
    n.notify_on = {"critical", "error"}  # no "info" -> trade alerts have no home
    fd._notifiers = [n]

    rid = fd.notify_event("REDUCE GE", severity="info")

    assert rid is not None  # the misleading part of the old bug
    assert fd.last_dispatch_reason() == DecisionReason.SEVERITY_FILTERED.value
    assert fd.last_dispatched() is False
    assert n.subjects == []  # confirms nothing was actually sent


def test_last_dispatched_false_with_zero_notifiers_configured(fd):
    fd._notifiers = []

    rid = fd.notify_event("nobody home", severity="info")

    assert rid is not None
    assert fd.last_dispatch_reason() == DecisionReason.NO_NOTIFIERS.value
    assert fd.last_dispatched() is False


def test_last_dispatch_reason_is_deduped_on_repeat_and_report_id_is_none(fd):
    n = _RecordingNotifier()
    n.notify_on = {"info"}
    fd._notifiers = [n]

    first = fd.notify_event("heartbeat", dedup_key="dedup:test", severity="info")
    second = fd.notify_event("heartbeat", dedup_key="dedup:test", severity="info")

    assert first is not None
    assert second is None
    assert fd.last_dispatch_reason() == DecisionReason.DEDUPED.value
    assert fd.last_dispatched() is False


def test_last_dispatch_reason_is_none_before_any_call(fd):
    assert fd.last_dispatch_reason() is None
    assert fd.last_dispatched() is False

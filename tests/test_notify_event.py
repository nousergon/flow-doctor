"""Tests for notify_event() — intentional non-error fleet notifications."""

from __future__ import annotations

import tempfile
from typing import List, Optional

import pytest

from flow_doctor import FlowDoctor
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

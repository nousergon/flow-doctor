"""0.6.0rc3 decision trace + heartbeat: every evaluated error records exactly
one DecisionReason (fired / deduped / rate_limited / severity_filtered /
delivery_failed / no_notifiers), and status()/log_summary() surface the
seen/fired/suppressed heartbeat so a quiet flow is legible rather than silent.
"""

from __future__ import annotations

import tempfile
from typing import List, Optional

import pytest

from flow_doctor import FlowDoctor
from flow_doctor.core.models import Decision, DecisionReason, Diagnosis, Report
from flow_doctor.notify.base import Notifier


class _RecordingNotifier(Notifier):
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def send(
        self, report: Report, flow_name: str, diagnosis: Optional[Diagnosis] = None
    ) -> Optional[str]:
        return None if self.fail else "recording:ok"


@pytest.fixture
def fd():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        yield FlowDoctor.builder("decision-test").with_store(path=f.name).build()


def _reasons(fd) -> List[str]:
    return list(fd._store.decision_breakdown_today("decision-test").keys())


# --- one decision per evaluated error ----------------------------------


def test_fired_records_fired(fd):
    fd._notifiers = [_RecordingNotifier()]
    fd.report(ValueError("boom"))
    assert fd._store.decision_breakdown_today("decision-test") == {
        DecisionReason.FIRED.value: 1
    }


def test_dedup_records_deduped(fd):
    fd._notifiers = [_RecordingNotifier()]
    fd.report(ValueError("same boom"))
    fd.report(ValueError("same boom"))  # within cooldown -> deduped
    breakdown = fd._store.decision_breakdown_today("decision-test")
    assert breakdown.get(DecisionReason.FIRED.value) == 1
    assert breakdown.get(DecisionReason.DEDUPED.value) == 1


def test_severity_filtered_is_now_recorded(fd):
    """The previously-invisible severity-skip branch leaves a trace."""
    n = _RecordingNotifier()
    n.notify_on = {"critical"}  # error won't match
    fd._notifiers = [n]
    fd.report(ValueError("an error, but no notifier wants errors"))
    assert fd._store.decision_breakdown_today("decision-test") == {
        DecisionReason.SEVERITY_FILTERED.value: 1
    }


def test_rate_limited_records_rate_limited(fd):
    fd._notifiers = [_RecordingNotifier()]
    # Force the limiter to degrade every dispatch.
    fd._rate_limiter.check = lambda action: "degrade"
    fd.report(ValueError("boom"))
    assert fd._store.decision_breakdown_today("decision-test") == {
        DecisionReason.RATE_LIMITED.value: 1
    }


def test_delivery_failed_records_delivery_failed(fd):
    fd._notifiers = [_RecordingNotifier(fail=True)]
    fd.report(ValueError("boom"))
    assert fd._store.decision_breakdown_today("decision-test") == {
        DecisionReason.DELIVERY_FAILED.value: 1
    }


def test_no_notifiers_records_no_notifiers(fd):
    fd._notifiers = []
    fd.report(ValueError("nobody listening"))
    assert fd._store.decision_breakdown_today("decision-test") == {
        DecisionReason.NO_NOTIFIERS.value: 1
    }


# --- heartbeat ----------------------------------------------------------


def test_status_includes_decision_breakdown(fd):
    fd._notifiers = [_RecordingNotifier()]
    fd.report(ValueError("a"))
    fd.report(ValueError("a"))  # deduped
    s = fd.status()
    assert s["errors_seen_today"] == 2
    assert s["decisions_today"][DecisionReason.FIRED.value] == 1
    assert s["decisions_today"][DecisionReason.DEDUPED.value] == 1


def test_log_summary_reports_seen_fired_suppressed(fd):
    n = _RecordingNotifier()
    n.notify_on = {"critical"}
    fd._notifiers = [n]
    fd.report(ValueError("filtered out"))  # severity_filtered
    summary = fd.log_summary()
    assert "seen=1" in summary
    assert "fired=0" in summary
    assert "suppressed=1" in summary
    assert "severity_filtered=1" in summary


def test_store_save_decision_roundtrip(fd):
    fd._store.save_decision(
        Decision(flow_name="decision-test", reason="fired", error_signature="abc123")
    )
    assert fd._store.decision_breakdown_today("decision-test") == {"fired": 1}

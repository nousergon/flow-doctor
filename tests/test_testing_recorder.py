"""Tests for the flow_doctor.testing pytest plugin + RecordingFlowDoctor.

The plugin is registered via [project.entry-points.pytest11] in
pyproject.toml. The fixture is exercised here both via direct import and
via the auto-discovered fixture name to verify the entry-point wiring.
"""

from __future__ import annotations

import asyncio

import pytest

import flow_doctor
from flow_doctor import FlowDoctorProtocol
from flow_doctor.testing import RecordingFlowDoctor, ReportedIncident


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_recording_flow_doctor_satisfies_protocol():
    """RecordingFlowDoctor must runtime-isinstance against the Protocol
    so production code typed as FlowDoctorProtocol accepts it as a drop-in."""
    rec = RecordingFlowDoctor()
    assert isinstance(rec, FlowDoctorProtocol)


# ---------------------------------------------------------------------------
# report() capture
# ---------------------------------------------------------------------------


def test_report_captures_exception_metadata():
    rec = RecordingFlowDoctor()
    rid = rec.report(ValueError("bad input"))
    assert rid == "recorded-1"
    assert len(rec.reports) == 1
    incident = rec.last
    assert isinstance(incident, ReportedIncident)
    assert incident.exc_type == "ValueError"
    assert incident.exc_message == "bad input"
    assert incident.severity == "error"


def test_report_captures_string_message():
    rec = RecordingFlowDoctor()
    rec.report("synthetic warning", severity="warning")
    assert rec.last.exc_type is None
    assert rec.last.error == "synthetic warning"
    assert rec.last.severity == "warning"


def test_report_captures_explicit_context_and_logs():
    rec = RecordingFlowDoctor()
    rec.report(
        RuntimeError("x"),
        context={"order_id": 42},
        logs="prior log line",
    )
    assert rec.last.context == {"order_id": 42}
    assert rec.last.logs == "prior log line"


def test_clear_resets_state():
    rec = RecordingFlowDoctor()
    rec.report("a")
    rec.report("b")
    rec.clear()
    assert rec.reports == []
    assert rec.report("c") == "recorded-1"


def test_of_type_filters_by_exception_class():
    rec = RecordingFlowDoctor()
    rec.report(ValueError("v"))
    rec.report(RuntimeError("r"))
    rec.report(ValueError("v2"))
    assert len(rec.of_type("ValueError")) == 2
    assert len(rec.of_type("RuntimeError")) == 1


# ---------------------------------------------------------------------------
# Ambient context propagation
# ---------------------------------------------------------------------------


def test_report_captures_ambient_flow_doctor_context():
    rec = RecordingFlowDoctor()
    with flow_doctor.context(flow_name="morning-signal", stage="rank"):
        rec.report(ValueError("rank failure"))
    assert rec.last.ambient_context == {
        "flow_name": "morning-signal",
        "stage": "rank",
    }


# ---------------------------------------------------------------------------
# guard() + monitor()
# ---------------------------------------------------------------------------


def test_guard_reports_and_reraises():
    rec = RecordingFlowDoctor()
    with pytest.raises(KeyError):
        with rec.guard():
            raise KeyError("missing")
    assert rec.last.exc_type == "KeyError"


def test_monitor_decorator_reports_and_reraises():
    rec = RecordingFlowDoctor()

    @rec.monitor
    def fail():
        raise ZeroDivisionError("nope")

    with pytest.raises(ZeroDivisionError):
        fail()
    assert rec.last.exc_type == "ZeroDivisionError"


# ---------------------------------------------------------------------------
# report_async()
# ---------------------------------------------------------------------------


def test_report_async_captures_in_recorder():
    rec = RecordingFlowDoctor()

    async def _run():
        await rec.report_async(ValueError("async"))

    asyncio.run(_run())
    assert rec.last.exc_type == "ValueError"


# ---------------------------------------------------------------------------
# Pytest plugin auto-fixture
# ---------------------------------------------------------------------------


def test_pytest_plugin_fixture_is_auto_discovered(flow_doctor_recorder):
    """``flow_doctor_recorder`` arrives without an explicit ``import`` —
    this exercises the [project.entry-points.pytest11] wiring."""
    assert isinstance(flow_doctor_recorder, RecordingFlowDoctor)
    flow_doctor_recorder.report(ValueError("fixture-supplied"))
    assert flow_doctor_recorder.last.exc_type == "ValueError"


def test_pytest_plugin_fixture_is_fresh_per_test_a(flow_doctor_recorder):
    flow_doctor_recorder.report("test-a")
    assert len(flow_doctor_recorder.reports) == 1


def test_pytest_plugin_fixture_is_fresh_per_test_b(flow_doctor_recorder):
    """If the fixture leaked state from test_a, this assertion would fail."""
    assert flow_doctor_recorder.reports == []

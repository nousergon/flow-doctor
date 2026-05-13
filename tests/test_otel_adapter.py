"""Tests for the OTel-compatible Report serialization adapter.

The adapter ships in 0.5.0 as a directional signal that v0.6.0 will
add an actual OTel exporter. Until then, consumers running on Datadog
/ Honeycomb / Grafana Cloud / Sentry-via-OTel can convert reports
themselves and ship through their own collector.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flow_doctor.core.models import Report, Severity
from flow_doctor.otel import report_to_otel_span_event


def _make_report(**overrides) -> Report:
    defaults = dict(
        flow_name="morning-signal",
        error_message="boom",
        severity=Severity.ERROR.value,
        error_type="ValueError",
        traceback="Traceback (most recent call last):\n  ValueError: boom",
        error_signature="vexsig-abc",
        context={"stage": "ingest", "run_id": "run-42"},
        created_at=datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Report(**defaults)


def test_top_level_shape_matches_otel_span_event():
    out = report_to_otel_span_event(_make_report())
    assert set(out.keys()) == {
        "resource",
        "name",
        "time_unix_nano",
        "severity_text",
        "severity_number",
        "attributes",
    }


def test_flow_name_maps_to_resource_service_name():
    out = report_to_otel_span_event(_make_report(flow_name="predictor"))
    assert out["resource"] == {"service.name": "predictor"}


def test_context_stage_promotes_to_event_name():
    out = report_to_otel_span_event(_make_report())
    assert out["name"] == "ingest"


def test_event_name_falls_back_when_no_stage():
    out = report_to_otel_span_event(_make_report(context={"run_id": "x"}))
    assert out["name"] == "report"


def test_severity_text_and_number_for_error():
    out = report_to_otel_span_event(_make_report(severity="error"))
    assert out["severity_text"] == "ERROR"
    assert out["severity_number"] == 17


def test_severity_text_and_number_for_warning_and_critical():
    out_warn = report_to_otel_span_event(_make_report(severity="warning"))
    assert (out_warn["severity_text"], out_warn["severity_number"]) == ("WARN", 13)

    out_crit = report_to_otel_span_event(_make_report(severity="critical"))
    assert (out_crit["severity_text"], out_crit["severity_number"]) == ("FATAL", 21)


def test_time_unix_nano_is_correct_for_utc_timestamp():
    ts = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    out = report_to_otel_span_event(_make_report(created_at=ts))
    expected_ns = int(ts.timestamp() * 1_000_000_000)
    assert out["time_unix_nano"] == expected_ns


def test_time_unix_nano_handles_naive_utc_datetime():
    """Report.created_at defaults to datetime.utcnow() (naive). The
    adapter must still produce a sane Unix-nano value rather than
    interpreting the naive timestamp as local time."""
    naive = datetime(2026, 5, 13, 12, 0, 0)
    out = report_to_otel_span_event(_make_report(created_at=naive))
    expected_ns = int(
        naive.replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000
    )
    assert out["time_unix_nano"] == expected_ns


def test_exception_fields_land_in_attributes():
    out = report_to_otel_span_event(_make_report())
    attrs = out["attributes"]
    assert attrs["exception.type"] == "ValueError"
    assert attrs["exception.message"] == "boom"
    assert attrs["exception.stacktrace"].startswith("Traceback")


def test_flow_doctor_specific_fields_use_prefixed_keys():
    out = report_to_otel_span_event(
        _make_report(error_signature="sig-xyz", cascade_source="upstream-flow")
    )
    attrs = out["attributes"]
    assert attrs["flow_doctor.error_signature"] == "sig-xyz"
    assert attrs["flow_doctor.cascade_source"] == "upstream-flow"


def test_context_extras_flatten_with_context_prefix():
    out = report_to_otel_span_event(
        _make_report(
            context={
                "stage": "rank",
                "run_id": "run-42",
                "nested": {"a": 1, "b": 2},
            }
        )
    )
    attrs = out["attributes"]
    assert attrs["context.run_id"] == "run-42"
    assert attrs["context.nested.a"] == 1
    assert attrs["context.nested.b"] == 2


def test_stage_not_duplicated_into_attributes():
    """stage is promoted to ``event.name`` — leaving it in attributes
    too would double-count it for collectors using both."""
    out = report_to_otel_span_event(_make_report())
    assert "context.stage" not in out["attributes"]


def test_flow_name_not_duplicated_into_attributes_via_context():
    """flow_name on the contextvars layer lands in ``context``; the
    adapter must not re-emit it as a context.* attribute since it's
    already on the resource."""
    out = report_to_otel_span_event(
        _make_report(context={"flow_name": "morning-signal", "stage": "x"})
    )
    assert "context.flow_name" not in out["attributes"]


def test_non_primitive_context_values_coerce_to_str():
    """OTel attributes must be str|bool|int|float — non-primitives get
    stringified instead of breaking the exporter."""

    class _Custom:
        def __str__(self) -> str:
            return "<Custom>"

    out = report_to_otel_span_event(
        _make_report(context={"obj": _Custom(), "tup": (1, "two")})
    )
    attrs = out["attributes"]
    assert attrs["context.obj"] == "<Custom>"
    # Mixed-type list coerced to stringified homogeneous list
    assert attrs["context.tup"] == ["1", "two"]


def test_homogeneous_list_preserved_as_native_list():
    out = report_to_otel_span_event(
        _make_report(context={"tags": ["a", "b", "c"]})
    )
    assert out["attributes"]["context.tags"] == ["a", "b", "c"]


def test_dedup_count_only_emitted_when_aggregated():
    """dedup_count=1 is the trivial case (no aggregation); skip the
    attribute to keep payloads small. Anything else emits."""
    out_solo = report_to_otel_span_event(_make_report())
    assert "flow_doctor.dedup_count" not in out_solo["attributes"]

    out_dup = report_to_otel_span_event(_make_report(dedup_count=5))
    assert out_dup["attributes"]["flow_doctor.dedup_count"] == 5

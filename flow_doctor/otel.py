"""OpenTelemetry-compatible serialization for ``Report``.

Pure-Python adapter — no ``opentelemetry-*`` dependency. The actual
exporter (which would speak OTLP to a collector) is deferred to v0.6.0
so the optional ``opentelemetry-exporter-otlp`` dep family can land in
its own release cycle. What ships here is the shape that exporter will
emit, so callers running on Datadog / Honeycomb / Grafana Cloud /
Sentry-via-OTel can already convert reports to OTel ``SpanEvent``
dicts and ship them through their own collector today.

Field mapping (plan table):

==================  =========================================
Flow Doctor field   OTel SpanEvent field
==================  =========================================
flow_name           ``resource.service.name``
context["stage"]    ``event.name`` (falls back to "report")
error_type          ``exception.type``           (attribute)
error_message       ``exception.message``        (attribute)
traceback           ``exception.stacktrace``     (attribute)
severity            ``event.severity_text`` + ``severity_number``
created_at          ``time_unix_nano``           (top-level)
context (other)     flattened into ``attributes`` with
                    ``context.<key>`` dot-prefix
error_signature     ``flow_doctor.error_signature`` attribute
cascade_source      ``flow_doctor.cascade_source`` attribute
==================  =========================================

OTel attribute values are restricted to ``str | bool | int | float``
(or homogeneous arrays of those). Nested dicts in ``context`` are
flattened with dot-separated keys; non-primitive values are coerced
to ``str(value)`` so the shape stays exporter-safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Tuple

from flow_doctor.core.models import Report, Severity

# OTel severity_number table (subset relevant to us). Full table at
# https://opentelemetry.io/docs/specs/otel/logs/data-model/#field-severitynumber
_SEVERITY_TEXT_TO_NUMBER: Dict[str, Tuple[str, int]] = {
    Severity.CRITICAL.value: ("FATAL", 21),
    Severity.ERROR.value: ("ERROR", 17),
    Severity.WARNING.value: ("WARN", 13),
}


def _coerce_attribute_value(v: Any) -> Any:
    """Coerce a Python value to something OTel attributes accept."""
    if isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        # OTel arrays must be homogeneous. We promote to a stringified
        # list when items are heterogeneous, which is exporter-safe.
        coerced = [_coerce_attribute_value(item) for item in v]
        types = {type(x) for x in coerced}
        if len(types) <= 1:
            return coerced
        return [str(x) for x in coerced]
    if v is None:
        return ""
    return str(v)


def _flatten_context(
    ctx: Mapping[str, Any], prefix: str = "context"
) -> Iterable[Tuple[str, Any]]:
    """Yield ``(attribute_key, attribute_value)`` pairs flattened from a
    nested context dict. Nested dicts get dot-prefixed keys; lists get
    coerced to OTel-array-safe form via ``_coerce_attribute_value``."""
    for k, v in ctx.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, Mapping):
            yield from _flatten_context(v, prefix=key)
        else:
            yield key, _coerce_attribute_value(v)


def _to_unix_nano(ts: datetime) -> int:
    """Convert a (naive UTC by convention) ``datetime`` to nanoseconds
    since the Unix epoch."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1_000_000_000)


def report_to_otel_span_event(report: Report) -> Dict[str, Any]:
    """Serialize a :class:`Report` to an OTel ``SpanEvent``-shaped dict.

    The result is JSON-safe and ready to fan-out to any OTel collector
    via a future ``OTelExporter`` notifier (v0.6.0) or via the caller's
    own collector today. Top-level shape::

        {
            "resource": {"service.name": "<flow_name>"},
            "name": "<event.name>",
            "time_unix_nano": 1735603200000000000,
            "severity_text": "ERROR",
            "severity_number": 17,
            "attributes": {
                "exception.type": "ValueError",
                "exception.message": "boom",
                "exception.stacktrace": "...",
                "flow_doctor.error_signature": "...",
                "context.stage": "ingest",
                "context.run_id": "abc",
            },
        }
    """
    context: Dict[str, Any] = report.context or {}
    stage = context.get("stage")
    event_name = stage if stage else "report"

    sev_text, sev_number = _SEVERITY_TEXT_TO_NUMBER.get(
        report.severity, (report.severity.upper(), 17)
    )

    attributes: Dict[str, Any] = {}
    if report.error_type:
        attributes["exception.type"] = report.error_type
    if report.error_message:
        attributes["exception.message"] = report.error_message
    if report.traceback:
        attributes["exception.stacktrace"] = report.traceback
    if report.error_signature:
        attributes["flow_doctor.error_signature"] = report.error_signature
    if report.cascade_source:
        attributes["flow_doctor.cascade_source"] = report.cascade_source
    if report.dedup_count and report.dedup_count != 1:
        attributes["flow_doctor.dedup_count"] = report.dedup_count
    if report.logs:
        attributes["flow_doctor.logs"] = report.logs

    # Flatten any remaining context dict entries (skip the stage we
    # already promoted to event.name + the flow_name which is on the
    # resource — keeping them only in attributes would duplicate).
    for k, v in _flatten_context(context):
        # ``context.flow_name`` and ``context.stage`` are promoted —
        # skip duplicates.
        if k in ("context.flow_name", "context.stage"):
            continue
        attributes[k] = v

    return {
        "resource": {"service.name": report.flow_name},
        "name": event_name,
        "time_unix_nano": _to_unix_nano(report.created_at),
        "severity_text": sev_text,
        "severity_number": sev_number,
        "attributes": attributes,
    }


__all__ = ["report_to_otel_span_event"]

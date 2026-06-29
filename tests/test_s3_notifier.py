"""Tests for the S3 notifier — covers schema-1.0.0 payload shape,
severity mapping, diagnosis-category normalization, event_id derivation,
and the missing-boto3 / preflight paths.

Closes Gap 1 of the changelog event-mining coverage gaps roadmap item:
flow-doctor reports now land in the structured changelog corpus at
s3://<bucket>/changelog/entries/{YYYY-MM-DD}/{event_id}.json.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from flow_doctor.core.errors import ConfigError
from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.s3 import (
    HEARTBEAT_PREFIX,
    HEARTBEAT_SCHEMA_VERSION,
    S3Notifier,
    SCHEMA_VERSION,
    SEVERITY_MAP,
    _build_summary,
    _event_id,
    _format_iso_utc,
    _map_diagnosis_category,
    write_heartbeat,
)


# --- helpers --------------------------------------------------------

def _report(**overrides) -> Report:
    base = dict(
        flow_name="test_flow",
        error_message="boom",
        severity="error",
        error_type="RuntimeError",
        traceback="Traceback (most recent call last):\n  File ...",
    )
    base.update(overrides)
    # Pin created_at so event_ids are deterministic in tests
    r = Report(**base)
    r.created_at = datetime(2026, 5, 1, 13, 0, 0, tzinfo=timezone.utc)
    return r


def _diagnosis(**overrides) -> Diagnosis:
    base = dict(
        report_id="r1",
        flow_name="test_flow",
        category="lambda_oom",
        root_cause="Heap exhausted at indicator computation",
        confidence=0.8,
    )
    base.update(overrides)
    return Diagnosis(**base)


def _patched_send(notifier: S3Notifier, report: Report, **kwargs) -> tuple[str | None, dict]:
    """Run notifier.send with a mocked boto3 client; return (target, captured_payload)."""
    captured = {}

    class _FakeClient:
        def put_object(self, **kw):
            captured.update(kw)

    fake_boto3 = MagicMock()
    fake_boto3.client = MagicMock(return_value=_FakeClient())
    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        target = notifier.send(report, report.flow_name, **kwargs)

    payload = (
        json.loads(captured["Body"].decode()) if "Body" in captured else None
    )
    return target, captured, payload  # type: ignore[return-value]


# --- payload shape --------------------------------------------------

def test_send_writes_schema_v1_payload():
    notifier = S3Notifier(bucket="test-bucket", subsystem="data_pipeline")
    target, captured, payload = _patched_send(notifier, _report())

    assert target.startswith("s3://test-bucket/changelog/entries/2026-05-01/")
    assert captured["Bucket"] == "test-bucket"
    assert captured["Key"].startswith("changelog/entries/2026-05-01/")
    assert captured["Key"].endswith(".json")
    assert captured["ContentType"] == "application/json"

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["event_type"] == "incident"
    assert payload["subsystem"] == "data_pipeline"
    assert payload["actor"] == "test_flow"
    assert payload["source"] == "flow-doctor"
    assert payload["auto_emitted"] is True
    assert payload["detected_at"] == "2026-05-01T13:00:00Z"
    assert payload["resolved_at"] is None
    assert payload["resolution_notes"] is None
    assert payload["git_refs"] == []


def test_event_id_deterministic_and_well_formed():
    notifier = S3Notifier(bucket="b", subsystem="executor")
    _, _, payload = _patched_send(notifier, _report())
    eid = payload["event_id"]
    # Format: {ts_id}_{flow_name_safe}_{7-hex}. flow_name may itself contain
    # underscores so don't split on `_` — match prefix + suffix instead.
    assert eid.startswith("2026-05-01T13-00-00_test_flow_")
    assert len(eid.rsplit("_", 1)[-1]) == 7


def test_event_id_helper_matches_emitter_scheme():
    """event_id digest must match what the SNS-mirror Lambda + composite
    action produce for the same logical event — joins across the corpus
    require this to be byte-stable."""
    eid = _event_id("2026-05-01T13:00:00Z", "weekly_collector", "phase 1 failed")
    assert eid.startswith("2026-05-01T13-00-00_weekly_collector_")
    assert len(eid.rsplit("_", 1)[-1]) == 7
    # Hash deterministic
    assert _event_id("2026-05-01T13:00:00Z", "weekly_collector", "phase 1 failed") == eid


# --- severity mapping ----------------------------------------------

def test_severity_critical_to_critical():
    notifier = S3Notifier(bucket="b", subsystem="executor")
    _, _, payload = _patched_send(notifier, _report(severity="critical"))
    assert payload["severity"] == "critical"


def test_severity_error_to_high():
    notifier = S3Notifier(bucket="b", subsystem="executor")
    _, _, payload = _patched_send(notifier, _report(severity="error"))
    assert payload["severity"] == "high"


def test_severity_warning_to_medium():
    notifier = S3Notifier(bucket="b", subsystem="executor")
    _, _, payload = _patched_send(notifier, _report(severity="warning"))
    assert payload["severity"] == "medium"


def test_severity_unknown_falls_back_to_high():
    notifier = S3Notifier(bucket="b", subsystem="executor")
    _, _, payload = _patched_send(notifier, _report(severity="bogus"))
    assert payload["severity"] == "high"


def test_severity_map_pins_all_three():
    assert SEVERITY_MAP == {
        "critical": "critical",
        "error": "high",
        "warning": "medium",
    }


# --- diagnosis category mapping ------------------------------------

def test_diagnosis_category_lambda_oom_to_infrastructure_failure():
    assert _map_diagnosis_category("lambda_oom") == "infrastructure_failure"


def test_diagnosis_category_handles_case_and_separators():
    assert _map_diagnosis_category("Lambda OOM") == "infrastructure_failure"
    assert _map_diagnosis_category("lambda-oom") == "infrastructure_failure"


def test_diagnosis_category_unknown_returns_none():
    assert _map_diagnosis_category("totally_made_up") is None


def test_diagnosis_overrides_default_root_cause_when_mapped():
    notifier = S3Notifier(bucket="b", subsystem="executor", default_root_cause_category="code_bug")
    _, _, payload = _patched_send(
        notifier, _report(), diagnosis=_diagnosis(category="lambda_oom"),
    )
    assert payload["root_cause_category"] == "infrastructure_failure"


def test_diagnosis_unmapped_falls_back_to_default():
    notifier = S3Notifier(bucket="b", subsystem="executor", default_root_cause_category="code_bug")
    _, _, payload = _patched_send(
        notifier, _report(), diagnosis=_diagnosis(category="some_new_category"),
    )
    assert payload["root_cause_category"] == "code_bug"


def test_no_diagnosis_uses_configured_default():
    notifier = S3Notifier(bucket="b", subsystem="executor", default_root_cause_category="data_quality")
    _, _, payload = _patched_send(notifier, _report())
    assert payload["root_cause_category"] == "data_quality"


# --- description / context capture ---------------------------------

def test_description_concatenates_all_context():
    notifier = S3Notifier(bucket="b", subsystem="executor")
    _, _, payload = _patched_send(
        notifier,
        _report(
            error_message="Heap exhausted",
            error_type="MemoryError",
            traceback="line1\nline2",
            logs="2026-05-01 13:00:00 INFO Starting...",
        ),
    )
    desc = payload["description"]
    assert "Heap exhausted" in desc
    assert "MemoryError" in desc
    assert "line1\nline2" in desc
    assert "Starting..." in desc


def test_run_id_pulled_from_context():
    notifier = S3Notifier(bucket="b", subsystem="research")
    r = _report()
    r.context = {"run_id": "abc-123-def", "extra": "ignored"}
    _, _, payload = _patched_send(notifier, r)
    assert payload["run_id"] == "abc-123-def"


# --- summary rendering ---------------------------------------------

def test_summary_combines_flow_and_message():
    s = _build_summary("phase1_collector", "FRED API timeout")
    assert s == "phase1_collector: FRED API timeout"


def test_summary_truncates_to_240_chars():
    s = _build_summary("flow", "x" * 500)
    assert len(s) == 240


def test_summary_empty_message_falls_back_to_flow_name():
    assert _build_summary("flow", "") == "flow"


# --- flow_doctor block --------------------------------------------

def test_flow_doctor_block_carries_provenance():
    notifier = S3Notifier(bucket="b", subsystem="executor")
    r = _report(error_signature="sig-xyz", dedup_count=3, cascade_source="upstream_flow")
    _, _, payload = _patched_send(notifier, r, diagnosis=_diagnosis())

    block = payload["flow_doctor"]
    assert block["flow_name"] == "test_flow"
    assert block["error_type"] == "RuntimeError"
    assert block["error_signature"] == "sig-xyz"
    assert block["dedup_count"] == 3
    assert block["cascade_source"] == "upstream_flow"
    assert block["diagnosis"]["category"] == "lambda_oom"
    assert block["diagnosis"]["confidence"] == 0.8


# --- failure / preflight paths -------------------------------------

def test_send_returns_none_when_boto3_missing():
    """If boto3 isn't importable (the optional [s3] extra wasn't installed),
    notifier.send must not crash the host process — return None and log."""
    notifier = S3Notifier(bucket="b", subsystem="executor")
    # Force ImportError by making `import boto3` fail
    with patch.dict(sys.modules, {"boto3": None}):
        target = notifier.send(_report(), "test_flow")
    assert target is None


def test_send_returns_none_when_put_object_raises():
    notifier = S3Notifier(bucket="b", subsystem="executor")

    class _BoomClient:
        def put_object(self, **_):
            raise RuntimeError("network blip")

    fake_boto3 = MagicMock()
    fake_boto3.client = MagicMock(return_value=_BoomClient())
    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        target = notifier.send(_report(), "test_flow")

    assert target is None


def test_validate_skips_when_env_set(monkeypatch):
    """FLOW_DOCTOR_SKIP_PREFLIGHT=1 short-circuits — useful for tests
    + air-gapped / offline contexts where head_bucket would error."""
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    S3Notifier(bucket="b", subsystem="executor").validate()  # no raise


# --- timestamp formatting ------------------------------------------

def test_format_iso_utc_naive_datetime():
    """datetime.utcnow() returns naive UTC; the formatter must coerce."""
    naive = datetime(2026, 5, 1, 13, 0, 0)
    assert _format_iso_utc(naive) == "2026-05-01T13:00:00Z"


def test_format_iso_utc_aware_datetime():
    aware = datetime(2026, 5, 1, 13, 0, 0, tzinfo=timezone.utc)
    assert _format_iso_utc(aware) == "2026-05-01T13:00:00Z"


# --- config wiring (end-to-end via FlowDoctor.init) ----------------

def test_init_dispatches_s3_notifier(monkeypatch):
    """Test the elif nc.type == 's3' branch in client.py builds an S3Notifier."""
    from flow_doctor import FlowDoctor
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    fd = FlowDoctor.from_config(
        notify=[{"type": "s3", "bucket": "test-bucket", "subsystem": "data_pipeline"}],
        store={"type": "sqlite", "path": ":memory:"},
        strict=False,
    )
    assert len(fd._notifiers) == 1
    assert isinstance(fd._notifiers[0], S3Notifier)
    assert fd._notifiers[0].bucket == "test-bucket"
    assert fd._notifiers[0].subsystem == "data_pipeline"


def test_init_rejects_s3_without_required_fields(monkeypatch):
    from flow_doctor import FlowDoctor
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    with pytest.raises(ConfigError) as exc:
        FlowDoctor.from_config(
            notify=[{"type": "s3"}],  # missing bucket + subsystem
            store={"type": "sqlite", "path": ":memory:"},
        )
    assert "bucket" in str(exc.value)
    assert "subsystem" in str(exc.value)


def test_init_picks_bucket_from_env(monkeypatch):
    from flow_doctor import FlowDoctor
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    monkeypatch.setenv("CHANGELOG_BUCKET", "env-bucket")
    fd = FlowDoctor.from_config(
        notify=[{"type": "s3", "subsystem": "executor"}],
        store={"type": "sqlite", "path": ":memory:"},
        strict=False,
    )
    assert fd._notifiers[0].bucket == "env-bucket"


# --- heartbeat emitter (config#646) ---------------------------------

class TestWriteHeartbeat:
    """The end-of-run status()->S3 heartbeat write primitive."""

    @staticmethod
    def _capture_put():
        """Return (mock boto3 client, captured-kwargs dict)."""
        captured: dict = {}
        client = MagicMock()
        client.put_object.side_effect = lambda **kw: captured.update(kw)
        return client, captured

    def test_writes_status_to_expected_key(self):
        status = {
            "healthy": True,
            "flow_name": "morning_enrich",
            "errors_seen_today": 3,
            "decisions_today": {"fired": 1, "deduped": 2},
        }
        client, captured = self._capture_put()
        with patch("boto3.client", return_value=client):
            uri = write_heartbeat(
                status,
                bucket="alpha-engine-research",
                flow_name="morning_enrich",
                date="2026-06-29",
            )

        assert uri == (
            "s3://alpha-engine-research/_flow_doctor/heartbeat/"
            "morning_enrich/2026-06-29.json"
        )
        assert captured["Bucket"] == "alpha-engine-research"
        assert captured["Key"] == "_flow_doctor/heartbeat/morning_enrich/2026-06-29.json"
        assert captured["ContentType"] == "application/json"
        body = json.loads(captured["Body"])
        assert body["schema_version"] == HEARTBEAT_SCHEMA_VERSION
        assert body["flow_name"] == "morning_enrich"
        assert body["source"] == "flow-doctor"
        assert body["status"] == status
        # ts_utc is the literal-Z schema format
        assert body["ts_utc"].endswith("Z")

    def test_default_prefix_constant(self):
        client, captured = self._capture_put()
        with patch("boto3.client", return_value=client):
            write_heartbeat({"x": 1}, bucket="b", flow_name="f", date="2026-06-29")
        assert captured["Key"].startswith(f"{HEARTBEAT_PREFIX}/")

    def test_custom_prefix_is_stripped_and_applied(self):
        client, captured = self._capture_put()
        with patch("boto3.client", return_value=client):
            write_heartbeat(
                {"x": 1},
                bucket="b",
                flow_name="f",
                prefix="/custom/hb/",
                date="2026-06-29",
            )
        assert captured["Key"] == "custom/hb/f/2026-06-29.json"

    def test_flow_name_sanitized_in_key(self):
        client, captured = self._capture_put()
        with patch("boto3.client", return_value=client):
            uri = write_heartbeat(
                {"x": 1},
                bucket="b",
                flow_name="weird/flow name:v2",
                date="2026-06-29",
            )
        assert captured["Key"] == "_flow_doctor/heartbeat/weird_flow_name_v2/2026-06-29.json"
        assert uri is not None

    def test_soft_fails_to_none_on_s3_error(self):
        client = MagicMock()
        client.put_object.side_effect = RuntimeError("AccessDenied")
        with patch("boto3.client", return_value=client):
            uri = write_heartbeat(
                {"x": 1}, bucket="b", flow_name="f", date="2026-06-29"
            )
        assert uri is None

    def test_soft_fails_when_boto3_missing(self):
        # Simulate boto3 not installed — the lazy import raises ImportError,
        # which the soft-fail wrapper must swallow (return None, not raise).
        with patch.dict(sys.modules, {"boto3": None}):
            uri = write_heartbeat(
                {"x": 1}, bucket="b", flow_name="f", date="2026-06-29"
            )
        assert uri is None

    def test_non_serializable_status_is_coerced_not_raised(self):
        # status() can carry e.g. datetimes; default=str must keep the
        # write from failing on payload shape.
        status = {"ts": datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)}
        client, captured = self._capture_put()
        with patch("boto3.client", return_value=client):
            uri = write_heartbeat(
                status, bucket="b", flow_name="f", date="2026-06-29"
            )
        assert uri is not None
        body = json.loads(captured["Body"])
        assert "2026-06-29" in body["status"]["ts"]

    def test_default_date_is_today_utc(self):
        client, captured = self._capture_put()
        with patch("boto3.client", return_value=client):
            write_heartbeat({"x": 1}, bucket="b", flow_name="f")
        expected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert captured["Key"] == f"_flow_doctor/heartbeat/f/{expected}.json"


class TestEmitHeartbeatClientMethod:
    """FlowDoctor.emit_heartbeat() delegates status() to write_heartbeat()."""

    def _make_fd(self, **kwargs):
        import tempfile

        from flow_doctor.core.client import FlowDoctor
        from flow_doctor.core.config import load_config

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        config = load_config(
            flow_name=kwargs.get("flow_name", "hb-flow"),
            store=f"sqlite://{f.name}",
        )
        return FlowDoctor(config)

    def test_delegates_status_to_write_heartbeat(self):
        fd = self._make_fd(flow_name="hb-flow")
        with patch("flow_doctor.notify.s3.write_heartbeat") as mock_write:
            mock_write.return_value = "s3://bucket/_flow_doctor/heartbeat/hb-flow/x.json"
            out = fd.emit_heartbeat("my-bucket")

        assert out == "s3://bucket/_flow_doctor/heartbeat/hb-flow/x.json"
        mock_write.assert_called_once()
        _, kwargs = mock_write.call_args
        assert kwargs["bucket"] == "my-bucket"
        assert kwargs["flow_name"] == "hb-flow"
        assert kwargs["prefix"] == HEARTBEAT_PREFIX
        # The positional payload is exactly status()
        assert mock_write.call_args.args[0] == fd.status()

    def test_custom_prefix_passed_through(self):
        fd = self._make_fd()
        with patch("flow_doctor.notify.s3.write_heartbeat") as mock_write:
            fd.emit_heartbeat("b", prefix="health/hb")
        assert mock_write.call_args.kwargs["prefix"] == "health/hb"

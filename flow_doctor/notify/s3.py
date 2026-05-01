"""S3 notification backend — writes flow-doctor reports as schema-1.0.0
structured entries to the system-wide changelog corpus.

Closes Gap 1 of the system-wide changelog event-mining coverage gaps
ROADMAP item: flow-doctor previously emitted only via Email/Slack/GitHub,
none of which route through the SNS-mirror Lambda, so its alerts (schema
validation errors, LLM API failures, agent timeouts, IB connectivity
issues) were invisible to the changelog corpus. With this notifier wired
into a caller's ``flow-doctor.yaml``, every reported flow failure lands
at ``s3://<bucket>/changelog/entries/{YYYY-MM-DD}/{event_id}.json``
alongside CI deploy entries + SNS-mirrored alarm entries.

Schema target: alpha-engine-config/changelog/vocab.yaml v1.0.0.

Caller config (in flow-doctor.yaml):

    notify:
      - type: s3
        bucket: alpha-engine-research
        subsystem: data_pipeline       # one of vocab.yaml subsystem values
        # Optional fields below — sensible defaults applied otherwise.
        entry_prefix: changelog/entries
        default_root_cause_category: code_bug
        default_resolution_type: null  # null on incidents — operator fills via follow-up

Why no separate ``service`` / ``hostname`` config: the flow-doctor caller
already identifies via ``flow_name`` per report, and ``socket.gethostname()``
covers the machine field for free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import Notifier

_logger = logging.getLogger("flow_doctor")

SCHEMA_VERSION = "1.0.0"

# flow_doctor.Severity → schema-1.0.0 severity mapping. The two scales
# overlap but aren't identical. flow-doctor has 3 levels (critical/error/
# warning); schema 1.0.0 has 5 (critical/high/medium/low/informational).
# critical → critical (system down, capital at risk)
# error    → high     (core feature down, manual intervention required)
# warning  → medium   (degraded path, workaround exists)
SEVERITY_MAP: Dict[str, str] = {
    "critical": "critical",
    "error": "high",
    "warning": "medium",
}


class S3Notifier(Notifier):
    """Send flow-doctor reports to the system-wide changelog S3 corpus.

    Writes each report as one schema-1.0.0 JSON entry at
    ``s3://{bucket}/{entry_prefix}/{YYYY-MM-DD}/{event_id}.json``.
    Uses the calling process's AWS credentials (Lambda execution role,
    EC2 instance role, or local CLI creds) — no auth config in the
    notifier itself.
    """

    def __init__(
        self,
        bucket: str,
        subsystem: str,
        *,
        entry_prefix: str = "changelog/entries",
        default_root_cause_category: str = "code_bug",
        default_resolution_type: Optional[str] = None,
    ):
        self.bucket = bucket
        self.subsystem = subsystem
        self.entry_prefix = entry_prefix.strip("/")
        self.default_root_cause_category = default_root_cause_category
        self.default_resolution_type = default_resolution_type

    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        try:
            entry = self._build_entry(report, flow_name, diagnosis)
            key = self._s3_key(entry)
            self._put_object(key, entry)
            target = f"s3://{self.bucket}/{key}"
            _logger.debug(
                "flow-doctor S3 entry written: bucket=%s key=%s flow=%s",
                self.bucket, key, flow_name,
            )
            return target
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "S3Notifier.send failed for flow=%s: %s: %s",
                flow_name, type(exc).__name__, exc,
            )
            return None

    def validate(self) -> None:
        """Cheap preflight: confirm boto3 importable + bucket reachable.

        Skips the network call entirely when ``FLOW_DOCTOR_SKIP_PREFLIGHT=1``
        is set in the environment — for test suites that construct
        notifiers without AWS credentials.
        """
        import os
        if os.environ.get("FLOW_DOCTOR_SKIP_PREFLIGHT") == "1":
            return
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(
                "S3Notifier requires boto3. Install with: "
                "pip install 'flow-doctor[s3]'"
            ) from e
        try:
            client = boto3.client("s3")
            client.head_bucket(Bucket=self.bucket)
        except Exception as e:  # noqa: BLE001
            # Soft-fail like GitHubNotifier on transient/network — don't
            # block app startup over an S3 hiccup. Hard-fail only on
            # missing-bucket or auth-revoked, which surface as
            # ClientError 403/404; we report and continue, matching the
            # GitHub notifier's strict-but-not-blocking pattern.
            _logger.warning(
                "S3Notifier preflight failed for bucket=%s: %s: %s",
                self.bucket, type(e).__name__, e,
            )

    # ----- internals --------------------------------------------------

    def _build_entry(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis],
    ) -> Dict[str, Any]:
        ts_utc = _format_iso_utc(report.created_at)
        summary = _build_summary(flow_name, report.error_message)
        description = _build_description(report)

        # When a diagnosis is attached, prefer its category for the
        # controlled-vocab root_cause_category — it's a richer signal
        # than the static default. Fall back to default_root_cause_category
        # when there's no diagnosis or the category isn't a vocab value.
        root_cause = self._resolve_root_cause(diagnosis)

        run_id = None
        if report.context and isinstance(report.context, dict):
            run_id = report.context.get("run_id")

        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": _event_id(ts_utc, flow_name, summary),
            "ts_utc": ts_utc,
            "event_type": "incident",
            "severity": SEVERITY_MAP.get(report.severity, "high"),
            "subsystem": self.subsystem,
            "root_cause_category": root_cause,
            "resolution_type": self.default_resolution_type,
            "started_at": None,
            "detected_at": ts_utc,
            "resolved_at": None,
            "verified_at": None,
            "summary": summary,
            "description": description,
            "resolution_notes": None,
            "actor": flow_name,
            "machine": socket.gethostname() or "",
            "source": "flow-doctor",
            "auto_emitted": True,
            "git_refs": [],
            "prompt_version": None,
            "run_id": run_id,
            "eval_run_ref": None,
            "flow_doctor": _build_flow_doctor_block(report, diagnosis),
        }

    def _resolve_root_cause(self, diagnosis: Optional[Diagnosis]) -> str:
        if diagnosis and diagnosis.category:
            mapped = _map_diagnosis_category(diagnosis.category)
            if mapped:
                return mapped
        return self.default_root_cause_category

    def _s3_key(self, entry: Dict[str, Any]) -> str:
        date = entry["ts_utc"][:10]
        return f"{self.entry_prefix}/{date}/{entry['event_id']}.json"

    def _put_object(self, key: str, entry: Dict[str, Any]) -> None:
        # Lazy import keeps boto3 a soft dep — flow-doctor's other notifiers
        # don't need it, so installs that don't use the s3 type don't need
        # to pull it in.
        import boto3
        client = boto3.client("s3")
        client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )


def _format_iso_utc(dt: datetime) -> str:
    """Return naive-or-aware datetime as `YYYY-MM-DDTHH:MM:SSZ`.

    flow-doctor's `Report.created_at` uses `datetime.utcnow()` which
    returns naive UTC — coerce to aware then format with a literal
    trailing Z so the schema's ISO-8601 UTC validator accepts it.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(ts_utc: str, flow_name: str, summary: str) -> str:
    """Schema-1.0.0 event_id matching the changelog-log + SNS-mirror pattern.

    Format: ``{ts_id}_{flow_name_safe}_{7-hex}``. The hash digests
    ``ts_utc|flow_name|summary`` (same fields all emitters use) so two
    paths emitting the same logical event would produce identical
    event_ids — useful for joins across the corpus.
    """
    ts_id = ts_utc.replace(":", "-").rstrip("Z")
    digest_input = f"{ts_utc}|{flow_name}|{summary}".encode()
    h = hashlib.sha1(digest_input).hexdigest()[:7]
    flow_safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in flow_name)
    return f"{ts_id}_{flow_safe}_{h}"


def _build_summary(flow_name: str, error_message: str) -> str:
    """Single-line ≤ 240-char summary that fits the schema constraint."""
    msg = (error_message or "").strip().splitlines()[0] if error_message else ""
    base = f"{flow_name}: {msg}" if msg else flow_name
    return base[:240]


def _build_description(report: Report) -> Optional[str]:
    """Full error context — message + traceback + logs concatenated."""
    parts = []
    if report.error_message:
        parts.append(report.error_message)
    if report.error_type:
        parts.append(f"\nerror_type: {report.error_type}")
    if report.traceback:
        parts.append(f"\nTraceback:\n{report.traceback}")
    if report.logs:
        parts.append(f"\nLogs:\n{report.logs}")
    if report.context:
        try:
            parts.append(f"\nContext: {json.dumps(report.context, default=str)}")
        except (TypeError, ValueError):
            parts.append(f"\nContext: {report.context!r}")
    text = "".join(parts).strip()
    return text or None


def _build_flow_doctor_block(
    report: Report, diagnosis: Optional[Diagnosis]
) -> Dict[str, Any]:
    """Nested provenance block — preserves the flow-doctor-specific fields
    that don't have first-class schema slots (error_signature, dedup_count,
    cascade_source, diagnosis details). Mirrors the SNS-mirror Lambda's
    `sns: {...}` block pattern."""
    block: Dict[str, Any] = {
        "flow_name": report.flow_name,
        "report_id": report.id,
        "error_type": report.error_type,
        "error_signature": report.error_signature,
        "dedup_count": report.dedup_count,
        "cascade_source": report.cascade_source,
    }
    if diagnosis:
        block["diagnosis"] = {
            "category": diagnosis.category,
            "root_cause": diagnosis.root_cause,
            "confidence": diagnosis.confidence,
            "remediation": diagnosis.remediation,
            "auto_fixable": diagnosis.auto_fixable,
            "llm_model": diagnosis.llm_model,
        }
    return block


# Diagnosis categories produced by the LLM diagnoser are free-form strings;
# this best-effort map normalizes the most common categories to the
# schema's controlled-vocab values. Anything unmapped falls back to the
# notifier's default_root_cause_category.
_DIAGNOSIS_CATEGORY_MAP: Dict[str, str] = {
    # data quality
    "data_quality": "data_quality",
    "stale_data": "data_quality",
    "missing_data": "data_quality",
    "schema_mismatch": "schema_evolution",
    "schema_drift": "schema_evolution",
    "validation_error": "schema_evolution",
    # infrastructure
    "infrastructure": "infrastructure_failure",
    "lambda_oom": "infrastructure_failure",
    "lambda_timeout": "infrastructure_failure",
    "network": "infrastructure_failure",
    "iam": "infrastructure_failure",
    # third-party
    "api_failure": "third_party_api",
    "rate_limit": "third_party_api",
    "anthropic_api": "third_party_api",
    "polygon_api": "third_party_api",
    "yfinance": "third_party_api",
    "ib_gateway": "third_party_api",
    # code / model
    "code_bug": "code_bug",
    "logic_error": "code_bug",
    "model_behavior": "model_behavior",
    "prompt_regression": "prompt_regression",
    # config
    "configuration": "configuration",
    "config_error": "configuration",
    "missing_env": "configuration",
}


def _map_diagnosis_category(category: str) -> Optional[str]:
    """Normalize a free-form diagnosis category to a schema vocab value.

    Returns None if no match — caller falls back to its configured default.
    """
    if not category:
        return None
    key = category.strip().lower().replace(" ", "_").replace("-", "_")
    return _DIAGNOSIS_CATEGORY_MAP.get(key)

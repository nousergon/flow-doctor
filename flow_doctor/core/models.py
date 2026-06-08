"""Data models for Flow Doctor reports, diagnoses, actions, and feedback."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


def _ulid() -> str:
    """Generate a ULID-like ID: timestamp prefix + random suffix for sortability."""
    ts = int(time.time() * 1000)
    ts_hex = format(ts, "012x")
    rand = uuid.uuid4().hex[:16]
    return f"{ts_hex}{rand}"


class Severity(str, Enum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    # Healthy-completion / success pings. Lower than WARNING — never
    # triggers diagnosis or remediation; only reaches notifiers that
    # explicitly opt in via ``notify_on`` (see NotifierConfig).
    INFO = "info"


class ActionType(str, Enum):
    SLACK_ALERT = "slack_alert"
    EMAIL_ALERT = "email_alert"
    GITHUB_ISSUE = "github_issue"
    GITHUB_PR = "github_pr"
    S3_ALERT = "s3_alert"
    TELEGRAM_ALERT = "telegram_alert"


class ActionStatus(str, Enum):
    SENT = "sent"
    FAILED = "failed"
    DEGRADED = "degraded"
    PR_OPEN = "pr_open"
    PR_MERGED = "pr_merged"
    PR_REJECTED = "pr_rejected"


@dataclass
class Report:
    flow_name: str
    error_message: str
    severity: str = Severity.ERROR.value
    error_type: Optional[str] = None
    traceback: Optional[str] = None
    logs: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    error_signature: Optional[str] = None
    dedup_count: int = 1
    cascade_source: Optional[str] = None
    id: str = field(default_factory=_ulid)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Diagnosis:
    report_id: str
    flow_name: str
    category: str
    root_cause: str
    confidence: float
    affected_files: Optional[List[str]] = None
    remediation: Optional[str] = None
    auto_fixable: Optional[bool] = None
    reasoning: Optional[str] = None
    alternative_hypotheses: Optional[List[str]] = None
    source: str = "llm"
    llm_model: Optional[str] = None
    tokens_used: Optional[int] = None
    cost_usd: Optional[float] = None
    id: str = field(default_factory=_ulid)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Action:
    report_id: str
    action_type: str
    status: str
    diagnosis_id: Optional[str] = None
    target: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    id: str = field(default_factory=_ulid)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Feedback:
    diagnosis_id: str
    correct: bool
    corrected_category: Optional[str] = None
    corrected_root_cause: Optional[str] = None
    notes: Optional[str] = None
    id: str = field(default_factory=_ulid)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class KnownPattern:
    error_signature: str
    category: str
    root_cause: str
    flow_name: Optional[str] = None
    resolution: Optional[str] = None
    auto_fixable: bool = False
    hit_count: int = 0
    last_seen: Optional[datetime] = None
    id: str = field(default_factory=_ulid)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class FixAttempt:
    diagnosis_id: str
    diff: str
    test_passed: Optional[bool] = None
    test_output: Optional[str] = None
    pr_url: Optional[str] = None
    pr_status: Optional[str] = None
    rejection_reason: Optional[str] = None
    id: str = field(default_factory=_ulid)
    created_at: datetime = field(default_factory=datetime.utcnow)

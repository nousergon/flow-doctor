"""Tests for DynamoDB storage backend (moto-backed)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from flow_doctor.core.models import Action, ActionStatus, ActionType, Report
from flow_doctor.storage.dynamodb import DynamoDBStorage

pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
mock_aws = moto.mock_aws


@pytest.fixture
def store():
    with mock_aws():
        s = DynamoDBStorage("flow-doctor-test", region="us-east-1")
        s.init_schema()
        yield s


def test_save_and_get_report(store):
    report = Report(
        flow_name="test",
        error_message="boom",
        severity="error",
        error_type="ValueError",
        error_signature="sig123",
        context={"key": "value"},
    )
    store.save_report(report)

    reports = store.get_reports(flow_name="test")
    assert len(reports) == 1
    assert reports[0].id == report.id
    assert reports[0].error_message == "boom"
    assert reports[0].context == {"key": "value"}


def test_find_report_by_signature(store):
    report = Report(
        flow_name="test",
        error_message="boom",
        error_signature="sig456",
    )
    store.save_report(report)

    found = store.find_report_by_signature(
        "sig456",
        since=datetime.utcnow() - timedelta(hours=1),
    )
    assert found is not None
    assert found.id == report.id


def test_increment_dedup_count(store):
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)

    store.increment_dedup_count(report.id)
    store.increment_dedup_count(report.id)

    saved = store.get_report(report.id)
    assert saved is not None
    assert saved.dedup_count == 3


def test_has_recent_failure(store):
    store.save_report(
        Report(
            flow_name="upstream",
            error_message="upstream broke",
            severity="error",
        )
    )

    assert store.has_recent_failure(
        "upstream", since=datetime.utcnow() - timedelta(hours=1)
    )
    assert not store.has_recent_failure(
        "other-flow", since=datetime.utcnow() - timedelta(hours=1)
    )


def test_has_recent_failure_warning_excluded(store):
    store.save_report(
        Report(
            flow_name="upstream",
            error_message="just a warning",
            severity="warning",
        )
    )

    assert not store.has_recent_failure(
        "upstream", since=datetime.utcnow() - timedelta(hours=1)
    )


def test_count_actions_today(store):
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)
    store.save_action(
        Action(
            report_id=report.id,
            action_type=ActionType.SLACK_ALERT.value,
            status=ActionStatus.SENT.value,
        )
    )

    assert store.count_actions_today(ActionType.SLACK_ALERT.value) == 1
    assert store.count_actions_today(ActionType.EMAIL_ALERT.value) == 0


def test_cross_instance_dedup_via_signature(store):
    """Two store handles on the same table share signature lookup."""
    report = Report(
        flow_name="lambda-a",
        error_message="same alert",
        error_signature="shared-sig",
    )
    store.save_report(report)

    found = store.find_report_by_signature(
        "shared-sig",
        since=datetime.utcnow() - timedelta(minutes=5),
    )
    assert found is not None
    assert found.id == report.id


def test_count_actions_today_is_scoped_by_flow(store):
    """The budget reads as per-component in every config file; the count behind
    it must agree. Five flows shared one `flow-doctor-store` table on
    2026-08-11, all drawing on one count (alpha-engine-config-I6921)."""
    from flow_doctor.core.models import Action

    for flow, n in (("executor", 3), ("research-lambda", 2)):
        for _ in range(n):
            store.save_action(
                Action(
                    report_id="r", action_type="email_alert",
                    status="sent", flow_name=flow,
                )
            )

    assert store.count_actions_today("email_alert", "executor") == 3
    assert store.count_actions_today("email_alert", "research-lambda") == 2
    # Unscoped keeps the old meaning for callers that have not been updated.
    assert store.count_actions_today("email_alert") == 5


def test_save_action_omits_an_absent_flow_name(store):
    """`flow_name` is a key attribute of SignatureIndex and DynamoDB rejects an
    empty string for an index key, so an `or ""` form raises ValidationException
    on every unscoped write. Absent must mean absent."""
    from flow_doctor.core.models import Action

    store.save_action(
        Action(report_id="r", action_type="email_alert", status="sent")
    )
    assert store.count_actions_today("email_alert") == 1
    # An unscoped row belongs to no flow's budget rather than to a "" flow.
    assert store.count_actions_today("email_alert", "executor") == 0


def test_count_actions_today_can_exclude_failed_deliveries(store):
    """alpha-engine-config-I12711: the rate limiter excludes FAILED rows
    server-side, which needs `status` written as a top-level attribute."""
    from flow_doctor.core.models import Action

    for status in ("failed", "failed", "sent", "degraded"):
        store.save_action(
            Action(
                report_id="r", action_type="telegram_alert",
                status=status, flow_name="executor",
            )
        )

    assert store.count_actions_today("telegram_alert", "executor") == 4
    assert store.count_actions_today(
        "telegram_alert", "executor", exclude_status="failed"
    ) == 2
    assert store.count_actions_today("telegram_alert", exclude_status="failed") == 2


def test_a_row_without_top_level_status_is_counted(store):
    """Rows written before `status` was top-level carry it only in `payload`.
    They are counted, so a legacy row can over-spend the budget for the day it
    was written but never let a failure hide inside the cap."""
    import json
    from datetime import datetime

    store._table.put_item(
        Item={
            "pk": "ACTION#legacy", "sk": "META", "entity": "action",
            "action_type": "telegram_alert", "flow_name": "executor",
            "created_at": datetime.utcnow().isoformat(),
            "payload": json.dumps({"status": "failed"}),
        }
    )
    assert store.count_actions_today(
        "telegram_alert", "executor", exclude_status="failed"
    ) == 1

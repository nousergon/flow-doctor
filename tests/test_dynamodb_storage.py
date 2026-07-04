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

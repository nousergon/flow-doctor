"""Consecutive-unavailable-diagnosis detector (alpha-engine-config-I7930).

I7789 deliverable 4: N diagnosis attempts in a row for one flow coming back
unavailable is a CRITICAL, not silence. The count is CONSECUTIVE, not a rate:
a usable diagnosis resets it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from flow_doctor import FlowDoctor
from flow_doctor.core.config import ConfigError, load_config
from flow_doctor.core.models import Action, ActionStatus, Diagnosis, Report
from flow_doctor.notify.base import Notifier
from flow_doctor.storage.base import StorageBackend
from flow_doctor.storage.sqlite import SQLiteStorage

FLOW = "streak-test"
STREAK_SUBJECT = "LLM diagnosis unavailable"
# Distinct words, not digits: signatures normalize digits, and a deduped
# report never reaches diagnosis.
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
         "hotel", "india", "juliet", "kilo", "lima"]


class _Recorder(Notifier):
    def __init__(self) -> None:
        self.reports: List[Report] = []

    def send(
        self, report: Report, flow_name: str, diagnosis: Optional[Diagnosis] = None
    ) -> Optional[str]:
        self.reports.append(report)
        return "recording:ok"

    def streak_alerts(self) -> List[Report]:
        return [r for r in self.reports if STREAK_SUBJECT in r.error_message]


class _Provider:
    """Diagnosis provider whose next outcomes are scripted: True = usable."""

    def __init__(self) -> None:
        self.script: List[bool] = []
        self.calls = 0

    def diagnose(self, context, assembler):
        self.calls += 1
        ok = self.script.pop(0)
        if not ok:
            raise RuntimeError("router: route_unconfigured (HTTP 400)")
        return Diagnosis(
            report_id="", flow_name=FLOW, category="code",
            root_cause="a real root cause", confidence=0.9,
        )


@pytest.fixture
def harness(tmp_path):
    fd = FlowDoctor.builder(FLOW).with_store(path=str(tmp_path / "fd.db")).build()
    rec = _Recorder()
    provider = _Provider()
    fd._notifiers = [rec]
    fd._knowledge_base = MagicMock(lookup=MagicMock(return_value=None))
    fd._diagnosis_provider = provider
    fd._context_assembler = MagicMock()
    # The fleet's diagnosis budget (3/day) would degrade the 4th attempt; these
    # tests drive the sequence of LLM outcomes, so the budget is lifted here
    # and exercised separately below.
    fd._rate_limiter = MagicMock(check=MagicMock(return_value="allow"))
    fd._load_git_context = lambda report=None: None
    words = iter(WORDS)

    def run(*outcomes: bool) -> None:
        for ok in outcomes:
            provider.script.append(ok)
            fd.report(ValueError(f"failure {next(words)}"))

    return fd, rec, run


def test_default_threshold_is_three(tmp_path):
    fd = FlowDoctor.builder(FLOW).with_store(path=str(tmp_path / "fd.db")).build()
    assert fd.config.diagnosis.unavailable_alert_after == 3


def test_n_consecutive_unavailable_fires_one_critical(harness):
    fd, rec, run = harness
    run(False, False, False)
    alerts = rec.streak_alerts()
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert FLOW in alerts[0].error_message
    assert "route_unconfigured" in (alerts[0].logs or "")


def test_n_minus_one_does_not_fire(harness):
    fd, rec, run = harness
    run(False, False)
    assert rec.streak_alerts() == []


def test_success_resets_the_count(harness):
    fd, rec, run = harness
    run(False, False, True, False, False)
    assert rec.streak_alerts() == []
    run(False)  # third in a row since the reset
    assert len(rec.streak_alerts()) == 1


def test_non_consecutive_mix_never_fires(harness):
    fd, rec, run = harness
    run(False, True, False, False, True, False, True, False, False)
    assert rec.streak_alerts() == []


def test_continuing_run_does_not_repage_same_day(harness):
    fd, rec, run = harness
    run(False, False, False, False, False)
    assert len(rec.streak_alerts()) == 1


def test_second_distinct_run_fires_again(harness):
    fd, rec, run = harness
    run(False, False, False, True, False, False, False)
    assert len(rec.streak_alerts()) == 2


def test_run_still_open_repages_on_first_attempt_of_a_new_day(harness):
    """A persisting outage re-pages daily rather than paging once forever."""
    fd, rec, run = harness
    store = fd._store
    yesterday = datetime.utcnow() - timedelta(days=1)
    for _ in range(4):
        store.save_action(Action(report_id=None,
            flow_name=FLOW, action_type="diagnosis",
            status=ActionStatus.FAILED.value, created_at=yesterday,
        ))
    run(False)  # 5th in a row, first attempt today
    assert len(rec.streak_alerts()) == 1
    assert "at least 3" in rec.streak_alerts()[0].error_message
    run(False)  # 6th, second today: no repeat
    assert len(rec.streak_alerts()) == 1


def test_degraded_attempts_neither_extend_nor_break_the_run(harness):
    fd, rec, run = harness
    run(False, False)
    fd._store.save_action(Action(report_id=None,
        flow_name=FLOW, action_type="diagnosis",
        status=ActionStatus.DEGRADED.value, target="degraded - rate limited",
    ))
    assert rec.streak_alerts() == []
    run(False)
    assert len(rec.streak_alerts()) == 1


def test_other_flows_attempts_do_not_count(harness):
    fd, rec, run = harness
    for _ in range(3):
        fd._store.save_action(Action(report_id=None,
            flow_name="another-flow", action_type="diagnosis",
            status=ActionStatus.FAILED.value,
        ))
    run(False, False)
    assert rec.streak_alerts() == []


def test_alert_does_not_re_enter_diagnosis(harness):
    """The alert about a dead diagnosis path must not call that path again."""
    fd, rec, run = harness
    run(False, False, False)
    assert fd._diagnosis_provider.calls == 3


def test_zero_disables(harness):
    fd, rec, run = harness
    fd.config.diagnosis.unavailable_alert_after = 0
    run(False, False, False, False)
    assert rec.streak_alerts() == []


def test_custom_threshold(harness):
    fd, rec, run = harness
    fd.config.diagnosis.unavailable_alert_after = 2
    run(False, False)
    assert len(rec.streak_alerts()) == 1


def test_with_the_real_daily_budget_the_third_failure_pages(tmp_path):
    """N=3 matches max_diagnosed_per_day=3: the day's whole budget failing pages."""
    fd = FlowDoctor.builder(FLOW).with_store(path=str(tmp_path / "fd.db")).build()
    assert fd._rate_limiter is not None
    fd._rate_limiter.limits["diagnosis"] = 3
    rec = _Recorder()
    provider = _Provider()
    fd._notifiers = [rec]
    fd._knowledge_base = MagicMock(lookup=MagicMock(return_value=None))
    fd._diagnosis_provider = provider
    fd._context_assembler = MagicMock()
    fd._load_git_context = lambda report=None: None
    for word in WORDS[:5]:
        provider.script.append(False)
        fd.report(ValueError(f"failure {word}"))
    assert provider.calls == 3  # 4th and 5th degraded by the budget
    assert len(rec.streak_alerts()) == 1


def test_backend_without_support_is_inert_and_says_so(harness, caplog):
    fd, rec, run = harness
    fd._store.recent_diagnosis_outcomes = lambda flow, limit: None
    with caplog.at_level("WARNING", logger="flow_doctor"):
        run(False, False, False)
    assert rec.streak_alerts() == []
    assert "detector is inert" in caplog.text


def test_store_error_never_breaks_reporting(harness):
    fd, rec, run = harness

    def boom(flow, limit):
        raise RuntimeError("store down")

    fd._store.recent_diagnosis_outcomes = boom
    run(False, False, False)
    assert rec.streak_alerts() == []
    assert len(rec.reports) == 3


def test_base_backend_default_is_none():
    assert StorageBackend.recent_diagnosis_outcomes(MagicMock(), FLOW, 3) is None


# --- storage ---------------------------------------------------------------


def _seed(store, statuses, flow=FLOW, start=None):
    start = start or datetime.utcnow() - timedelta(minutes=len(statuses))
    for i, status in enumerate(statuses):
        store.save_action(Action(report_id=None,
            flow_name=flow, action_type="diagnosis", status=status,
            created_at=start + timedelta(seconds=i),
        ))


def test_sqlite_outcomes_newest_first_and_filtered(tmp_path):
    store = SQLiteStorage(str(tmp_path / "s.db"))
    store.init_schema()
    _seed(store, ["sent", "failed", "degraded", "failed"])
    _seed(store, ["sent"], flow="other")
    store.save_action(Action(
        report_id=None, flow_name=FLOW, action_type="telegram_alert", status="failed",
    ))
    assert store.recent_diagnosis_outcomes(FLOW, 10) == ["failed", "failed", "sent"]
    assert store.recent_diagnosis_outcomes(FLOW, 2) == ["failed", "failed"]


def test_dynamodb_outcomes_newest_first_and_filtered():
    pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from flow_doctor.storage.dynamodb import DynamoDBStorage

    with moto.mock_aws():
        store = DynamoDBStorage("flow-doctor-test", region="us-east-1")
        store.init_schema()
        _seed(store, ["sent", "failed", "degraded", "failed"])
        _seed(store, ["sent"], flow="other")
        store.save_report(Report(flow_name=FLOW, error_message="x", severity="error"))
        assert store.recent_diagnosis_outcomes(FLOW, 10) == ["failed", "failed", "sent"]
        assert store.recent_diagnosis_outcomes(FLOW, 2) == ["failed", "failed"]


def test_config_parses_and_rejects_negative(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text(
        "flow_name: x\nrepo: o/r\ndiagnosis:\n  enabled: false\n"
        "  unavailable_alert_after: 5\n"
    )
    assert load_config(str(good)).diagnosis.unavailable_alert_after == 5
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "flow_name: x\nrepo: o/r\ndiagnosis:\n  enabled: false\n"
        "  unavailable_alert_after: -1\n"
    )
    with pytest.raises(ConfigError):
        load_config(str(bad))

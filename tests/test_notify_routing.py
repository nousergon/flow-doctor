"""0.6.0 notification routing: per-notifier severity filter, notify_success
healthy-completion API, and the GitHub auto-issue / auto-fix-PR toggles.
0.8.0 adds per-notifier diagnosis-category routing (notify_on_category) and
an auto_fix_pr cross-repo misconfiguration warning."""

from __future__ import annotations

import json
import logging
import tempfile
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from flow_doctor import (
    FlowDoctor,
    GitHubNotifierConfig,
    TelegramNotifierConfig,
)
from flow_doctor.core.client import _classify_dispatch
from flow_doctor.core.models import DecisionReason, Diagnosis, Report
from flow_doctor.notify.base import Notifier
from flow_doctor.notify.github import GitHubNotifier


class _RecordingNotifier(Notifier):
    """Captures the severity of every report it is asked to send."""

    def __init__(self) -> None:
        self.received: List[str] = []
        self.bodies: List[str] = []

    def send(
        self, report: Report, flow_name: str, diagnosis: Optional[Diagnosis] = None
    ) -> Optional[str]:
        self.received.append(report.severity)
        self.bodies.append(report.error_message)
        return "recording:ok"


@pytest.fixture
def fd():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        yield FlowDoctor.builder("routing-test").with_store(path=f.name).build()


# --- per-notifier severity routing -------------------------------------


def test_default_routing_is_critical_and_error_only(fd):
    n = _RecordingNotifier()
    fd._notifiers = [n]
    fd.report(ValueError("boom"))                 # error -> delivered
    fd.report("a warning", severity="warning")    # warning -> skipped
    fd.notify_success("all good")                 # info -> skipped
    assert n.received == ["error"]


def test_notify_on_info_receives_success_ping(fd):
    n = _RecordingNotifier()
    n.notify_on = {"critical", "error", "info"}
    fd._notifiers = [n]
    rid = fd.notify_success("nightly done", body="42 rows")
    assert rid is not None
    assert n.received == ["info"]
    assert n.bodies == ["nightly done"]


def test_notify_on_warning_only_routes_warnings_not_errors(fd):
    n = _RecordingNotifier()
    n.notify_on = {"warning"}
    fd._notifiers = [n]
    fd.report(ValueError("err"))                  # error -> skipped
    fd.report("just a warning", severity="warning")  # warning -> delivered
    assert n.received == ["warning"]


def test_notify_success_persists_an_info_report(fd):
    n = _RecordingNotifier()
    n.notify_on = {"info"}
    fd._notifiers = [n]
    rid = fd.notify_success("done")
    saved = fd._store.get_reports(flow_name="routing-test", limit=5)
    assert any(r.id == rid and r.severity == "info" for r in saved)


def test_config_notify_on_flows_through_to_notifier_instance():
    """notify_on declared on a typed config must land on the built
    notifier instance so the dispatcher can route by it."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        built = (
            FlowDoctor.builder("cfg-test")
            .with_store(path=f.name)
            .add_notifier(
                TelegramNotifierConfig(
                    bot_token="x", chat_id=1, notify_on=["critical", "error", "info"]
                )
            )
            .build()
        )
    assert built._notifiers[0].notify_on == {"critical", "error", "info"}


# --- per-notifier category routing (0.8.0) ------------------------------


def _diagnosis(category: str) -> Diagnosis:
    return Diagnosis(
        report_id="r1", flow_name="cat-test", category=category,
        root_cause="because", confidence=0.9,
    )


def test_category_gate_unset_fires_regardless_of_diagnosis_category(fd):
    """No notify_on_category configured -> every category reaches the
    notifier, preserving pre-0.8.0 behavior."""
    n = _RecordingNotifier()
    fd._notifiers = [n]
    fd._send_notifications(
        Report(flow_name="f", error_message="e", severity="error"),
        is_cascade=False, diagnosis=_diagnosis("TRANSIENT"),
    )
    assert n.received == ["error"]


def test_category_gate_skips_non_matching_category(fd):
    n = _RecordingNotifier()
    n.notify_on_category = {"CODE", "CONFIG"}
    fd._notifiers = [n]
    fd._send_notifications(
        Report(flow_name="f", error_message="e", severity="error"),
        is_cascade=False, diagnosis=_diagnosis("TRANSIENT"),
    )
    assert n.received == []


def test_category_gate_fires_on_matching_category_case_insensitive(fd):
    n = _RecordingNotifier()
    n.notify_on_category = {"CODE", "CONFIG"}
    fd._notifiers = [n]
    fd._send_notifications(
        Report(flow_name="f", error_message="e", severity="error"),
        is_cascade=False, diagnosis=_diagnosis("code"),  # lower-case from a provider
    )
    assert n.received == ["error"]


def test_category_gate_skipped_when_diagnosis_unavailable(fd):
    """A notifier scoped to CODE/CONFIG must still fire when no diagnosis
    ran (feature disabled, or the diagnosis call itself failed) — an
    unavailable enrichment must never silently blank a channel."""
    n = _RecordingNotifier()
    n.notify_on_category = {"CODE", "CONFIG"}
    fd._notifiers = [n]
    fd._send_notifications(
        Report(flow_name="f", error_message="e", severity="error"),
        is_cascade=False, diagnosis=None,
    )
    assert n.received == ["error"]


def test_config_notify_on_category_flows_through_to_notifier_instance():
    """notify_on_category declared on a typed config must land on the built
    notifier instance, normalized to an upper-cased set."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        built = (
            FlowDoctor.builder("cat-cfg-test")
            .with_store(path=f.name)
            .add_notifier(
                GitHubNotifierConfig(
                    repo="o/r", token="t", notify_on_category=["code", " Config "]
                )
            )
            .build()
        )
    assert built._notifiers[0].notify_on_category == {"CODE", "CONFIG"}


def test_category_filtered_is_the_decision_reason_when_only_category_skips():
    """_classify_dispatch must distinguish category_filtered from
    severity_filtered/no_notifiers so the daily heartbeat stays legible."""
    assert (
        _classify_dispatch({"category_skipped": 1})
        == DecisionReason.CATEGORY_FILTERED.value
    )


# --- auto_fix_pr cross-repo misconfiguration warning (0.8.0) ------------


def test_auto_fix_pr_same_repo_as_app_repo_warns_nothing(monkeypatch, caplog):
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        with caplog.at_level(logging.WARNING, logger="flow_doctor"):
            (
                FlowDoctor.builder("same-repo-test")
                .with_repo("o/r")
                .with_store(path=f.name)
                .add_notifier(
                    GitHubNotifierConfig(repo="o/r", token="t", auto_fix_pr=True)
                )
                .build()
            )
    assert not any(
        "auto_fix_pr=True with repo=" in r.getMessage() for r in caplog.records
    )


def test_auto_fix_pr_different_repo_warns(monkeypatch, caplog):
    """auto_fix_pr=True with a github repo that differs from the app's own
    top-level ``repo`` must warn — the flow-doctor-fix Actions workflow can
    only trigger same-repo, so redirecting the issue silently drops auto-fix."""
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        with caplog.at_level(logging.WARNING, logger="flow_doctor"):
            (
                FlowDoctor.builder("cross-repo-test")
                .with_repo("myorg/source-repo")
                .with_store(path=f.name)
                .add_notifier(
                    GitHubNotifierConfig(
                        repo="myorg/central-backlog", token="t", auto_fix_pr=True,
                    )
                )
                .build()
            )
    warnings = [
        r.getMessage() for r in caplog.records
        if "auto_fix_pr=True with repo=" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "myorg/central-backlog" in warnings[0]
    assert "myorg/source-repo" in warnings[0]


# --- auto-issue toggle --------------------------------------------------


def test_auto_create_issue_false_skips_github_notifier():
    """A github notifier with auto_create_issue=False is skipped at init —
    no issue is filed, and (because it's skipped before the field checks)
    it doesn't even require repo/token."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        built = (
            FlowDoctor.builder("toggle-test")
            .with_store(path=f.name)
            .add_notifier(GitHubNotifierConfig(auto_create_issue=False))
            .build()
        )
    assert not any(isinstance(n, GitHubNotifier) for n in built._notifiers)


# --- auto-fix-PR toggle -------------------------------------------------


def _mock_resp(status, body):
    m = MagicMock()
    m.status = status
    m.read.return_value = json.dumps(body).encode("utf-8")
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: None
    return m


def _report():
    return Report(
        flow_name="f", error_message="boom", error_type="RuntimeError",
        severity="error",
    )


def test_auto_fix_pr_applies_label_after_issue_creation():
    notifier = GitHubNotifier(repo="o/r", token="t", auto_fix_pr=True)
    create = _mock_resp(201, {"html_url": "https://github.com/o/r/issues/42", "number": 42})
    label = _mock_resp(200, [{"name": "flow-doctor:fix"}])

    with patch(
        "flow_doctor.notify.github.urlopen", side_effect=[create, label]
    ) as mock_url:
        result = notifier.send(_report(), "f")

    assert result == "https://github.com/o/r/issues/42"
    # Two calls: create issue, then apply the fix label.
    assert mock_url.call_count == 2
    label_req = mock_url.call_args_list[1][0][0]
    assert label_req.full_url.endswith("/issues/42/labels")
    assert json.loads(label_req.data)["labels"] == ["flow-doctor:fix"]


def test_no_auto_fix_pr_means_no_label_call():
    notifier = GitHubNotifier(repo="o/r", token="t")  # auto_fix_pr default False
    create = _mock_resp(201, {"html_url": "https://github.com/o/r/issues/7", "number": 7})

    with patch(
        "flow_doctor.notify.github.urlopen", side_effect=[create]
    ) as mock_url:
        result = notifier.send(_report(), "f")

    assert result == "https://github.com/o/r/issues/7"
    assert mock_url.call_count == 1  # issue creation only, no label POST


def test_label_failure_does_not_flip_issue_success():
    """A labeling failure is best-effort — the issue creation success the
    operator already sees must stand."""
    notifier = GitHubNotifier(repo="o/r", token="t", auto_fix_pr=True)
    create = _mock_resp(201, {"html_url": "https://github.com/o/r/issues/9", "number": 9})

    def _side_effect(req, *a, **k):
        if req.full_url.endswith("/labels"):
            raise Exception("label API down")
        return create

    with patch("flow_doctor.notify.github.urlopen", side_effect=_side_effect):
        result = notifier.send(_report(), "f")

    assert result == "https://github.com/o/r/issues/9"

"""Tests for GitHub issue notifier."""

import json
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.github import GitHubNotifier


def _make_report(**kwargs):
    defaults = dict(
        flow_name="test-flow",
        error_message="Something failed",
        error_type="RuntimeError",
        severity="error",
        traceback="Traceback (most recent call last):\n  File 'main.py', line 5\nRuntimeError: fail",
    )
    defaults.update(kwargs)
    return Report(**defaults)


def _make_diagnosis(**kwargs):
    defaults = dict(
        report_id="r1",
        flow_name="test-flow",
        category="CODE",
        root_cause="Logic error in main loop",
        confidence=0.85,
        remediation="Fix the loop condition",
        affected_files=["main.py:5"],
        auto_fixable=True,
        alternative_hypotheses=["Could be race condition"],
    )
    defaults.update(kwargs)
    return Diagnosis(**defaults)


def test_format_title_with_diagnosis():
    report = _make_report()
    diagnosis = _make_diagnosis()
    title = GitHubNotifier._format_title(report, "test-flow", diagnosis)
    assert title == "[CODE] test-flow: RuntimeError"


def test_format_title_without_diagnosis():
    report = _make_report()
    title = GitHubNotifier._format_title(report, "test-flow")
    assert title == "[ERROR] test-flow: RuntimeError"


def test_format_title_no_error_type():
    report = _make_report(error_type=None)
    title = GitHubNotifier._format_title(report, "test-flow")
    assert "Something failed" in title


def test_format_body_with_diagnosis():
    report = _make_report()
    diagnosis = _make_diagnosis()
    body = GitHubNotifier._format_body(report, "test-flow", diagnosis)

    assert "## Diagnosis" in body
    assert "CODE" in body
    assert "85%" in body
    assert "Logic error in main loop" in body
    assert "Fix the loop condition" in body
    assert "`main.py:5`" in body
    assert "race condition" in body
    assert "Auto-fixable:** Yes" in body


def test_format_body_without_diagnosis():
    report = _make_report()
    body = GitHubNotifier._format_body(report, "test-flow")

    assert "## Error" in body
    assert "RuntimeError" in body
    assert "## Traceback" in body
    assert "## Diagnosis" not in body


def test_format_title_with_diagnosis_error():
    """alpha-engine-config-I7789: a failed diagnosis attempt is loud, not
    indistinguishable from diagnosis being disabled."""
    report = _make_report(diagnosis_error="APIError: 400 Bad Request")
    title = GitHubNotifier._format_title(report, "test-flow")
    assert title == "[DIAGNOSIS UNAVAILABLE] test-flow: RuntimeError"


def test_format_body_with_diagnosis_error():
    report = _make_report(
        diagnosis_error="Error code: 400 - credit balance is too low"
    )
    body = GitHubNotifier._format_body(report, "test-flow")

    assert "## Diagnosis" in body
    assert "Unavailable" in body
    assert "credit balance is too low" in body


def test_format_body_diagnosis_error_ignored_when_real_diagnosis_present():
    """A real diagnosis always wins — diagnosis_error is a fallback signal
    for when there is nothing else to show, never a competing section."""
    report = _make_report(diagnosis_error="stale error from a prior attempt")
    diagnosis = _make_diagnosis()
    body = GitHubNotifier._format_body(report, "test-flow", diagnosis)

    assert "Logic error in main loop" in body
    assert "stale error from a prior attempt" not in body


def test_format_body_cascade():
    report = _make_report(cascade_source="upstream-flow")
    body = GitHubNotifier._format_body(report, "test-flow")
    assert "upstream-flow" in body


def test_format_body_with_logs():
    report = _make_report(logs="INFO: Starting\nERROR: Crashed\nDEBUG: cleanup")
    body = GitHubNotifier._format_body(report, "test-flow")
    assert "Captured Logs" in body
    assert "Crashed" in body


def test_send_success():
    notifier = GitHubNotifier(repo="owner/repo", token="test-token")
    report = _make_report()

    mock_resp = MagicMock()
    mock_resp.status = 201
    mock_resp.read.return_value = json.dumps(
        {"html_url": "https://github.com/owner/repo/issues/42"}
    ).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("flow_doctor.notify.github.urlopen", return_value=mock_resp) as mock_url:
        result = notifier.send(report, "test-flow")

    # send() now returns the issue URL on success (Optional[str] contract)
    assert result == "https://github.com/owner/repo/issues/42"
    call_args = mock_url.call_args
    req = call_args[0][0]
    assert "owner/repo" in req.full_url
    payload = json.loads(req.data)
    assert "flow-doctor" in payload["labels"]


def test_send_failure():
    notifier = GitHubNotifier(repo="owner/repo", token="test-token")
    report = _make_report()

    with patch("flow_doctor.notify.github.urlopen", side_effect=Exception("API error")):
        result = notifier.send(report, "test-flow")

    # send() returns None on failure (Optional[str] contract)
    assert result is None


def _fake_response(status, body_obj=None):
    resp = MagicMock()
    resp.status = status
    if body_obj is not None:
        resp.read.return_value = json.dumps(body_obj).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    return resp


def test_send_collapses_wrapper_onto_payload_same_process():
    """alpha-engine-config-I10350 case (a): two findings from the same
    report a fraction of a second apart, one text a superstring of the
    other, collapse to ONE filing — the measured `I8305`/`I8306` pair
    (payload + `RuntimeError:` wrapper, 229us apart)."""
    notifier = GitHubNotifier(repo="owner/repo", token="test-token")

    payload_report = _make_report(
        error_type=None,
        error_message=(
            "[reconcile_audit] correction FAILED - EOD P&L integrity "
            "gate failed"
        ),
    )
    wrapper_report = _make_report(
        error_type="RuntimeError",
        error_message="EOD P&L integrity gate failed",
    )

    search_resp = _fake_response(200, {"items": []})
    create_resp = _fake_response(
        201, {"html_url": "https://github.com/owner/repo/issues/8305", "number": 8305}
    )

    def fake_urlopen(req, timeout=15):
        if req.get_method() == "GET":
            return search_resp
        return create_resp

    with patch("flow_doctor.notify.github.urlopen", side_effect=fake_urlopen) as mock_url:
        first = notifier.send(payload_report, "executor")
        second = notifier.send(wrapper_report, "executor")

    assert first == "https://github.com/owner/repo/issues/8305"
    assert second == first
    # Only the payload's send() touches the network (search + create);
    # the wrapper collapses in-memory before ever calling urlopen.
    assert mock_url.call_count == 2


def test_send_recurrence_comments_and_bumps_occurrences_no_new_issue():
    """alpha-engine-config-I10350 case (b): a fingerprint already open on
    the tracker produces a recurrence comment and increments Occurrences
    — no new issue is filed (the `NAV MARK CORRECTION` shape: I9779,
    I9872, I9947 for one recurring condition)."""
    notifier = GitHubNotifier(repo="owner/repo", token="test-token")
    report = _make_report(
        error_type="RuntimeError", error_message="NAV MARK CORRECTION applied"
    )

    existing_body = (
        "**Occurrences:** 1 (first 2026-09-01T00:00:00+00:00, "
        "latest 2026-09-01T00:00:00+00:00)\n"
        "\n<!-- flow-doctor-fingerprint: abc123 -->"
    )
    search_resp = _fake_response(
        200,
        {
            "items": [
                {
                    "number": 9779,
                    "html_url": "https://github.com/owner/repo/issues/9779",
                    "body": existing_body,
                }
            ]
        },
    )
    comment_resp = _fake_response(201)
    patch_resp = _fake_response(200)

    def fake_urlopen(req, timeout=15):
        method = req.get_method()
        if method == "GET":
            return search_resp
        if method == "POST":
            return comment_resp
        if method == "PATCH":
            return patch_resp
        raise AssertionError(f"unexpected method {method}")

    with patch("flow_doctor.notify.github.urlopen", side_effect=fake_urlopen) as mock_url:
        result = notifier.send(report, "predictor")

    assert result == "https://github.com/owner/repo/issues/9779"
    methods = [c[0][0].get_method() for c in mock_url.call_args_list]
    assert methods == ["GET", "POST", "PATCH"]
    # No POST to the issue-creation endpoint — only the comment POST.
    create_calls = [
        c for c in mock_url.call_args_list
        if c[0][0].get_method() == "POST" and c[0][0].full_url.endswith("/issues")
    ]
    assert create_calls == []
    # The PATCH body carries the bumped occurrence count.
    patch_call = [c for c in mock_url.call_args_list if c[0][0].get_method() == "PATCH"][0]
    patched_body = json.loads(patch_call[0][0].data)["body"]
    assert "**Occurrences:** 2" in patched_body
    assert "first 2026-09-01T00:00:00+00:00" in patched_body


def test_send_files_normally_when_fingerprint_search_fails():
    """A degraded/failing tracker search must never suppress a real alert
    — losing an alert is worse than a possible duplicate
    (alpha-engine-config-I10350)."""
    notifier = GitHubNotifier(repo="owner/repo", token="test-token")
    report = _make_report()

    create_resp = _fake_response(
        201, {"html_url": "https://github.com/owner/repo/issues/42", "number": 42}
    )

    def fake_urlopen(req, timeout=15):
        if req.get_method() == "GET":
            raise URLError("boom")
        return create_resp

    with patch("flow_doctor.notify.github.urlopen", side_effect=fake_urlopen):
        result = notifier.send(report, "test-flow")

    assert result == "https://github.com/owner/repo/issues/42"


def test_format_body_embeds_fingerprint_marker_without_diagnosis():
    """The fingerprint marker must be present even with no diagnosis —
    the measured duplicate pairs had none, and the cross-session search
    depends on every issue carrying it."""
    report = _make_report()
    body = GitHubNotifier._format_body(report, "test-flow", fingerprint="deadbeef01234567")
    assert "flow-doctor-fingerprint: deadbeef01234567" in body


def test_format_body_includes_visible_occurrences_line():
    report = _make_report()
    body = GitHubNotifier._format_body(report, "test-flow")
    assert "**Occurrences:** 1 (first " in body

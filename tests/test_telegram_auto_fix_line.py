"""Every flow-doctor Telegram alert carries an ``Auto-fix:`` line.

Rule: nous-ergon-ops observability-policy.md § Notification — auto-fix line
(Brian, 2026-10-23).
"""

from __future__ import annotations

from types import SimpleNamespace

from flow_doctor.core.client import _describe_auto_fix
from flow_doctor.core.models import Report
from flow_doctor.notify import telegram as tgmod
from flow_doctor.notify.telegram import TelegramNotifier


def _report(**kw):
    base = dict(flow_name="f", error_type="ValueError", error_message="boom", severity="error")
    base.update(kw)
    return Report(**base)


def test_format_message_always_ends_with_auto_fix_and_resolution_lines():
    lines = TelegramNotifier._format_message(_report(), "f").splitlines()
    assert lines[-2] == f"*Auto-fix:* {tgmod.DEFAULT_AUTO_FIX}"
    assert lines[-1] == f"*Resolution:* {tgmod.DEFAULT_RESOLUTION}"


def test_format_message_uses_the_described_wiring():
    text = TelegramNotifier._format_message(
        _report(), "f", auto_fix="Filed as issue X.", resolution="issue X"
    )
    assert text.endswith("*Auto-fix:* Filed as issue X.\n*Resolution:* issue X")


def test_long_alert_keeps_the_lines():
    long = _report(logs="\n".join("l" * 200 for _ in range(100)), traceback="t\n" * 50)
    text = TelegramNotifier._format_message(long, "f", auto_fix="Filed.", resolution="R.")
    out = tgmod._truncate_keeping_auto_fix(text)
    assert len(out) <= tgmod._MAX_MESSAGE_LEN
    assert out.endswith("*Auto-fix:* Filed.\n*Resolution:* R.")


def test_default_resolution_never_invents_an_issue_number():
    assert "#" not in tgmod.DEFAULT_RESOLUTION


def test_resolution_for_issue_names_the_url():
    url = "https://github.com/nousergon/alpha-engine-config/issues/123"
    assert tgmod.resolution_for_issue(url).startswith(url)


def test_filed_issue_becomes_the_telegram_resolution():
    """Issue-filing notifiers dispatch first; the issue they file becomes the
    Telegram alert's Resolution line, even when Telegram is listed first."""
    import tempfile
    from typing import Optional

    from flow_doctor import FlowDoctor
    from flow_doctor.core import client as client_mod
    from flow_doctor.notify.base import Notifier

    url = "https://github.com/nousergon/alpha-engine-config/issues/77"
    order = []

    class FakeTelegram(Notifier):
        def __init__(self):
            self.resolution: Optional[str] = None
            self.seen: Optional[str] = "unset"

        def send(self, report, flow_name, diagnosis=None):
            order.append("telegram")
            self.seen = self.resolution
            return "telegram:1"

    class FakeGitHub(Notifier):
        auto_fix_pr = False

        def send(self, report, flow_name, diagnosis=None):
            order.append("github")
            return url

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = FlowDoctor.builder("resolution-test").with_store(path=f.name).build()
        tg, gh = FakeTelegram(), FakeGitHub()
        fd._notifiers = [tg, gh]
        fd.report(ValueError("boom"))

    assert order == ["github", "telegram"]
    assert tg.seen == tgmod.resolution_for_issue(url)
    assert client_mod._ISSUE_URL_RE.match(url)
    assert not client_mod._ISSUE_URL_RE.match("https://github.com/o/r/issues")


def test_no_issue_filed_leaves_the_default_resolution():
    import tempfile
    from typing import Optional

    from flow_doctor import FlowDoctor
    from flow_doctor.notify.base import Notifier

    class FakeTelegram(Notifier):
        def __init__(self):
            self.resolution: Optional[str] = "stale from a previous report"
            self.seen: Optional[str] = "unset"

        def send(self, report, flow_name, diagnosis=None):
            self.seen = self.resolution
            return "telegram:1"

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = FlowDoctor.builder("resolution-test2").with_store(path=f.name).build()
        tg = FakeTelegram()
        fd._notifiers = [tg]
        fd.report(ValueError("boom"))

    assert tg.seen is None  # renders DEFAULT_RESOLUTION


def test_describe_names_issue_repo_fix_pr_and_sweep():
    gh = SimpleNamespace(repo="nousergon/alpha-engine-config", labels=["flow-doctor"], auto_fix_pr=True)
    cfg = SimpleNamespace(remediation=SimpleNamespace(enabled=False, dry_run=True))
    text = _describe_auto_fix(cfg, [gh])
    assert "GitHub issue in nousergon/alpha-engine-config" in text
    assert "fix PR is drafted automatically" in text
    assert "nightly sweep" in text


def test_describe_without_issue_filing_still_names_the_sweep():
    cfg = SimpleNamespace(remediation=SimpleNamespace(enabled=True, dry_run=False))
    text = _describe_auto_fix(cfg, [])
    assert text.startswith("A matching remediation playbook runs automatically")
    assert "nightly sweep" in text


# ── Caller-declared footer lines and informational reports (11-05 intake) ──
# Measured 2026-11-04/05 in s3://alpha-engine-research/ops/alerts/v1/: the
# sf-telegram-notifier body carried "Auto-fix: none. Unclassified failure ..."
# and flow-doctor appended a contradicting default under it; a SUCCEEDED
# (severity info) notice said "root-causes it" and "none tracked yet".


def test_caller_declared_lines_replace_the_default_footer():
    msg = "Post-close SF FAILED\nCause: X\nAuto-fix: none. Unclassified.\nResolution: aec#13255 (plan)"
    text = TelegramNotifier._format_message(
        _report(error_type=None, error_message=msg), "f", auto_fix="Wiring default."
    )
    assert text.count("Auto-fix:") == 1 and text.count("Resolution:") == 1
    assert text.endswith("*Auto-fix:* none. Unclassified.\n*Resolution:* aec#13255 (plan)")
    assert "Cause: X" in text


def test_caller_declared_resolution_wins_over_a_filed_issue():
    msg = "boom\nResolution: aec#13255"
    text = TelegramNotifier._format_message(
        _report(error_message=msg), "f", auto_fix="Filed.", resolution="https://x/issues/1"
    )
    assert text.endswith("*Auto-fix:* Filed.\n*Resolution:* aec#13255")


def test_only_auto_fix_declared_keeps_the_default_resolution():
    msg = "boom\nAuto-fix: retried once."
    text = TelegramNotifier._format_message(_report(error_message=msg), "f", auto_fix="W.")
    assert text.endswith(f"*Auto-fix:* retried once.\n*Resolution:* {tgmod.DEFAULT_RESOLUTION}")


def test_informational_report_says_nothing_to_fix():
    text = TelegramNotifier._format_message(
        _report(error_type=None, error_message="Pre-open SF SUCCEEDED", severity="info"),
        "f", auto_fix="Wiring default; tonight's sweep root-causes it.",
    )
    assert text.endswith(
        f"*Auto-fix:* {tgmod.INFORMATIONAL}\n*Resolution:* {tgmod.INFORMATIONAL}"
    )
    assert "none tracked yet" not in text


def test_declared_footer_survives_truncation():
    msg = "x\n" + ("l" * 200 + "\n") * 40 + "Resolution: aec#13255"
    text = TelegramNotifier._format_message(
        _report(error_message=msg, logs="\n".join("l" * 200 for _ in range(100))), "f",
        auto_fix="Filed.",
    )
    out = tgmod._truncate_keeping_auto_fix(text)
    assert len(out) <= tgmod._MAX_MESSAGE_LEN
    assert out.endswith("*Auto-fix:* Filed.\n*Resolution:* aec#13255")


def test_oversized_declared_footer_is_bounded():
    """codex 4c7462a53b16aa9a: a declared Resolution line was preserved whole,
    so the footer alone could exceed Telegram's 4096 and the alert 400."""
    msg = "boom\nResolution: " + "r" * 5000
    text = TelegramNotifier._format_message(
        _report(error_message=msg, logs="\n".join("l" * 200 for _ in range(30))), "f",
        auto_fix="Filed.",
    )
    out = tgmod._truncate_keeping_auto_fix(text)
    assert len(out) <= tgmod._MAX_MESSAGE_LEN
    assert "*Auto-fix:* Filed." in out and "*Resolution:* rrr" in out

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

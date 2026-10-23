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


def test_format_message_always_ends_with_auto_fix_line():
    text = TelegramNotifier._format_message(_report(), "f")
    assert text.splitlines()[-1] == f"*Auto-fix:* {tgmod.DEFAULT_AUTO_FIX}"


def test_format_message_uses_the_described_wiring():
    text = TelegramNotifier._format_message(_report(), "f", auto_fix="Filed as issue X.")
    assert text.endswith("*Auto-fix:* Filed as issue X.")


def test_long_alert_keeps_the_line():
    long = _report(logs="\n".join("l" * 200 for _ in range(100)), traceback="t\n" * 50)
    text = TelegramNotifier._format_message(long, "f", auto_fix="Filed.")
    out = tgmod._truncate_keeping_auto_fix(text)
    assert len(out) <= tgmod._MAX_MESSAGE_LEN
    assert out.endswith("*Auto-fix:* Filed.")


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

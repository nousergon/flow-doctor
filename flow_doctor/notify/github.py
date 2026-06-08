"""GitHub issue notification backend."""

from __future__ import annotations

import json
import logging
import sys
from typing import List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import Notifier

_logger = logging.getLogger("flow_doctor")


class GitHubNotifier(Notifier):
    """Create GitHub issues for error reports."""

    def __init__(
        self,
        repo: str,
        token: str,
        labels: Optional[List[str]] = None,
        *,
        auto_fix_pr: bool = False,
        fix_label: str = "flow-doctor:fix",
    ):
        self.repo = repo
        self.token = token
        self.labels = labels or ["flow-doctor"]
        # When True, apply ``fix_label`` to each filed issue so the
        # flow-doctor-fix GitHub Actions workflow generates a fix PR with
        # no human label step. The fix_label is applied as a separate call
        # AFTER issue creation so a ``labeled`` event reliably fires.
        self.auto_fix_pr = auto_fix_pr
        self.fix_label = fix_label

    def validate(self) -> None:
        """Preflight: confirm token is valid via ``GET /user``.

        Raises ``RuntimeError`` on 401/403 so revoked or missing-scope PATs
        fail at FlowDoctor init time. Other errors (network, 5xx) are
        non-fatal — reported to the logger but don't block init, since
        transient GitHub issues shouldn't prevent app startup.

        Skips the network call entirely when
        ``FLOW_DOCTOR_SKIP_PREFLIGHT=1`` is set in the environment — for
        test suites that construct notifiers with fake tokens and for
        air-gapped environments that can't reach api.github.com.
        """
        import os
        if os.environ.get("FLOW_DOCTOR_SKIP_PREFLIGHT") == "1":
            return
        try:
            req = Request(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                method="GET",
            )
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return
                _logger.warning(
                    "flow-doctor GitHub preflight returned HTTP %s (non-401, proceeding)",
                    resp.status,
                )
        except URLError as e:
            reason = getattr(e, "reason", e)
            code = getattr(e, "code", None)
            if code in (401, 403):
                raise RuntimeError(
                    f"flow-doctor GitHub token rejected by api.github.com "
                    f"(HTTP {code}). Check FLOW_DOCTOR_GITHUB_TOKEN (or "
                    f"GITHUB_TOKEN) — it may be revoked, expired, or missing "
                    f"the 'repo' scope for {self.repo}."
                ) from e
            _logger.warning(
                "flow-doctor GitHub preflight non-auth error (proceeding): %s",
                reason,
            )

    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        try:
            title = self._format_title(report, flow_name, diagnosis)
            body = self._format_body(report, flow_name, diagnosis)

            payload = {
                "title": title,
                "body": body,
                "labels": self.labels,
            }

            url = f"https://api.github.com/repos/{self.repo}/issues"
            data = json.dumps(payload).encode("utf-8")
            req = Request(
                url,
                data=data,
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=15) as resp:
                if resp.status == 201:
                    # Return the user-facing issue URL so the dispatcher
                    # can persist it in actions.target for traceability.
                    response_body = json.loads(resp.read().decode("utf-8"))
                    issue_url = response_body.get("html_url", "")
                    issue_number = response_body.get("number")
                    # Auto-fix-PR toggle: apply the fix label so the
                    # flow-doctor-fix workflow generates a PR. Best-effort —
                    # a labeling failure must not flip the issue-creation
                    # success the operator already sees.
                    if self.auto_fix_pr and issue_number is not None:
                        self._add_labels(issue_number, [self.fix_label])
                    return issue_url or f"https://github.com/{self.repo}/issues"
                _logger.critical(
                    "flow-doctor GitHub issue creation returned HTTP %s for repo %s",
                    resp.status, self.repo,
                )
                return None
        except Exception as e:
            # Log via Python logging at CRITICAL so host apps see it in their
            # log stream (journalctl/Sentry/Datadog). Also keep the stderr
            # print for shells without structured logging configured.
            _logger.critical(
                "flow-doctor GitHub issue creation failed for repo %s: %s",
                self.repo, e, exc_info=True,
            )
            print(f"[flow-doctor] GitHub issue creation failed: {e}", file=sys.stderr)
            return None

    def _add_labels(self, issue_number: int, labels: List[str]) -> bool:
        """Apply labels to an existing issue (fires GitHub ``labeled`` events).

        Best-effort: returns False and logs a WARNING on failure rather
        than raising, so an auto-fix-PR labeling miss never masks the
        successful issue creation that the caller already recorded.
        """
        try:
            payload = {"labels": labels}
            url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/labels"
            data = json.dumps(payload).encode("utf-8")
            req = Request(
                url,
                data=data,
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=15) as resp:
                if resp.status in (200, 201):
                    return True
                _logger.warning(
                    "flow-doctor auto-fix-pr: labeling issue #%s in %s returned HTTP %s "
                    "(issue filed OK; fix workflow not triggered)",
                    issue_number, self.repo, resp.status,
                )
                return False
        except Exception as e:
            _logger.warning(
                "flow-doctor auto-fix-pr: failed to apply %s to issue #%s in %s "
                "(issue filed OK; fix workflow not triggered): %s",
                labels, issue_number, self.repo, e,
            )
            return False

    @staticmethod
    def comment_on_issue(
        repo: str,
        issue_number: int,
        body: str,
        token: str,
    ) -> bool:
        """Post a comment on a GitHub issue."""
        try:
            payload = {"body": body}
            url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
            data = json.dumps(payload).encode("utf-8")
            req = Request(
                url,
                data=data,
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=15) as resp:
                return resp.status == 201
        except Exception as e:
            print(f"[flow-doctor] GitHub comment failed: {e}", file=sys.stderr)
            return False

    @staticmethod
    def _format_title(
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> str:
        if diagnosis:
            category = diagnosis.category
            if report.error_type:
                return f"[{category}] {flow_name}: {report.error_type}"
            return f"[{category}] {flow_name}: {report.error_message[:80]}"
        else:
            if report.error_type:
                return f"[{report.severity.upper()}] {flow_name}: {report.error_type}"
            return f"[{report.severity.upper()}] {flow_name}: {report.error_message[:80]}"

    @staticmethod
    def _format_body(
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> str:
        sections = []

        # Header
        severity_badge = {"critical": "🔴", "error": "🟠", "warning": "🟡"}.get(
            report.severity, "⚪"
        )
        sections.append(f"{severity_badge} **Severity:** {report.severity.upper()}")
        sections.append(f"**Flow:** {flow_name}")
        sections.append(f"**Report ID:** `{report.id}`")
        sections.append(f"**Time:** {report.created_at.isoformat()}")

        if report.cascade_source:
            sections.append(
                f"\n> ⚠️ Likely caused by upstream `{report.cascade_source}` failure"
            )

        # Error
        sections.append("\n## Error")
        if report.error_type:
            sections.append(f"```\n{report.error_type}: {report.error_message}\n```")
        else:
            sections.append(f"```\n{report.error_message}\n```")

        # Diagnosis (Phase 2)
        if diagnosis:
            sections.append("\n## Diagnosis")

            category_emoji = {
                "TRANSIENT": "🔄", "DATA": "📊", "CODE": "🐛",
                "CONFIG": "⚙️", "EXTERNAL": "🌐", "INFRA": "🏗️",
            }.get(diagnosis.category, "❓")

            sections.append(f"**Category:** {category_emoji} {diagnosis.category}")
            sections.append(f"**Confidence:** {diagnosis.confidence:.0%}")
            sections.append(f"**Source:** {diagnosis.source}")

            if diagnosis.root_cause:
                sections.append(f"\n### Root Cause\n{diagnosis.root_cause}")

            if diagnosis.remediation:
                sections.append(f"\n### Remediation\n{diagnosis.remediation}")

            if diagnosis.affected_files:
                files_str = "\n".join(f"- `{f}`" for f in diagnosis.affected_files)
                sections.append(f"\n### Affected Files\n{files_str}")

            if diagnosis.alternative_hypotheses:
                alt_str = "\n".join(f"- {h}" for h in diagnosis.alternative_hypotheses)
                sections.append(f"\n### Alternative Hypotheses\n{alt_str}")

            if diagnosis.auto_fixable is not None:
                fixable = "Yes" if diagnosis.auto_fixable else "No"
                sections.append(f"\n**Auto-fixable:** {fixable}")

        # Traceback
        if report.traceback:
            sections.append("\n## Traceback")
            sections.append(f"```python\n{report.traceback}\n```")

        # Logs (truncated)
        if report.logs:
            log_lines = report.logs.strip().splitlines()[-30:]
            sections.append("\n## Captured Logs (last 30 lines)")
            sections.append(f"```\n" + "\n".join(log_lines) + "\n```")

        sections.append("\n---\n*Created by [Flow Doctor](https://github.com/brianmcmahon/flow-doctor)*")

        # Embed machine-readable metadata for the fix CLI
        if diagnosis:
            metadata_block = (
                "\n\n<!-- flow-doctor-metadata\n"
                f"report_id: {report.id}\n"
                f"diagnosis_id: {diagnosis.id}\n"
                f"flow_name: {flow_name}\n"
                f"category: {diagnosis.category}\n"
                f"confidence: {diagnosis.confidence}\n"
                f"error_signature: {report.error_signature or ''}\n"
                f"root_cause: {diagnosis.root_cause}\n"
                f"remediation: {diagnosis.remediation or ''}\n"
                f"affected_files: {','.join(diagnosis.affected_files) if diagnosis.affected_files else ''}\n"
                "-->"
            )
            sections.append(metadata_block)

        return "\n".join(sections)

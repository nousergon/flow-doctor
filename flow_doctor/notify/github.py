"""GitHub issue notification backend."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flow_doctor.core.dedup import compute_body_fingerprint, is_same_finding
from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import (
    PREFLIGHT_UNREACHABLE_MARKER,
    Notifier,
    preflight_timeout,
)

_logger = logging.getLogger("flow_doctor")

# Marker wrapping the fingerprint embedded in every filed issue's body, so
# a later ``send()`` can search the tracker for it via the GitHub Search
# API (``in:body``) before filing a new issue for the same underlying
# finding (alpha-engine-config-I10350 deliverable 1). Distinct from the
# ``flow-doctor-metadata`` block below (which only exists when a
# diagnosis ran) — the fingerprint must be present on EVERY issue,
# diagnosed or not, since the duplicate pairs that motivated this were
# plain error reports with no diagnosis attached.
_FINGERPRINT_MARKER = "flow-doctor-fingerprint"

# Marker opening the machine-readable block the fix CLI parses. Named as a
# constant because TWO places must agree about it: `_format_body`, which emits
# it only when a diagnosis exists, and `send()`, which refuses to apply the
# fix label to a body without it (alpha-engine-config-I10368).
_METADATA_MARKER = "flow-doctor-metadata"

_OCCURRENCES_RE = re.compile(
    r"\*\*Occurrences:\*\*\s*(\d+)\s*\(first\s+([^,]+?),\s*latest\s+([^)]+?)\)"
)

# In-process window for collapsing a wrapper exception onto the payload
# finding it re-states (deliverable 2). The measured pair was 229
# microseconds apart in report-creation time; this is generous enough to
# also catch the case where the two ``report()`` calls straddle a
# synchronous network round trip, while staying well short of it being
# plausible for two genuinely unrelated findings to land here by chance.
_COLLAPSE_WINDOW_SECONDS = 5.0

# Bound on how many recent sends are kept for the collapse check, so a
# long-lived process (a daemon, not a short script) doesn't grow this
# list unbounded. Entries older than _COLLAPSE_WINDOW_SECONDS are pruned
# on every send() anyway; this is a hard backstop.
_MAX_RECENT_SENDS = 200


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
        # (monotonic_ts, error_message, issue_number, issue_url) — recent
        # issues filed BY THIS NOTIFIER INSTANCE, for the same-process
        # wrapper/payload collapse (deliverable 2). Deliberately in-memory
        # and per-instance: it only needs to catch two report() calls in
        # the same process/session, not the cross-session recurrence,
        # which is handled by the tracker search in send() instead.
        self._recent_sends: List[Tuple[float, str, int, str]] = []

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
            with urlopen(req, timeout=preflight_timeout()) as resp:
                if resp.status == 200:
                    return
                _logger.warning(
                    "flow-doctor GitHub preflight returned HTTP %s (non-401, proceeding)",
                    resp.status,
                )
        except (URLError, TimeoutError, OSError) as e:
            # `TimeoutError` is listed explicitly: it is NOT a URLError
            # subclass, so a read timeout used to escape this handler
            # entirely and propagate out of ``FlowDoctor.__init__`` under
            # ``strict`` — the same defect that crashed the predictor
            # Lambda through the Telegram preflight
            # (alpha-engine-config-I8298).
            reason = getattr(e, "reason", e)
            code = getattr(e, "code", None)
            if code in (401, 403):
                raise RuntimeError(
                    f"flow-doctor GitHub token rejected by api.github.com "
                    f"(HTTP {code}). Check FLOW_DOCTOR_GITHUB_TOKEN (or "
                    f"GITHUB_TOKEN) - it may be revoked, expired, or missing "
                    f"the 'repo' scope for {self.repo}."
                ) from e
            _logger.warning(
                "%s flow-doctor GitHub preflight non-auth error (proceeding): %s",
                PREFLIGHT_UNREACHABLE_MARKER, reason,
            )

    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        self.last_error = None
        try:
            now = time.monotonic()
            self._prune_recent_sends(now)

            # 1. Same-process collapse (deliverable 2): a wrapper exception
            # whose str() re-states a finding this instance already filed
            # a few seconds ago. No comment, no occurrence bump — this is
            # the SAME occurrence reaching send() twice, not a recurrence.
            collapse_target = self._find_collapse_target(report.error_message, now)
            if collapse_target is not None:
                _, issue_url = collapse_target
                _logger.info(
                    "flow-doctor: collapsed report onto recently-filed issue %s "
                    "for repo %s (wrapper/payload pair, alpha-engine-config-I10350)",
                    issue_url, self.repo,
                )
                return issue_url

            fingerprint = compute_body_fingerprint(report.error_type, report.error_message)

            # 2. Cross-session dedup (deliverable 1): search the tracker
            # itself for an open issue already carrying this fingerprint
            # before filing a new one. A search failure must never block
            # filing — losing an alert is worse than a duplicate — so a
            # failed/degraded search is treated as "not found" and logged,
            # never swallowed silently.
            existing = self._search_open_issue_by_fingerprint(fingerprint)
            if existing is not None:
                issue_number, issue_url, existing_body = existing
                self._post_recurrence_comment(issue_number, report, flow_name)
                self._bump_occurrences(issue_number, existing_body)
                self._recent_sends.append(
                    (now, report.error_message, issue_number, issue_url)
                )
                self._trim_recent_sends()
                return issue_url

            title = self._format_title(report, flow_name, diagnosis)
            body = self._format_body(report, flow_name, diagnosis, fingerprint=fingerprint)

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
                    if issue_number is not None:
                        self._recent_sends.append(
                            (now, report.error_message, issue_number, issue_url)
                        )
                        self._trim_recent_sends()
                    # Auto-fix-PR toggle: apply the fix label so the
                    # flow-doctor-fix workflow generates a PR. Best-effort —
                    # a labeling failure must not flip the issue-creation
                    # success the operator already sees.
                    #
                    # GATED ON THE BODY ACTUALLY CARRYING METADATA
                    # (alpha-engine-config-I10368). The fix CLI parses the
                    # `flow-doctor-metadata` block, and `_format_body` emits
                    # that block ONLY under `if diagnosis:`. Labelling
                    # regardless dispatched the `issues: [labeled]` workflow
                    # into a guaranteed failure: measured on
                    # nousergon/nousergon-data's `Flow Doctor Fix` — 61
                    # failures, 39 skipped, ZERO successes across its entire
                    # retained history, every one of them exiting on "No
                    # flow-doctor metadata found in issue body", because
                    # diagnosis had been failing closed since 2026-08-13 and
                    # every issue since was titled [DIAGNOSIS UNAVAILABLE].
                    #
                    # An issue with no diagnosis is a tracked record, not a
                    # fix candidate. Saying so here is the only place that
                    # can be known — the workflow sees a label, not a body.
                    if self.auto_fix_pr and issue_number is not None:
                        if _METADATA_MARKER in body:
                            self._add_labels(issue_number, [self.fix_label])
                        else:
                            _logger.warning(
                                "flow-doctor: issue #%s in %s filed WITHOUT a "
                                "diagnosis, so it carries no %s block for the "
                                "fix CLI to parse; %r not applied. The issue "
                                "stands as a tracked record. Diagnosis "
                                "failure reason: %s",
                                issue_number, self.repo, _METADATA_MARKER,
                                self.fix_label,
                                getattr(report, "diagnosis_error", None)
                                or "not recorded",
                            )
                    return issue_url or f"https://github.com/{self.repo}/issues"
                self.last_error = (
                    f"GitHub issue creation returned HTTP {resp.status} for repo {self.repo}"
                )
                _logger.critical(
                    "flow-doctor GitHub issue creation returned HTTP %s for repo %s",
                    resp.status, self.repo,
                )
                return None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            # Log via Python logging at CRITICAL so host apps see it in their
            # log stream (journalctl/Sentry/Datadog). Also keep the stderr
            # print for shells without structured logging configured.
            _logger.critical(
                "flow-doctor GitHub issue creation failed for repo %s: %s",
                self.repo, e, exc_info=True,
            )
            print(f"[flow-doctor] GitHub issue creation failed: {e}", file=sys.stderr)
            return None

    def _prune_recent_sends(self, now: float) -> None:
        """Drop entries older than the collapse window."""
        cutoff = now - _COLLAPSE_WINDOW_SECONDS
        self._recent_sends = [e for e in self._recent_sends if e[0] >= cutoff]

    def _trim_recent_sends(self) -> None:
        """Hard cap so a long-lived process never grows this list unbounded."""
        if len(self._recent_sends) > _MAX_RECENT_SENDS:
            self._recent_sends = self._recent_sends[-_MAX_RECENT_SENDS:]

    def _find_collapse_target(
        self, error_message: str, now: float
    ) -> Optional[Tuple[int, str]]:
        """Return (issue_number, issue_url) of a just-filed issue for the same
        finding, or None.

        Walks newest-first so the most recent matching filing wins when
        more than one recent send happens to overlap.
        """
        for ts, msg, issue_number, issue_url in reversed(self._recent_sends):
            if now - ts > _COLLAPSE_WINDOW_SECONDS:
                continue
            if is_same_finding(error_message, msg):
                return issue_number, issue_url
        return None

    def _search_open_issue_by_fingerprint(
        self, fingerprint: str
    ) -> Optional[Tuple[int, str, str]]:
        """Search this repo's OPEN issues for one carrying ``fingerprint``.

        Returns (issue_number, issue_url, body) of the first match, or
        None if none was found OR the search itself failed. A search
        failure (network, rate limit, malformed response) is logged at
        WARNING and treated as "not found" — never raised, and never
        silently absorbed either: the caller proceeds to file a normal
        issue rather than lose the alert, and the WARNING is the durable
        record that dedup lookup degraded for this call.
        """
        try:
            query = f'repo:{self.repo} is:issue is:open "{fingerprint}" in:body'
            url = "https://api.github.com/search/issues?q=" + quote(query, safe="")
            req = Request(
                url,
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                method="GET",
            )
            with urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    _logger.warning(
                        "flow-doctor: GitHub fingerprint search returned HTTP %s "
                        "for repo %s (filing normally, not treated as a duplicate)",
                        resp.status, self.repo,
                    )
                    return None
                data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items") or []
            if not items:
                return None
            item = items[0]
            return item["number"], item.get("html_url", ""), item.get("body") or ""
        except Exception as e:  # noqa: BLE001 - a degraded dedup lookup must
            # never block filing (losing an alert is worse than a possible
            # duplicate); recorded at WARNING so the degradation itself is
            # visible rather than swallowed (alpha-engine-config-I10350).
            _logger.warning(
                "flow-doctor: GitHub fingerprint search failed for repo %s "
                "(filing normally, not treated as a duplicate): %s",
                self.repo, e, exc_info=True,
            )
            return None

    def _post_recurrence_comment(
        self, issue_number: int, report: Report, flow_name: str
    ) -> bool:
        """Post a comment marking a new occurrence of an already-tracked finding."""
        body = (
            f"**Recurrence** — {flow_name} reported this finding again "
            f"at {report.created_at.isoformat()} (report `{report.id}`, "
            f"severity {report.severity.upper()}). Not filed as a new issue "
            f"— see the `Occurrences` count above."
        )
        return self.comment_on_issue(self.repo, issue_number, body, self.token)

    def _bump_occurrences(self, issue_number: int, body: str) -> bool:
        """Increment the ``Occurrences`` line on an existing issue's body.

        Parses ``**Occurrences:** N (first X, latest Y)`` and rewrites it
        with N+1 and ``latest`` set to now; ``first`` is preserved
        unchanged. If the line is missing (an issue filed before this
        fix), appends one with N=2 — the issue already represents one
        prior occurrence, this call is the second. Best-effort: a failure
        here must not be treated as filing failure — the recurrence
        comment already recorded the occurrence, this only keeps the
        visible counter current — but it is logged at WARNING, not
        swallowed.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        match = _OCCURRENCES_RE.search(body)
        if match:
            count = int(match.group(1)) + 1
            first = match.group(2)
            new_line = f"**Occurrences:** {count} (first {first}, latest {now_iso})"
            new_body = body[: match.start()] + new_line + body[match.end():]
        else:
            new_line = f"**Occurrences:** 2 (first unknown, latest {now_iso})"
            new_body = body.rstrip() + f"\n\n{new_line}\n"

        try:
            payload = {"body": new_body}
            url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}"
            data = json.dumps(payload).encode("utf-8")
            req = Request(
                url,
                data=data,
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                method="PATCH",
            )
            with urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
                _logger.warning(
                    "flow-doctor: bumping Occurrences on issue #%s in %s returned "
                    "HTTP %s (recurrence comment still posted)",
                    issue_number, self.repo, resp.status,
                )
                return False
        except Exception as e:
            _logger.warning(
                "flow-doctor: failed to bump Occurrences on issue #%s in %s "
                "(recurrence comment still posted): %s",
                issue_number, self.repo, e,
            )
            return False

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
        elif report.diagnosis_error:
            if report.error_type:
                return f"[DIAGNOSIS UNAVAILABLE] {flow_name}: {report.error_type}"
            return f"[DIAGNOSIS UNAVAILABLE] {flow_name}: {report.error_message[:80]}"
        else:
            if report.error_type:
                return f"[{report.severity.upper()}] {flow_name}: {report.error_type}"
            return f"[{report.severity.upper()}] {flow_name}: {report.error_message[:80]}"

    @staticmethod
    def _format_body(
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
        fingerprint: Optional[str] = None,
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
        # Visible from filing (alpha-engine-config-I10350 deliverable 3):
        # a chronic condition must read as chronic, not as N one-off
        # issues. Bumped in place by ``_bump_occurrences`` on every
        # tracker-matched recurrence.
        sections.append(
            f"**Occurrences:** 1 (first {report.created_at.isoformat()}, "
            f"latest {report.created_at.isoformat()})"
        )

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
        elif report.diagnosis_error:
            sections.append("\n## Diagnosis")
            sections.append(f"⚠️ **Unavailable** — diagnosis was attempted and failed:")
            sections.append(f"```\n{report.diagnosis_error}\n```")

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

        # Fingerprint marker: embedded on EVERY issue (diagnosed or not),
        # unlike the flow-doctor-metadata block below, because the
        # cross-session dedup search in ``send()`` (alpha-engine-config-
        # I10350 deliverable 1) needs it regardless of whether diagnosis
        # ran — the duplicate pairs that motivated this had no diagnosis.
        if fingerprint:
            sections.append(f"\n\n<!-- {_FINGERPRINT_MARKER}: {fingerprint} -->")

        # Embed machine-readable metadata for the fix CLI
        if diagnosis:
            metadata_block = (
                f"\n\n<!-- {_METADATA_MARKER}\n"
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

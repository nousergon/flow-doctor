"""Rate limiting: tiered degradation and cascade-aware budget."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from flow_doctor.core.models import ActionStatus, ActionType

if TYPE_CHECKING:
    from flow_doctor.storage.base import StorageBackend
    from flow_doctor.core.config import RateLimitConfig

_logger = logging.getLogger(__name__)

# Action types that are "an alert reached a human" and therefore share
# ``max_alerts_per_day``. Derived from ActionType rather than hand-listed so a
# newly-added notifier cannot silently end up unbudgeted — see the incident in
# ``check``'s docstring.
_ALERT_ACTIONS = frozenset({
    ActionType.SLACK_ALERT.value,
    ActionType.EMAIL_ALERT.value,
    ActionType.S3_ALERT.value,
    ActionType.TELEGRAM_ALERT.value,
})
_ISSUE_ACTIONS = frozenset({
    ActionType.GITHUB_ISSUE.value,
    ActionType.GITHUB_PR.value,
})


class RateLimiter:
    """Tiered rate limiter that returns 'allow' or 'degrade' for each action."""

    def __init__(
        self,
        store: StorageBackend,
        config: RateLimitConfig,
        flow_name: Optional[str] = None,
    ) -> None:
        self.store = store
        self.config = config
        # The budget reads as per-component in every config file — each declares
        # its own `max_alerts_per_day` — but the count behind it was not scoped,
        # so every flow sharing a store drew on ONE budget. `check`'s docstring
        # already names "a store SHARED by every consumer" as part of the
        # 2026-07-28 blackout; this is the half of that which was never closed.
        # Measured 2026-08-11: five flows (`executor`, `data-collector`,
        # `predictor-inference`, `predictor-regime`, `research-lambda`) on one
        # `flow-doctor-store` table. Since 0.8.8 the severity exemption keeps
        # error/critical out of the cap, so the remaining exposure is warning
        # and info — real, but not a page (alpha-engine-config-I6921).
        self.flow_name = flow_name
        limits = {"diagnosis": config.max_diagnosed_per_day}
        for action in _ISSUE_ACTIONS:
            limits[action] = config.max_issues_per_day
        for action in _ALERT_ACTIONS:
            limits[action] = config.max_alerts_per_day
        self.limits = limits

        # Fail loud at construction if an ActionType exists that this limiter
        # does not budget: that is the defect below, caught one layer earlier.
        unbudgeted = {a.value for a in ActionType} - set(limits)
        if unbudgeted:
            _logger.warning(
                "RateLimiter has no configured budget for action type(s) %s — "
                "they will be ALLOWED unrestricted. Add them to _ALERT_ACTIONS "
                "or _ISSUE_ACTIONS in flow_doctor/core/rate_limiter.py.",
                sorted(unbudgeted),
            )

    def check(self, action: str, severity: Optional[str] = None) -> str:
        """Returns 'allow' or 'degrade'.

        An unmapped action FAILS OPEN (allow), loudly. It previously fell back
        to a hardcoded ``10``, which was the generative defect behind a
        fleet-wide alert blackout (2026-07-28):

        ``telegram_alert`` and ``s3_alert`` were never added to the budget map
        when those notifiers shipped, so a config setting
        ``max_alerts_per_day: 100`` reached only ``slack_alert``/``email_alert``
        — channels that fleet did not use — while Telegram, the only channel
        anyone actually read, silently took the hardcoded 10/day, counted
        against a store SHARED by every consumer. The budget burned out early
        each day, so terminal pipeline notifications (which happen late) were
        systematically dropped while start-of-run pings got through. Two
        consecutive production trading-pipeline failures paged and were never
        seen; 12 of 13 terminal notifications were suppressed over two days.

        A silent small default is the wrong failure direction for an alerting
        library: an extra alert costs noise, a dropped one costs an outage.

        ``severity`` exempts genuine failure pages from the daily cap
        (``rate_limit_exempt_severities``, default critical+error). Storms of
        the SAME failure are already handled by signature dedup and
        ``dedup_cooldown_minutes``; the daily cap is a blunt backstop that must
        never be the thing that silences a page.
        """
        if severity is not None and severity in self.config.rate_limit_exempt_severities:
            return "allow"

        limit = self.limits.get(action)
        if limit is None:
            _logger.warning(
                "Rate limiter has no budget for action %r — allowing "
                "unrestricted. This is a mapping gap, not a policy: add it to "
                "flow_doctor/core/rate_limiter.py.",
                action,
            )
            return "allow"

        # FAILED alert deliveries do not spend the alert budget
        # (alpha-engine-config-I12711). That cap counts alerts that reached a
        # human; on 2026-10-26 seven failed executor sends spent the 10/day
        # `telegram_alert` budget and the cap then held a trade alert. Alert
        # actions only: a FAILED diagnosis or issue attempt still spent an LLM
        # call or an API write, so those budgets keep counting it. DEGRADED
        # rows still count everywhere: the first-crossing ERROR below relies
        # on them.
        exclude = ActionStatus.FAILED.value if action in _ALERT_ACTIONS else None
        today_count = self.store.count_actions_today(
            action, self.flow_name, exclude_status=exclude
        )
        if today_count < limit:
            return "allow"

        # SATURATION SIGNAL (alpha-engine-config-I6927, Brian's ruling
        # 2026-08-11: keep the cap, but stop treating it as invisible
        # plumbing).
        #
        # The FIRST crossing reports at ERROR; every subsequent degrade that day
        # stays at WARNING. `today_count == limit` is exactly the first
        # crossing: degraded actions are persisted with the same action_type, so
        # the count keeps climbing past the limit and this equality holds once.
        # No new storage, no status filter, no cursor.
        #
        # ERROR is what makes it REACH someone. `krepis.logging.setup_logging`
        # attaches its alert handler to the ROOT logger at ERROR, so a WARNING —
        # all 0.10.0 emitted — lands in logs nobody reads during an incident.
        # That is the same shape as the blackout in this docstring: the system
        # knew it was suppressing and said so where no one was looking.
        #
        # It does not recurse. The report this ERROR generates is itself
        # severity=error, and error is in `rate_limit_exempt_severities` by
        # default, so it returns "allow" and never re-enters this branch. If an
        # operator empties that list they get one extra degraded row per day per
        # (flow, action) — bounded, and visible in the store.
        #
        # A cap whose saturation is unobservable has caused two incidents here
        # and prevented no recorded one; this is the condition of keeping it.
        level = logging.ERROR if today_count == limit else logging.WARNING
        _logger.log(
            level,
            "flow=%s action=%s: daily budget reached (%d/%d) — this and further "
            "non-exempt %s from this flow are DEGRADED and will not be "
            "delivered until UTC midnight.",
            self.flow_name or "<unscoped>", action, today_count, limit, action,
        )
        return "degrade"


class CascadeDetector:
    """Detect if a failure is caused by an upstream dependency failure."""

    def __init__(self, store: StorageBackend, window_hours: int = 4) -> None:
        self.store = store
        self.window_hours = window_hours

    def check_cascade(
        self,
        dependencies: list[str],
        flow_name: str,
    ) -> Optional[str]:
        """Check if any dependency reported a failure within the cascade window.

        Returns the dependency flow_name that failed, or None.
        """
        if not dependencies:
            return None
        cutoff = datetime.utcnow() - timedelta(hours=self.window_hours)
        for dep in dependencies:
            if self.store.has_recent_failure(dep, since=cutoff):
                return dep
        return None

"""Decision gate: routes diagnosis to auto-remediate, generate PR, or escalate.

The gate uses diagnosis confidence and category to determine the appropriate
action, with safety rails for market hours and retry limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from flow_doctor.core.models import Diagnosis
from flow_doctor.remediation.playbook import (
    Playbook,
    PlaybookPattern,
    RemediationAction,
    RemediationType,
)

logger = logging.getLogger("flow_doctor.decision_gate")


@runtime_checkable
class _RemediationCounter(Protocol):
    """The one storage method the gate reads (SQLite and DynamoDB both have it)."""

    def count_remediations_today(self) -> int: ...


#: The exchange clock. Market hours are read in this zone, never as a fixed
#: UTC offset, so the guard holds across the US daylight-time changes.
MARKET_TZ = ZoneInfo("America/New_York")


class DecisionType(str, Enum):
    AUTO_REMEDIATE = "auto_remediate"
    GENERATE_FIX_PR = "generate_fix_pr"
    ESCALATE = "escalate"
    LOG_ONLY = "log_only"


@dataclass
class Decision:
    """The gate's routing decision for a diagnosed failure."""
    decision_type: DecisionType
    reason: str
    diagnosis: Diagnosis
    playbook_match: Optional[PlaybookPattern] = None
    action: Optional[RemediationAction] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GateConfig:
    """Configuration for the decision gate."""
    # Confidence thresholds. Raised in the conservative-defaults revision
    # (fix_pr_min_confidence 0.8 → 0.85) to cut false-positive PR volume.
    auto_remediate_min_confidence: float = 0.9
    fix_pr_min_confidence: float = 0.85
    # Market hours lockout (ET)
    market_open_hour: int = 9   # 9:30 AM ET
    market_close_hour: int = 16  # 4:00 PM ET
    # Retry limits. max_auto_remediations_per_day lowered 5 → 2 in the
    # conservative-defaults revision to keep review bandwidth manageable.
    max_auto_remediations_per_day: int = 2
    max_auto_remediations_per_failure: int = 2
    # Hard deny list. Repos on this list will NEVER have auto-fix applied.
    # Matched against the flow_name OR repo field on the diagnosis/report
    # in a case-insensitive substring check, so both "owner/repo" and
    # bare "repo" work. Issue-filing for these repos still works — only
    # code modifications (auto_remediate, generate_fix_pr) are blocked.
    deny_repos: List[str] = field(default_factory=list)
    # Categories eligible for auto-remediation
    auto_remediable_categories: List[str] = field(
        default_factory=lambda: ["INFRA", "CONFIG", "TRANSIENT", "DATA"]
    )
    # Categories eligible for PR generation
    pr_eligible_categories: List[str] = field(
        default_factory=lambda: ["CODE", "CONFIG"]
    )


class DecisionGate:
    """Routes diagnosed failures to the appropriate remediation path."""

    def __init__(
        self,
        playbook: Optional[Playbook] = None,
        config: Optional[GateConfig] = None,
        store: object = None,
    ) -> None:
        self.playbook = playbook or Playbook()
        self.config = config or GateConfig()
        self._store = store  # Optional SQLite store for persistent counting
        self._daily_remediation_count = 0
        self._failure_remediation_counts: Dict[str, int] = {}
        self._last_reset_date: Optional[str] = None

    def decide(
        self,
        diagnosis: Diagnosis,
        error_type: Optional[str] = None,
        error_message: str = "",
        flow_name: Optional[str] = None,
    ) -> Decision:
        """Determine the appropriate action for a diagnosed failure."""
        self._maybe_reset_daily_counts()

        # 1. Check for playbook match
        playbook_match = self.playbook.match(error_type, error_message, flow_name)

        # 2. Hard deny list — block ALL code modifications (auto-remediate +
        # fix PR generation) for repos on the deny list. This is the last
        # line of defense for production-critical repos where a bad fix
        # could cost real money or safety. Issue-filing still works for
        # these repos because this check only gates the auto-fix path.
        denied = self._check_deny_repo(diagnosis, flow_name)
        if denied:
            logger.info(
                "decision_gate: repo %s is on the deny list — escalating "
                "to human instead of auto-remediating / generating PR",
                denied,
            )
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason=(
                    f"Repo '{denied}' is on the remediation deny list. "
                    f"Auto-fix is disabled for this repo."
                ),
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # 3. Route based on confidence and category
        if diagnosis.confidence >= self.config.auto_remediate_min_confidence:
            if diagnosis.category in self.config.auto_remediable_categories:
                return self._try_auto_remediate(diagnosis, playbook_match)

        if diagnosis.confidence >= self.config.fix_pr_min_confidence:
            if diagnosis.category in self.config.pr_eligible_categories:
                return Decision(
                    decision_type=DecisionType.GENERATE_FIX_PR,
                    reason=f"High-confidence {diagnosis.category} issue — generating fix PR",
                    diagnosis=diagnosis,
                    playbook_match=playbook_match,
                )

        if diagnosis.category == "TRANSIENT":
            return Decision(
                decision_type=DecisionType.LOG_ONLY,
                reason="Transient error — log and monitor",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        return Decision(
            decision_type=DecisionType.ESCALATE,
            reason=f"Confidence {diagnosis.confidence:.2f} below threshold — escalating to human",
            diagnosis=diagnosis,
            playbook_match=playbook_match,
        )

    def _check_deny_repo(
        self,
        diagnosis: Diagnosis,
        flow_name: Optional[str],
    ) -> Optional[str]:
        """Check if the diagnosis's repo is on the deny list.

        Returns the matched deny-list entry if blocked, or None if the
        repo is allowed to proceed. Match is case-insensitive substring,
        so both ``owner/repo`` and bare ``repo`` formats work.

        Looks at multiple sources for the repo name:
        1. diagnosis.context.get('repo') if the diagnosis set it
        2. flow_name (common pattern: flow_name includes repo name)
        3. diagnosis.flow_name
        """
        if not self.config.deny_repos:
            return None

        haystacks: List[str] = []
        # Diagnosis may carry repo explicitly in context
        ctx = getattr(diagnosis, "context", None)
        if isinstance(ctx, dict) and "repo" in ctx:
            haystacks.append(str(ctx["repo"]))
        if flow_name:
            haystacks.append(flow_name)
        diag_flow = getattr(diagnosis, "flow_name", None)
        if diag_flow:
            haystacks.append(diag_flow)

        for deny in self.config.deny_repos:
            deny_lc = deny.lower()
            for hay in haystacks:
                if deny_lc in hay.lower():
                    return deny
        return None

    def _get_daily_remediation_count(self) -> int:
        """Get today's remediation count — persistent (SQLite) or in-memory."""
        if isinstance(self._store, _RemediationCounter):
            try:
                return self._store.count_remediations_today()
            except Exception:
                pass
        return self._daily_remediation_count

    def _try_auto_remediate(
        self,
        diagnosis: Diagnosis,
        playbook_match: Optional[PlaybookPattern],
    ) -> Decision:
        """Attempt auto-remediation with safety checks."""
        # Safety: daily limit (persistent when store available)
        daily_count = self._get_daily_remediation_count()
        if daily_count >= self.config.max_auto_remediations_per_day:
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason=f"Daily auto-remediation limit ({self.config.max_auto_remediations_per_day}) reached",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # Safety: per-failure limit
        failure_key = f"{diagnosis.flow_name}:{diagnosis.category}:{diagnosis.root_cause[:50]}"
        failure_count = self._failure_remediation_counts.get(failure_key, 0)
        if failure_count >= self.config.max_auto_remediations_per_failure:
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason=f"Max retries ({self.config.max_auto_remediations_per_failure}) reached for this failure",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # Safety: market hours lockout for trading-path components
        if not self._is_safe_for_auto_remediation(playbook_match):
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason="Market hours lockout — auto-remediation blocked",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # Must have a playbook match with action to auto-remediate
        if not playbook_match:
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason="No playbook pattern — cannot auto-remediate without known fix",
                diagnosis=diagnosis,
            )

        action = playbook_match.action
        if action.action_type == RemediationType.ESCALATE:
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason=f"Playbook pattern '{playbook_match.name}' requires human intervention",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        if action.action_type == RemediationType.CODE_FIX:
            return Decision(
                decision_type=DecisionType.GENERATE_FIX_PR,
                reason=f"Playbook pattern '{playbook_match.name}' requires code change",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # Auto-remediate
        self._daily_remediation_count += 1
        self._failure_remediation_counts[failure_key] = failure_count + 1

        return Decision(
            decision_type=DecisionType.AUTO_REMEDIATE,
            reason=f"Matched playbook pattern '{playbook_match.name}' — auto-remediating",
            diagnosis=diagnosis,
            playbook_match=playbook_match,
            action=action,
        )

    def _is_safe_for_auto_remediation(
        self, playbook_match: Optional[PlaybookPattern],
    ) -> bool:
        """Check if auto-remediation is safe given current market hours."""
        if playbook_match and playbook_match.action.safe_during_market_hours:
            return True

        now_et = self._now().astimezone(MARKET_TZ)
        et_hour = now_et.hour
        is_market_hours = self.config.market_open_hour <= et_hour < self.config.market_close_hour
        is_weekday = now_et.weekday() < 5

        if is_market_hours and is_weekday:
            return False
        return True

    @staticmethod
    def _now() -> datetime:
        """Current time, UTC-aware. A seam so tests can pin the clock."""
        return datetime.now(timezone.utc)

    def _maybe_reset_daily_counts(self) -> None:
        """Reset daily counters at midnight."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_remediation_count = 0
            self._failure_remediation_counts.clear()
            self._last_reset_date = today

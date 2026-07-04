"""Abstract base class for notification backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Set

from flow_doctor.core.models import Diagnosis, Report


class Notifier(ABC):
    """Pluggable notification interface."""

    # Severity routing for this notifier instance, set by
    # ``FlowDoctor._init_notifiers`` from the config's ``notify_on``. A set
    # of severity strings (e.g. {"critical", "error", "info"}); when None
    # the dispatcher applies the default set {critical, error}. Custom
    # notifier subclasses inherit this attribute and need not set it.
    notify_on: Optional[Set[str]] = None

    # Diagnosis-category routing for this notifier instance, set by
    # ``FlowDoctor._init_notifiers`` from the config's ``notify_on_category``.
    # A set of uppercased category strings (e.g. {"CODE", "CONFIG"}); when
    # None, every category reaches this notifier (unchanged pre-0.8.0
    # behavior). Requires Phase 2 diagnosis to be enabled — a report with no
    # diagnosis always passes this gate regardless of what's configured
    # here, since an unavailable enrichment must never silently blank a
    # channel. Custom notifier subclasses inherit this attribute and need
    # not set it.
    notify_on_category: Optional[Set[str]] = None

    @abstractmethod
    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        """Send a notification for the given report.

        Args:
            report: The error report.
            flow_name: Name of the flow that failed.
            diagnosis: Optional diagnosis to enrich the notification.

        Returns:
            On success, a target identifier string that will be stored in
            the action record's ``target`` field — typically a user-facing
            URL (GitHub issue URL, Slack webhook endpoint) or address
            (email recipients). On failure, ``None``.

            Callers should use truthiness (``if send(...)``) to distinguish
            success from failure, and use the value to construct follow-up
            links when it is non-empty.
        """

    def validate(self) -> None:
        """Lightweight auth/reachability preflight.

        Called by ``FlowDoctor.__init__`` in strict mode so revoked tokens
        and unreachable backends fail fast at startup instead of silently
        dropping error reports later. Subclasses that can cheaply verify
        their credentials (e.g., a GitHub ``GET /user`` call) should
        override this and raise on auth failure. Default is no-op.
        """
        return None

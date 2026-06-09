"""Abstract base class for storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from typing import Dict

from flow_doctor.core.models import (
    Action,
    Decision,
    Diagnosis,
    FixAttempt,
    KnownPattern,
    Report,
)


class StorageBackend(ABC):
    """Pluggable storage interface for Flow Doctor."""

    @abstractmethod
    def init_schema(self) -> None:
        """Create tables/schema if they don't exist."""

    @abstractmethod
    def save_report(self, report: Report) -> None:
        """Persist a report."""

    @abstractmethod
    def save_action(self, action: Action) -> None:
        """Persist an action record."""

    @abstractmethod
    def save_decision(self, decision: Decision) -> None:
        """Persist a dispatch decision (why an error did/didn't alert)."""

    @abstractmethod
    def decision_breakdown_today(self, flow_name: Optional[str] = None) -> Dict[str, int]:
        """Return today's decision counts keyed by reason (UTC), optionally per flow."""

    @abstractmethod
    def find_report_by_signature(
        self,
        error_signature: str,
        since: datetime,
    ) -> Optional[Report]:
        """Find the most recent report with this signature since cutoff."""

    @abstractmethod
    def increment_dedup_count(self, report_id: str) -> None:
        """Increment the dedup_count for a report."""

    @abstractmethod
    def count_actions_today(self, action_type: str) -> int:
        """Count actions of the given type created today (UTC)."""

    @abstractmethod
    def has_recent_failure(self, flow_name: str, since: datetime) -> bool:
        """Check if a flow reported a failure since the given time."""

    @abstractmethod
    def get_reports(
        self,
        flow_name: Optional[str] = None,
        limit: int = 10,
    ) -> List[Report]:
        """Get recent reports, optionally filtered by flow name."""

    @abstractmethod
    def get_report(self, report_id: str) -> Optional[Report]:
        """Get a single report by ID."""

    @abstractmethod
    def save_diagnosis(self, diagnosis: Diagnosis) -> None:
        """Persist a diagnosis."""

    @abstractmethod
    def get_diagnosis_by_report(self, report_id: str) -> Optional[Diagnosis]:
        """Get the diagnosis for a given report."""

    @abstractmethod
    def find_known_pattern(self, error_signature: str) -> Optional[KnownPattern]:
        """Find a known pattern matching this error signature."""

    @abstractmethod
    def save_known_pattern(self, pattern: KnownPattern) -> None:
        """Persist a known pattern."""

    @abstractmethod
    def increment_pattern_hit(self, pattern_id: str) -> None:
        """Increment hit_count and update last_seen for a known pattern."""

    @abstractmethod
    def get_degraded_actions(self, since: datetime) -> List[Action]:
        """Get all degraded actions since the given time."""

    @abstractmethod
    def save_fix_attempt(self, attempt: FixAttempt) -> None:
        """Persist a fix attempt."""

    @abstractmethod
    def get_fix_attempts_for_diagnosis(self, diagnosis_id: str) -> List[FixAttempt]:
        """Get all fix attempts for a given diagnosis."""

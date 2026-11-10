"""Knowledge base: error signature → known diagnosis mapping."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, TypedDict

from flow_doctor.core.models import Diagnosis, KnownPattern
from flow_doctor.storage.base import StorageBackend


class _BootstrapPattern(TypedDict, total=False):
    error_signature: str
    category: str
    root_cause: str
    flow_name: Optional[str]
    resolution: Optional[str]
    auto_fixable: bool


class KnowledgeBase:
    """Checks known patterns before calling the LLM.

    Populated from:
    1. Bootstrap patterns (seeded at init from config)
    2. Confirmed diagnoses (via feedback loop)
    """

    def __init__(self, store: StorageBackend) -> None:
        self.store = store

    def lookup(self, error_signature: str, report_id: str, flow_name: str) -> Optional[Diagnosis]:
        """Check if this error signature has a known diagnosis.

        Returns a Diagnosis with source="knowledge_base" if found, None otherwise.
        """
        pattern = self.store.find_known_pattern(error_signature)
        if pattern is None:
            return None

        # Record the hit
        self.store.increment_pattern_hit(pattern.id)

        return Diagnosis(
            report_id=report_id,
            flow_name=flow_name,
            category=pattern.category,
            root_cause=pattern.root_cause,
            confidence=0.95,  # High confidence for known patterns
            remediation=pattern.resolution,
            auto_fixable=pattern.auto_fixable,
            source="knowledge_base",
        )

    def record(self, diagnosis: Diagnosis, error_signature: str) -> None:
        """Promote a confirmed diagnosis to a known pattern.

        Called when feedback confirms a diagnosis is correct.
        """
        existing = self.store.find_known_pattern(error_signature)
        if existing is not None:
            return  # Already in KB

        pattern = KnownPattern(
            error_signature=error_signature,
            category=diagnosis.category,
            root_cause=diagnosis.root_cause,
            flow_name=diagnosis.flow_name,
            resolution=diagnosis.remediation,
            auto_fixable=diagnosis.auto_fixable or False,
            hit_count=1,
            # Preserve the existing naive UTC timestamp representation.
            last_seen=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        self.store.save_known_pattern(pattern)

    def bootstrap(self, patterns: List[_BootstrapPattern]) -> None:
        """Seed the knowledge base with known failure patterns.

        Each dict should have: error_signature, category, root_cause,
        and optionally: flow_name, resolution, auto_fixable.
        """
        for p in patterns:
            sig = p.get("error_signature", "")
            if not sig:
                continue
            existing = self.store.find_known_pattern(sig)
            if existing is not None:
                continue  # Don't overwrite

            pattern = KnownPattern(
                error_signature=sig,
                category=p.get("category", "CODE"),
                root_cause=p.get("root_cause", ""),
                flow_name=p.get("flow_name"),
                resolution=p.get("resolution"),
                auto_fixable=p.get("auto_fixable", False),
            )
            self.store.save_known_pattern(pattern)

"""The market-hours safety rail reads the exchange clock, not a fixed UTC-4.

Before this, ``et_hour = (utc_hour - 4) % 24``: correct under EDT, one hour
off under EST, so 15:00-16:00 EST (20:00-21:00Z) read as after-hours and
auto-remediation was allowed during the last market hour.
"""

from datetime import datetime, timezone

import pytest

from flow_doctor.remediation.decision_gate import DecisionGate, GateConfig


def _gate(monkeypatch: pytest.MonkeyPatch, utc: datetime) -> DecisionGate:
    gate = DecisionGate(config=GateConfig())
    monkeypatch.setattr(DecisionGate, "_now", staticmethod(lambda: utc))
    return gate


@pytest.mark.parametrize(
    "utc, safe",
    [
        # EST (after 2026-11-01): session 14:30-21:00Z.
        (datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc), False),  # 15:30 EST
        (datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc), True),   # 08:30 EST
        (datetime(2026, 11, 2, 21, 5, tzinfo=timezone.utc), True),    # 16:05 EST
        # EDT: session 13:30-20:00Z.
        (datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc), False),  # 09:30 EDT
        (datetime(2026, 10, 28, 20, 30, tzinfo=timezone.utc), True),   # 16:30 EDT
        # Weekday is the ET weekday: Fri 22:00 ET is Sat 03:00Z.
        (datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc), True),
    ],
)
def test_market_hours_follow_the_exchange_clock(
    monkeypatch: pytest.MonkeyPatch, utc: datetime, safe: bool
) -> None:
    assert _gate(monkeypatch, utc)._is_safe_for_auto_remediation(None) is safe  # pyright: ignore[reportPrivateUsage]

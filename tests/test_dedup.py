"""Tests for deduplication."""

import tempfile

from flow_doctor.core.dedup import (
    DedupChecker,
    compute_error_signature,
    compute_signature_from_exception,
    compute_signature_from_message,
    normalize_message_for_signature,
)
from flow_doctor.core.models import Report
from flow_doctor.storage.sqlite import SQLiteStorage


def test_error_signature_same_exception():
    """Same exception type + traceback should produce the same signature."""
    tb = '''Traceback (most recent call last):
  File "handler.py", line 10, in main
    run_pipeline()
  File "pipeline.py", line 42, in run_pipeline
    fetch_data()
  File "data.py", line 15, in fetch_data
    raise ValueError("bad data")
ValueError: bad data'''

    sig1 = compute_error_signature("ValueError", tb)
    sig2 = compute_error_signature("ValueError", tb)
    assert sig1 == sig2


def test_error_signature_different_exception():
    """Different exception types should produce different signatures."""
    tb = '''Traceback (most recent call last):
  File "handler.py", line 10, in main
    run_pipeline()
'''
    sig1 = compute_error_signature("ValueError", tb)
    sig2 = compute_error_signature("KeyError", tb)
    assert sig1 != sig2


def test_error_signature_different_frames():
    """Different stack frames should produce different signatures."""
    tb1 = '''Traceback (most recent call last):
  File "handler.py", line 10, in main
    run_pipeline()
'''
    tb2 = '''Traceback (most recent call last):
  File "handler.py", line 20, in other_func
    do_something()
'''
    sig1 = compute_error_signature("ValueError", tb1)
    sig2 = compute_error_signature("ValueError", tb2)
    assert sig1 != sig2


def test_compute_signature_from_exception():
    """Should compute signature from a live exception."""
    try:
        raise ValueError("test error")
    except ValueError as e:
        sig = compute_signature_from_exception(e)
        assert isinstance(sig, str)
        assert len(sig) == 16  # hex digest prefix


def test_dedup_checker_no_duplicate():
    """First report should not be a duplicate."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = SQLiteStorage(f.name)
        store.init_schema()
        checker = DedupChecker(store, cooldown_minutes=60)

        is_dup, existing_id = checker.is_duplicate("sig123")
        assert is_dup is False
        assert existing_id is None


def test_dedup_checker_finds_duplicate():
    """Second report with same signature should be flagged as duplicate."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = SQLiteStorage(f.name)
        store.init_schema()

        # Save a report with a known signature
        report = Report(
            flow_name="test",
            error_message="boom",
            error_signature="sig123",
        )
        store.save_report(report)

        checker = DedupChecker(store, cooldown_minutes=60)
        is_dup, existing_id = checker.is_duplicate("sig123")
        assert is_dup is True
        assert existing_id == report.id


# Real IB error fixtures captured from alpha-engine on 2026-04-13.
# Three back-to-back emails were sent for what is operationally one incident
# (IB Gateway "competing live session" for different contracts).
_IB_10197_D = (
    "Error 10197, reqId 257: No market data during competing live session, "
    "contract: Stock(conId=6327, symbol='D', exchange='SMART', "
    "primaryExchange='NYSE', currency='USD', localSymbol='D', "
    "tradingClass='D')"
)
_IB_10197_LLY = (
    "Error 10197, reqId 261: No market data during competing live session, "
    "contract: Stock(conId=9160, symbol='LLY', exchange='SMART', "
    "primaryExchange='NYSE', currency='USD', localSymbol='LLY', "
    "tradingClass='LLY')"
)
_IB_10197_CASY = (
    "Error 10197, reqId 253: No market data during competing live session, "
    "contract: Stock(conId=267265, symbol='CASY', exchange='SMART', "
    "primaryExchange='NASDAQ', currency='USD', localSymbol='CASY', "
    "tradingClass='CASY')"
)


def test_normalize_strips_ib_contract_identifiers():
    """IB 10197 errors that differ only by reqId/conId/symbol normalize identically."""
    n1 = normalize_message_for_signature(_IB_10197_D)
    n2 = normalize_message_for_signature(_IB_10197_LLY)
    n3 = normalize_message_for_signature(_IB_10197_CASY)
    assert n1 == n2 == n3
    # Error code must survive normalization — we dedup per incident, not per family
    assert "10197" in n1


def test_normalize_preserves_distinct_error_codes():
    """Different IB error codes must not collapse into the same signature."""
    msg_10197 = "Error 10197, reqId 1: foo"
    msg_200 = "Error 200, reqId 1: bar"
    assert compute_signature_from_message(msg_10197) != compute_signature_from_message(msg_200)


def test_signature_from_message_dedups_ib_fixtures():
    """compute_signature_from_message collapses the three real IB 10197 messages."""
    s1 = compute_signature_from_message(_IB_10197_D)
    s2 = compute_signature_from_message(_IB_10197_LLY)
    s3 = compute_signature_from_message(_IB_10197_CASY)
    assert s1 == s2 == s3


def test_normalize_uuids_and_request_ids():
    """UUIDs and AWS-style request IDs normalize away."""
    uuid_a = "failed: task 550e8400-e29b-41d4-a716-446655440000 timed out"
    uuid_b = "failed: task 123e4567-e89b-12d3-a456-426614174000 timed out"
    assert normalize_message_for_signature(uuid_a) == normalize_message_for_signature(uuid_b)

    req_a = "S3 error request_id=ABC123DEF456 access denied"
    req_b = "S3 error request_id=XYZ789QWE012 access denied"
    assert normalize_message_for_signature(req_a) == normalize_message_for_signature(req_b)


_NAV_SERIES_REFUSAL_A = (
    "nav_series point refused — Event timestamp 2026-07-06T20:00:16.706630+00:00 "
    "belongs to session 2026-07-07, not the labeled session 2026-07-06 — "
    "refusing to write a mis-keyed session artifact."
)
_NAV_SERIES_REFUSAL_B = (
    "nav_series point refused — Event timestamp 2026-07-06T20:15:22.415495+00:00 "
    "belongs to session 2026-07-07, not the labeled session 2026-07-06 — "
    "refusing to write a mis-keyed session artifact."
)
_NAV_SERIES_REFUSAL_DIFFERENT_SESSION = (
    "nav_series point refused — Event timestamp 2026-07-06T20:15:22.415495+00:00 "
    "belongs to session 2026-07-08, not the labeled session 2026-07-06 — "
    "refusing to write a mis-keyed session artifact."
)


def test_normalize_strips_iso_event_timestamps():
    """Repeated log errors differing only by embedded event time collapse."""
    n1 = normalize_message_for_signature(_NAV_SERIES_REFUSAL_A)
    n2 = normalize_message_for_signature(_NAV_SERIES_REFUSAL_B)
    assert n1 == n2
    assert "DT" in n1
    assert "2026-07-07" in n1
    assert "2026-07-06" in n1


def test_signature_from_message_dedups_nav_series_fixture():
    """Per-minute nav_series refusals share one signature (executor 2026-07-06)."""
    s1 = compute_signature_from_message(_NAV_SERIES_REFUSAL_A)
    s2 = compute_signature_from_message(_NAV_SERIES_REFUSAL_B)
    assert s1 == s2


def test_signature_from_message_preserves_distinct_session_labels():
    """Different session labels must not collapse into one signature."""
    s_same = compute_signature_from_message(_NAV_SERIES_REFUSAL_A)
    s_diff = compute_signature_from_message(_NAV_SERIES_REFUSAL_DIFFERENT_SESSION)
    assert s_same != s_diff


def test_dedup_increment():
    """Dedup hit should increment the counter."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = SQLiteStorage(f.name)
        store.init_schema()

        report = Report(
            flow_name="test",
            error_message="boom",
            error_signature="sig123",
        )
        store.save_report(report)

        checker = DedupChecker(store, cooldown_minutes=60)
        checker.record_dedup_hit(report.id)

        # Check the count was incremented
        reports = store.get_reports(limit=1)
        assert reports[0].dedup_count == 2

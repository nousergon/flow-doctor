"""
Smoke test for Flow Doctor (0.5.0rc+ surface).

Exercises the recommended FlowDoctor.builder() entry point, the typed
TelegramNotifierConfig (without actually firing — no real bot token
required), the flow_doctor.context() contextvars layer, the
report_async() coroutine, and the historical report-handling features
(guard / monitor / dedup / capture_logs / secret scrubbing /
never-crash-the-caller). All sqlite, no network.

Run from the flow-doctor repo root::

    python examples/smoke_test.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

import flow_doctor
from flow_doctor import (
    EmailNotifierConfig,
    FlowDoctor,
    FlowDoctorProtocol,
    TelegramNotifierConfig,
)

db_path = os.path.join(tempfile.gettempdir(), "fd_smoke_test.db")
if os.path.exists(db_path):
    os.remove(db_path)

# Skip notifier preflight network calls — this smoke test runs offline
# and the fake credentials below would otherwise trip /getMe and the
# Gmail SMTP banner. Downstream consumers should NOT set this in prod.
os.environ["FLOW_DOCTOR_SKIP_PREFLIGHT"] = "1"

# ---------------------------------------------------------------------------
# Build a FlowDoctor with the recommended (0.5.0rc+) builder API.
# Telegram is the recommended default; we wire fake creds since the
# smoke test stays offline.
# ---------------------------------------------------------------------------
fd: FlowDoctorProtocol = (
    FlowDoctor.builder("smoke-test")
    .with_repo("your-org/your-repo", owner="@your-username")
    .with_store(path=db_path)
    .with_dedup(cooldown_minutes=60)
    .add_notifier(
        TelegramNotifierConfig(
            bot_token="123:fake-smoke-token",
            chat_id=-1001234567890,
            # message_thread_id=42,  # optional: forum-topic routing
        )
    )
    # A second notifier to demonstrate the discriminated union — also
    # fake creds since we stay offline.
    .add_notifier(
        EmailNotifierConfig(
            sender="alerts@example.com",
            recipients=["oncall@example.com"],
            smtp_password="fake-app-password",
        )
    )
    .build()
)

print("=" * 60)
print("FLOW DOCTOR — SMOKE TEST (0.5.0rc+ surface)")
print("=" * 60)
print(f"Built via: FlowDoctor.builder()  →  satisfies FlowDoctorProtocol: "
      f"{isinstance(fd, FlowDoctorProtocol)}")
print(f"Notifiers: {len(fd.config.notify)} "
      f"({', '.join(n.type for n in fd.config.notify)})")


# --- Test 1: Exception report ---
print("\n--- Test 1: Exception report ---")
try:
    raise KeyError("RSI_14")
except Exception as e:
    report_id = fd.report(e)
    print(f"  Report ID: {report_id}")
    assert report_id is not None, "Expected a report ID"


# --- Test 2: guard() re-raises ---
print("\n--- Test 2: guard() context manager ---")
try:
    with fd.guard():
        raise ValueError("bad data from yfinance")
except ValueError as e:
    print(f"  Guard re-raised correctly: {e}")
else:
    raise AssertionError("guard() should have re-raised")


# --- Test 3: monitor() decorator ---
print("\n--- Test 3: @monitor decorator ---")

@fd.monitor
def failing_function():
    raise RuntimeError("Lambda timeout after 300s")

try:
    failing_function()
except RuntimeError as e:
    print(f"  Monitor re-raised correctly: {e}")
else:
    raise AssertionError("monitor() should have re-raised")


# --- Test 4: Dedup suppression ---
print("\n--- Test 4: Dedup (5 identical errors → 1 report) ---")
dedup_results = []
for i in range(5):
    try:
        raise KeyError("RSI_14")
    except Exception as e:
        result = fd.report(e)
        dedup_results.append(result)
        status = "NEW" if result else "DEDUPED"
        print(f"  Attempt {i+1}: {status}")

new_count = sum(1 for r in dedup_results if r is not None)
dedup_count = sum(1 for r in dedup_results if r is None)
print(f"  → {new_count} new, {dedup_count} deduped")


# --- Test 5: flow_doctor.context() contextvars propagation ---
print("\n--- Test 5: flow_doctor.context() ambient propagation ---")
with flow_doctor.context(flow_name="smoke-test-am", stage="ingest", run_id="run-42"):
    try:
        raise RuntimeError("ingest stage failed")
    except Exception as e:
        report_id = fd.report(e)

# Verify the context landed on the persisted report.
recent = fd.history(limit=1)[0]
print(f"  Report flow_name: {recent.context.get('flow_name')}")
print(f"  Report stage:     {recent.context.get('stage')}")
print(f"  Report run_id:    {recent.context.get('run_id')}")
assert recent.context.get("stage") == "ingest"
assert recent.context.get("run_id") == "run-42"


# --- Test 6: report_async() ---
print("\n--- Test 6: report_async() from an asyncio context ---")
async def async_pipeline():
    try:
        raise TimeoutError("async pipeline tripped a deadline")
    except Exception as e:
        return await fd.report_async(e)

async_report_id = asyncio.run(async_pipeline())
print(f"  Async report ID: {async_report_id}")
assert async_report_id is not None


# --- Test 7: capture_logs() ---
print("\n--- Test 7: Log capture ---")
logger = logging.getLogger("test.scanner")
with fd.capture_logs(level=logging.INFO):
    logger.info("Starting scanner with 900 tickers")
    logger.warning("yfinance rate limit approaching")
    try:
        raise ConnectionError("yfinance RSS feed timeout")
    except Exception as e:
        report_id = fd.report(e)
        print(f"  Report with logs: {report_id}")


# --- Test 8: Secret scrubbing ---
print("\n--- Test 8: Secret scrubbing ---")
try:
    api_key = "AKIAIOSFODNN7EXAMPLE"
    raise RuntimeError(f"S3 auth failed with key {api_key}")
except Exception as e:
    report_id = fd.report(
        e,
        context={
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG",
            "tickers_scanned": 900,
        },
    )
    print(f"  Scrubbed report ID: {report_id}")


# --- Test 9: report() never crashes its caller ---
print("\n--- Test 9: report() never crashes caller ---")
broken_fd = (
    FlowDoctor.builder("broken-flow")
    .with_store(path="/nonexistent/impossible/path/db.sqlite")
    .build(strict=False)  # degraded mode — store init will fail loudly
)
try:
    raise RuntimeError("this should not crash")
except Exception as e:
    result = broken_fd.report(e)
    print(f"  Broken store report result: {result} (None is OK)")
print("  Caller survived — report() did not propagate")


# --- Test 10: OTel serialization ---
print("\n--- Test 10: OTel SpanEvent serialization ---")
from flow_doctor.otel import report_to_otel_span_event

span_event = report_to_otel_span_event(recent)  # from test 5
print(f"  resource.service.name: {span_event['resource']['service.name']}")
print(f"  event.name:            {span_event['name']}")
print(f"  severity_text:         {span_event['severity_text']}")
print(f"  attributes.context.run_id: "
      f"{span_event['attributes'].get('context.run_id')}")


# --- History ---
print("\n--- Report History ---")
for r in fd.history(limit=20):
    dedup_str = f" (dedup x{r.dedup_count})" if r.dedup_count > 1 else ""
    print(f"  [{r.severity:8s}] {r.error_type or 'msg'}: "
          f"{r.error_message[:60]}{dedup_str}")

print("\n" + "=" * 60)
print("ALL SMOKE TESTS PASSED")
print(f"DB at: {db_path}")
print("=" * 60)

"""Tests for rate limiting and cascade detection."""

import tempfile
from datetime import datetime, timedelta

from flow_doctor.core.config import RateLimitConfig
from flow_doctor.core.models import Action, ActionStatus, ActionType, Report
from flow_doctor.core.rate_limiter import CascadeDetector, RateLimiter
from flow_doctor.storage.sqlite import SQLiteStorage


def _make_store():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = SQLiteStorage(f.name)
    store.init_schema()
    return store


def test_rate_limiter_allow():
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=5)
    rl = RateLimiter(store, config)

    assert rl.check("slack_alert") == "allow"


def test_rate_limiter_degrade():
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=2)
    rl = RateLimiter(store, config)

    # Create a report to reference
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)

    # Add 2 actions to hit the limit
    for _ in range(2):
        store.save_action(Action(
            report_id=report.id,
            action_type="slack_alert",
            status=ActionStatus.SENT.value,
        ))

    assert rl.check("slack_alert") == "degrade"


def test_rate_limiter_different_action_types():
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=1, max_diagnosed_per_day=3)
    rl = RateLimiter(store, config)

    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)

    store.save_action(Action(
        report_id=report.id,
        action_type="slack_alert",
        status=ActionStatus.SENT.value,
    ))

    assert rl.check("slack_alert") == "degrade"
    assert rl.check("diagnosis") == "allow"


def test_cascade_detector_no_dependencies():
    store = _make_store()
    detector = CascadeDetector(store)
    result = detector.check_cascade([], "my-flow")
    assert result is None


def test_cascade_detector_no_upstream_failure():
    store = _make_store()
    detector = CascadeDetector(store)
    result = detector.check_cascade(["upstream-flow"], "my-flow")
    assert result is None


def test_cascade_detector_upstream_failure():
    store = _make_store()
    # Record an upstream failure
    store.save_report(Report(
        flow_name="research-lambda",
        error_message="research failed",
        severity="error",
    ))

    detector = CascadeDetector(store)
    result = detector.check_cascade(["research-lambda"], "predictor-training")
    assert result == "research-lambda"


def test_cascade_detector_upstream_warning_not_cascade():
    store = _make_store()
    # Record an upstream warning (not a failure)
    store.save_report(Report(
        flow_name="research-lambda",
        error_message="just a warning",
        severity="warning",
    ))

    detector = CascadeDetector(store)
    result = detector.check_cascade(["research-lambda"], "predictor-training")
    assert result is None


# ── telegram_alert budget regression (2026-07-28 fleet alert blackout) ────────
#
# telegram_alert and s3_alert shipped as notifiers but were never added to
# RateLimiter.limits, so `limits.get(action, 10)` silently gave them a
# hardcoded 10/day while the configured max_alerts_per_day reached only
# slack/email. Telegram was the ONLY channel that fleet read. The shared budget
# burned out early each day, so terminal pipeline notifications (late in the
# cycle) were systematically dropped while start-of-run pings got through —
# 12 of 13 terminal notifications suppressed across two days, including two
# consecutive production trading-pipeline failures that paged and were never
# seen.


def test_every_action_type_has_a_configured_budget():
    """The generative defect: a notifier ships, its ActionType is never added
    to the budget map, and it silently inherits a small hardcoded default.
    Asserting over the enum means a NEW ActionType cannot regress this."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig())
    missing = {a.value for a in ActionType} - set(rl.limits)
    assert not missing, (
        f"ActionType(s) {sorted(missing)} have no rate-limit budget and would "
        f"fall through to the unmapped path. Add them to _ALERT_ACTIONS or "
        f"_ISSUE_ACTIONS in flow_doctor/core/rate_limiter.py."
    )


def test_telegram_and_s3_alerts_use_the_configured_alert_budget():
    """Not a hardcoded default — the value the operator actually configured."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=100))
    assert rl.limits[ActionType.TELEGRAM_ALERT.value] == 100
    assert rl.limits[ActionType.S3_ALERT.value] == 100
    # Regression pin: the old code produced 10 here regardless of config.
    assert rl.limits[ActionType.TELEGRAM_ALERT.value] != 10


def test_unmapped_action_fails_open_not_to_a_silent_small_default():
    """An extra alert costs noise; a dropped one costs an outage. An unknown
    action must never be silently capped."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=1))
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)
    for _ in range(50):
        store.save_action(Action(
            report_id=report.id,
            action_type="brand_new_notifier",
            status=ActionStatus.SENT.value,
        ))
    assert rl.check("brand_new_notifier") == "allow"


def test_failure_severities_are_exempt_from_the_daily_cap():
    """A rate limiter that can drop a failure page is an outage amplifier.
    Repeats of the SAME failure are suppressed by signature dedup upstream, so
    anything reaching the limiter at error/critical is a DISTINCT failure."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=1))
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)
    for _ in range(5):
        store.save_action(Action(
            report_id=report.id,
            action_type=ActionType.TELEGRAM_ALERT.value,
            status=ActionStatus.SENT.value,
        ))

    # Budget is blown...
    assert rl.check(ActionType.TELEGRAM_ALERT.value) == "degrade"
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="info") == "degrade"
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="warning") == "degrade"
    # ...but failures still get through.
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="error") == "allow"
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="critical") == "allow"


def test_exemption_is_configurable_and_can_be_disabled():
    """Setting it to [] restores the pre-0.8.8 cap-everything behaviour."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(
        max_alerts_per_day=1, rate_limit_exempt_severities=[]
    ))
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)
    for _ in range(5):
        store.save_action(Action(
            report_id=report.id,
            action_type=ActionType.TELEGRAM_ALERT.value,
            status=ActionStatus.SENT.value,
        ))
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="error") == "degrade"


def _alert(flow=None):
    return Action(
        report_id="r",
        action_type=ActionType.EMAIL_ALERT.value,
        status=ActionStatus.SENT.value,
        flow_name=flow,
    )


def test_budget_is_scoped_per_flow():
    """One store, two flows, one nominal budget each — and they must not draw
    on each other's.

    The budget reads as per-component in every config file. Until `flow_name`
    was threaded through, the count behind it was not, so five flows on one
    `flow-doctor-store` table shared a single budget
    (alpha-engine-config-I6921). `check`'s own docstring already named "a store
    SHARED by every consumer" as part of the 2026-07-28 blackout; this is the
    half of that which was never closed.
    """
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=2)

    noisy = RateLimiter(store, config, flow_name="executor")
    quiet = RateLimiter(store, config, flow_name="research-lambda")

    for _ in range(5):
        store.save_action(_alert("executor"))

    assert noisy.check(ActionType.EMAIL_ALERT.value) == "degrade"
    # The whole point: a noisy neighbour must not spend this flow's budget.
    assert quiet.check(ActionType.EMAIL_ALERT.value) == "allow"


def test_unscoped_limiter_keeps_the_old_global_meaning():
    """A caller that has not been updated must not silently start counting
    under a null flow — it keeps counting everything, as before."""
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=2)
    rl = RateLimiter(store, config)

    for flow in ("executor", "research-lambda", "data-collector"):
        store.save_action(_alert(flow))

    assert rl.check(ActionType.EMAIL_ALERT.value) == "degrade"


def test_a_degrade_is_announced(caplog):
    """Suppressed alerting is the failure mode this subsystem exists to
    prevent. The previous silent path made a two-day blackout
    indistinguishable from a quiet stretch."""
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=1)
    rl = RateLimiter(store, config, flow_name="executor")
    store.save_action(_alert("executor"))

    with caplog.at_level("WARNING"):
        assert rl.check(ActionType.EMAIL_ALERT.value) == "degrade"

    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "executor" in msg and "DEGRADED" in msg


def test_the_first_crossing_reports_at_ERROR(caplog):
    """The saturation signal (alpha-engine-config-I6927).

    `krepis.logging.setup_logging` attaches its alert handler to the root
    logger at ERROR, so a WARNING lands in logs nobody reads during an
    incident. The first crossing must REACH someone — that is the condition
    under which this cap was kept rather than removed.
    """
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=2)
    rl = RateLimiter(store, config, flow_name="executor")
    for _ in range(2):
        store.save_action(_alert("executor"))

    with caplog.at_level("DEBUG"):
        assert rl.check(ActionType.EMAIL_ALERT.value) == "degrade"

    errors = [r for r in caplog.records if r.levelno >= 40]
    assert errors, "the first crossing must be reportable, not just logged"
    assert "2/2" in errors[0].getMessage()


def test_subsequent_degrades_stay_at_WARNING(caplog):
    """One saturation notice per (flow, action) per day, not one per suppressed
    alert. A signal that repeats on every occurrence becomes the noise the cap
    exists to prevent."""
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=2)
    rl = RateLimiter(store, config, flow_name="executor")
    for _ in range(5):  # already well past the limit
        store.save_action(_alert("executor"))

    with caplog.at_level("DEBUG"):
        assert rl.check(ActionType.EMAIL_ALERT.value) == "degrade"

    assert not [r for r in caplog.records if r.levelno >= 40]
    assert [r for r in caplog.records if r.levelno == 30]


def test_the_saturation_signal_cannot_recurse():
    """The report this ERROR generates is itself severity=error, which is
    exempt by default — so it returns 'allow' and never re-enters the degrade
    branch. Pinned because a self-triggering alert is the obvious way this
    design goes wrong."""
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=1)
    rl = RateLimiter(store, config, flow_name="executor")
    store.save_action(_alert("executor"))

    assert rl.check(ActionType.EMAIL_ALERT.value, severity="error") == "allow"


def test_each_flow_gets_its_own_saturation_notice(caplog):
    """Scoping applies to the signal too: one flow crossing its budget must not
    consume another flow's notice."""
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=1)
    for _ in range(3):
        store.save_action(_alert("executor"))
    store.save_action(_alert("data-collector"))

    with caplog.at_level("DEBUG"):
        RateLimiter(store, config, flow_name="executor").check(
            ActionType.EMAIL_ALERT.value
        )
        errors_after_noisy = [r for r in caplog.records if r.levelno >= 40]
        RateLimiter(store, config, flow_name="data-collector").check(
            ActionType.EMAIL_ALERT.value
        )

    # executor is past its first crossing (3 > 1) so it is quiet; the
    # data-collector is exactly at it (1 == 1) and must report.
    assert not errors_after_noisy
    assert [r for r in caplog.records if r.levelno >= 40]


def test_an_allow_stays_quiet(caplog):
    """The warning must fire on the degrade, not on every check — an alerting
    library that logs a WARNING per delivered alert is its own noise source."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=5), flow_name="x")

    with caplog.at_level("WARNING"):
        assert rl.check(ActionType.EMAIL_ALERT.value) == "allow"

    assert not [r for r in caplog.records if "DEGRADED" in r.getMessage()]


def test_exempt_severities_still_bypass_the_scoped_budget():
    """The 0.8.8 exemption is what keeps a page from ever being capped, and it
    must survive the scoping change — it is the reason I6921 was a P2 and not
    an incident."""
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=1)
    rl = RateLimiter(store, config, flow_name="executor")
    for _ in range(9):
        store.save_action(_alert("executor"))

    assert rl.check(ActionType.EMAIL_ALERT.value, severity="error") == "allow"
    assert rl.check(ActionType.EMAIL_ALERT.value, severity="critical") == "allow"
    assert rl.check(ActionType.EMAIL_ALERT.value, severity="warning") == "degrade"


def test_failed_deliveries_do_not_spend_the_alert_budget():
    """alpha-engine-config-I12711: on 2026-10-26 seven FAILED executor sends
    spent the 10/day `telegram_alert` budget, and the cap then held a trade
    alert. A delivery that reached nobody is not an alert the cap counts."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=2), flow_name="executor")
    for _ in range(5):
        store.save_action(Action(
            report_id="r", action_type="telegram_alert",
            status=ActionStatus.FAILED.value, flow_name="executor",
        ))
    assert rl.check("telegram_alert") == "allow"

    store.save_action(Action(
        report_id="r", action_type="telegram_alert",
        status=ActionStatus.SENT.value, flow_name="executor",
    ))
    assert rl.check("telegram_alert") == "allow"


def test_degraded_rows_still_count_toward_the_budget():
    """The first-crossing ERROR (`today_count == limit`) relies on DEGRADED rows
    pushing the count past the limit, so they must stay in the count."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=2), flow_name="executor")
    for status in (ActionStatus.SENT.value, ActionStatus.DEGRADED.value):
        store.save_action(Action(
            report_id="r", action_type="telegram_alert",
            status=status, flow_name="executor",
        ))
    assert rl.check("telegram_alert") == "degrade"


def test_failed_diagnoses_still_spend_the_diagnosis_budget():
    """A FAILED diagnosis still made an LLM call, so only alert budgets leave
    FAILED rows out; the diagnosis cap keeps counting them."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_diagnosed_per_day=2), flow_name="executor")
    for _ in range(2):
        store.save_action(Action(
            report_id="r", action_type="diagnosis",
            status=ActionStatus.FAILED.value, flow_name="executor",
        ))
    assert rl.check("diagnosis") == "degrade"

"""Integration test: full report → diagnose → decide → remediate pipeline.

Tests the complete flow through FlowDoctor with remediation enabled,
using mocked LLM responses and AWS clients.
"""

import json
import sqlite3
import tempfile

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

import flow_doctor
from flow_doctor.core.models import Diagnosis
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext
from flow_doctor.diagnosis.provider import DiagnosisProvider
from flow_doctor.remediation.decision_gate import DecisionType
from flow_doctor.remediation.executor import ExecutionResult, RemediationExecutor
from flow_doctor.remediation.playbook import Playbook, RemediationType


# ── Executor Unit Tests ──────────────────────────────────────────────────────


class TestRemediationExecutor:

    def test_dry_run_logs_without_executing(self):
        """Dry-run mode should succeed without calling AWS."""
        from flow_doctor.remediation.decision_gate import Decision, DecisionGate, GateConfig
        from flow_doctor.remediation.playbook import PlaybookPattern, RemediationAction

        executor = RemediationExecutor(dry_run=True)

        diagnosis = Diagnosis(
            report_id="r1", flow_name="executor-planner",
            category="INFRA", root_cause="IB Gateway stale session",
            confidence=0.95,
        )
        action = RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="Restart IB Gateway",
            commands=["sudo systemctl restart ibgateway", "sleep 30"],
            ssm_target="ae-trading",
        )
        pattern = PlaybookPattern(
            name="ib_gateway_stale_session",
            description="IB Gateway stale session",
            category="INFRA",
            action=action,
        )
        decision = Decision(
            decision_type=DecisionType.AUTO_REMEDIATE,
            reason="Matched playbook",
            diagnosis=diagnosis,
            playbook_match=pattern,
            action=action,
        )

        result = executor.execute(decision)
        assert result.success is True
        assert result.dry_run is True
        assert len(result.commands_run) == 2
        assert "DRY RUN" in result.output

    def test_non_auto_remediate_returns_error(self):
        """Non-auto-remediate decisions should not execute."""
        executor = RemediationExecutor(dry_run=True)

        diagnosis = Diagnosis(
            report_id="r1", flow_name="test",
            category="CODE", root_cause="bug",
            confidence=0.9,
        )
        decision = MagicMock()
        decision.decision_type = DecisionType.ESCALATE
        decision.diagnosis = diagnosis

        result = executor.execute(decision)
        assert result.success is False

    def test_audit_trail_saved(self, tmp_path):
        """Execution results should be persisted to SQLite."""
        from flow_doctor.storage.sqlite import SQLiteStorage
        from flow_doctor.remediation.decision_gate import Decision
        from flow_doctor.remediation.playbook import PlaybookPattern, RemediationAction

        db_path = str(tmp_path / "test.db")
        store = SQLiteStorage(db_path)
        store.init_schema()

        executor = RemediationExecutor(dry_run=True, store=store)

        diagnosis = Diagnosis(
            report_id="r1", flow_name="executor-planner",
            category="INFRA", root_cause="test",
            confidence=0.95,
        )
        action = RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="test restart",
            commands=["echo test"],
        )
        decision = Decision(
            decision_type=DecisionType.AUTO_REMEDIATE,
            reason="test",
            diagnosis=diagnosis,
            playbook_match=PlaybookPattern(
                name="test_pattern", description="test",
                category="INFRA", action=action,
            ),
            action=action,
        )

        executor.execute(decision)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM remediation_actions").fetchall()
        conn.close()
        assert len(rows) == 1


# ── Full Pipeline Integration Test ───────────────────────────────────────────


class TestFullPipeline:
    """Test the complete report() → diagnose → decide → remediate flow."""

    def _make_fd(self, tmp_path, diagnosis_enabled=False, remediation_enabled=True):
        """Create a FlowDoctor instance with remediation enabled."""
        db_path = str(tmp_path / "flow_doctor_pipeline.db")

        fd = flow_doctor.FlowDoctor.from_config(
            flow_name="executor-planner",
            repo="test-org/test-repo",
            store={"type": "sqlite", "path": db_path},
            notify=[],
            dependencies=["predictor-training", "data-phase1"],
            rate_limits={"max_alerts_per_day": 50, "dedup_cooldown_minutes": 1},
            remediation={
                "enabled": remediation_enabled,
                "dry_run": True,
                "market_hours_lockout": False,
            },
        )
        return fd, db_path

    def test_report_with_remediation_enabled(self, tmp_path):
        """report() should proceed without error when remediation is enabled."""
        fd, db_path = self._make_fd(tmp_path)

        try:
            raise RuntimeError("No market data during competing live session (error 10197)")
        except Exception as e:
            report_id = fd.report(e, severity="error", context={
                "site": "executor_planner"})

        assert report_id is not None

        # Check that the report was stored
        conn = sqlite3.connect(db_path)
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.close()
        assert report is not None

    def test_remediation_audit_trail_created(self, tmp_path):
        """When diagnosis runs and decision gate fires, audit trail should exist."""
        fd, db_path = self._make_fd(tmp_path)

        # Manually inject a decision gate with a playbook that matches
        from flow_doctor.remediation.decision_gate import DecisionGate, GateConfig
        from flow_doctor.remediation.playbook import Playbook, PlaybookPattern, RemediationAction

        test_playbook = Playbook(patterns=[
            PlaybookPattern(
                name="ib_gateway_stale",
                description="IB Gateway stale session",
                category="INFRA",
                message_pattern=r"(10197|competing live session)",
                flow_names=["executor-planner"],
                action=RemediationAction(
                    action_type=RemediationType.RESTART_SERVICE,
                    description="Restart IB Gateway",
                    commands=["sudo systemctl restart ibgateway"],
                    safe_during_market_hours=False,
                ),
            ),
        ])

        gate = DecisionGate(
            playbook=test_playbook,
            config=GateConfig(
                market_open_hour=0, market_close_hour=0,
            ),
        )

        diagnosis = Diagnosis(
            report_id="test-r1", flow_name="executor-planner",
            category="INFRA", root_cause="IB Gateway stale session",
            confidence=0.95,
        )

        decision = gate.decide(
            diagnosis,
            error_type="RuntimeError",
            error_message="competing live session (error 10197)",
            flow_name="executor-planner",
        )

        assert decision.decision_type == DecisionType.AUTO_REMEDIATE

        # Execute in dry-run
        from flow_doctor.storage.sqlite import SQLiteStorage
        store = SQLiteStorage(db_path)
        store.init_schema()

        executor = RemediationExecutor(dry_run=True, store=store)
        result = executor.execute(decision)

        assert result.success is True
        assert result.dry_run is True

        # Verify audit trail
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM remediation_actions").fetchall()
        conn.close()
        assert len(rows) == 1

    def test_remediation_disabled_skips_gate(self, tmp_path):
        """When remediation is disabled, decision gate should not be initialized."""
        fd, _ = self._make_fd(tmp_path, remediation_enabled=False)
        assert fd._decision_gate is None
        assert fd._remediation_executor is None

    def test_report_never_crashes_with_remediation(self, tmp_path):
        """report() must never crash even if remediation has errors."""
        fd, _ = self._make_fd(tmp_path)

        # These should all succeed without raising
        fd.report("string error", severity="error")
        fd.report(None, severity="warning", message="manual warning")

        try:
            raise ValueError("test error")
        except Exception as e:
            result = fd.report(e, severity="critical")
            assert result is not None or result is None  # just no crash

    def test_decision_gate_routes_all_five_failures(self, tmp_path):
        """Verify the 5 failure types are routed correctly with a test playbook."""
        from flow_doctor.remediation.decision_gate import DecisionGate, GateConfig
        from flow_doctor.remediation.playbook import Playbook, PlaybookPattern, RemediationAction

        test_playbook = Playbook(patterns=[
            PlaybookPattern(
                name="step_function_denied",
                description="Step Function access denied",
                category="CONFIG",
                message_pattern=r"(AccessDenied|states:StartExecution|not authorized)",
                flow_names=["weekday-pipeline"],
                action=RemediationAction(
                    action_type=RemediationType.RERUN_STEP,
                    description="Retry Step Function",
                ),
            ),
            PlaybookPattern(
                name="ib_gateway_stale",
                description="IB Gateway stale session",
                category="INFRA",
                message_pattern=r"(10197|competing live session)",
                flow_names=["executor-planner"],
                action=RemediationAction(
                    action_type=RemediationType.RESTART_SERVICE,
                    description="Restart IB Gateway",
                    commands=["sudo systemctl restart ibgateway"],
                    safe_during_market_hours=False,
                ),
            ),
            PlaybookPattern(
                name="daemon_not_running",
                description="Daemon not running",
                category="INFRA",
                message_pattern=r"(daemon.*inactive|daemon.*not running)",
                flow_names=["executor-daemon"],
                action=RemediationAction(
                    action_type=RemediationType.RESTART_SERVICE,
                    description="Restart daemon",
                    commands=["sudo systemctl restart daemon"],
                    safe_during_market_hours=True,
                ),
            ),
        ])

        gate = DecisionGate(
            playbook=test_playbook,
            config=GateConfig(
                market_open_hour=0, market_close_hour=0,
                # This integration test exercises 3+ auto-remediation
                # decisions back-to-back. The default cap was lowered
                # 5 → 2 in the conservative-defaults revision; raise it
                # for this test so the routing logic is under test
                # rather than the rate limit.
                max_auto_remediations_per_day=10,
            ),
        )

        failures = [
            # (category, confidence, error_type, error_message, flow_name, expected_decision)
            ("CONFIG", 0.95, "ClientError", "states:StartExecution not authorized",
             "weekday-pipeline", DecisionType.AUTO_REMEDIATE),
            ("CODE", 0.9, None, "not a trading day",
             "weekday-pipeline", DecisionType.GENERATE_FIX_PR),
            ("INFRA", 0.95, "RuntimeError", "competing live session (error 10197)",
             "executor-planner", DecisionType.AUTO_REMEDIATE),
            ("INFRA", 0.95, "RuntimeError", "No valid price for any ticker",
             "executor-planner", DecisionType.ESCALATE),  # No playbook match
            ("INFRA", 0.95, None, "alpha-engine-daemon inactive",
             "executor-daemon", DecisionType.AUTO_REMEDIATE),
        ]

        for cat, conf, err_type, err_msg, flow, expected in failures:
            diagnosis = Diagnosis(
                report_id="test", flow_name=flow,
                category=cat, root_cause=err_msg[:50],
                confidence=conf,
            )
            decision = gate.decide(diagnosis, err_type, err_msg, flow)
            assert decision.decision_type == expected, (
                f"Failed for '{err_msg[:40]}': expected {expected}, got {decision.decision_type} "
                f"(reason: {decision.reason})"
            )


# ── End-to-End: report() → diagnose → decide → remediate ────────────────────


class _FakeProvider(DiagnosisProvider):
    """Diagnosis provider that returns a pre-configured Diagnosis."""

    def __init__(self, category: str, root_cause: str, confidence: float):
        self._category = category
        self._root_cause = root_cause
        self._confidence = confidence

    def diagnose(self, context: DiagnosisContext, assembler: ContextAssembler) -> Diagnosis:
        return Diagnosis(
            report_id="",  # set by caller
            flow_name=context.flow_name,
            category=self._category,
            root_cause=self._root_cause,
            confidence=self._confidence,
            remediation="Restart the service",
            auto_fixable=True,
            source="fake",
            tokens_used=100,
            cost_usd=0.001,
        )


class TestEndToEnd:
    """Full report() → diagnose → decide → remediate with mocked LLM."""

    def _make_fd_with_diagnosis(self, tmp_path, category, root_cause, confidence):
        """Create a FlowDoctor with a fake diagnosis provider and remediation enabled."""
        db_path = str(tmp_path / "e2e_test.db")

        fd = flow_doctor.FlowDoctor.from_config(
            flow_name="executor-planner",
            repo="test-org/test-repo",
            store={"type": "sqlite", "path": db_path},
            notify=[],
            dependencies=["predictor-training"],
            rate_limits={"max_alerts_per_day": 50, "dedup_cooldown_minutes": 1},
            diagnosis={"enabled": True, "api_key": "fake-key"},
            remediation={
                "enabled": True,
                "dry_run": True,
                "market_hours_lockout": False,
            },
        )

        # Inject fake provider (replaces the real AnthropicProvider that would
        # fail without a valid API key)
        fd._diagnosis_provider = _FakeProvider(category, root_cause, confidence)

        # Inject test playbook (no baked-in patterns in generic package)
        from flow_doctor.remediation.playbook import Playbook, PlaybookPattern, RemediationAction
        test_playbook = Playbook(patterns=[
            PlaybookPattern(
                name="ib_gateway_stale_session",
                description="IB Gateway stale session",
                category="INFRA",
                message_pattern=r"(10197|competing live session)",
                flow_names=["executor-planner"],
                action=RemediationAction(
                    action_type=RemediationType.RESTART_SERVICE,
                    description="Restart IB Gateway",
                    commands=["sudo systemctl restart ibgateway"],
                    safe_during_market_hours=False,
                ),
            ),
        ])
        if fd._decision_gate:
            fd._decision_gate.playbook = test_playbook

        return fd, db_path

    def test_ib_gateway_flows_through_to_auto_remediate(self, tmp_path):
        """IB Gateway error → diagnosis(INFRA, 0.95) → auto-remediate (dry-run)."""
        fd, db_path = self._make_fd_with_diagnosis(
            tmp_path,
            category="INFRA",
            root_cause="IB Gateway stale session — error 10197",
            confidence=0.95,
        )

        try:
            raise RuntimeError("No market data during competing live session (error 10197)")
        except Exception as e:
            report_id = fd.report(e, severity="error")

        assert report_id is not None

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Report stored
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        assert report is not None

        # Diagnosis stored
        diag = conn.execute(
            "SELECT * FROM diagnoses WHERE report_id = ?", (report_id,)
        ).fetchone()
        assert diag is not None
        assert diag["category"] == "INFRA"
        assert diag["source"] == "fake"

        # Remediation audit trail stored
        rem = conn.execute(
            "SELECT * FROM remediation_actions WHERE report_id = ?", (report_id,)
        ).fetchone()
        assert rem is not None
        assert rem["decision_type"] == "auto_remediate"
        assert rem["dry_run"] == 1
        assert rem["playbook_pattern"] == "ib_gateway_stale_session"

        conn.close()

    def test_code_bug_flows_through_to_generate_pr(self, tmp_path):
        """Code bug → diagnosis(CODE, 0.9) → generate_fix_pr decision."""
        fd, db_path = self._make_fd_with_diagnosis(
            tmp_path,
            category="CODE",
            root_cause="Holiday detection missing InProgress state",
            confidence=0.9,
        )

        try:
            raise ValueError("not a trading day — market closed")
        except Exception as e:
            report_id = fd.report(e, severity="error")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rem = conn.execute(
            "SELECT * FROM remediation_actions WHERE report_id = ?", (report_id,)
        ).fetchone()
        assert rem is not None
        assert rem["decision_type"] == "generate_fix_pr"
        conn.close()

    def test_low_confidence_flows_through_to_escalate(self, tmp_path):
        """Low-confidence diagnosis → escalate decision."""
        fd, db_path = self._make_fd_with_diagnosis(
            tmp_path,
            category="CODE",
            root_cause="Something unclear",
            confidence=0.5,
        )

        try:
            raise RuntimeError("unexpected failure")
        except Exception as e:
            report_id = fd.report(e, severity="error")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rem = conn.execute(
            "SELECT * FROM remediation_actions WHERE report_id = ?", (report_id,)
        ).fetchone()
        assert rem is not None
        assert rem["decision_type"] == "escalate"
        conn.close()

    def test_warning_skips_diagnosis_and_remediation(self, tmp_path):
        """Warnings should skip diagnosis entirely (no LLM call, no gate)."""
        fd, db_path = self._make_fd_with_diagnosis(
            tmp_path, category="CODE", root_cause="irrelevant", confidence=0.9,
        )

        try:
            raise ValueError("minor issue")
        except Exception as e:
            report_id = fd.report(e, severity="warning")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        diag = conn.execute(
            "SELECT * FROM diagnoses WHERE report_id = ?", (report_id,)
        ).fetchone()
        # Warnings skip diagnosis
        assert diag is None

        rem = conn.execute(
            "SELECT * FROM remediation_actions WHERE report_id = ?", (report_id,)
        ).fetchone()
        # No remediation either
        assert rem is None
        conn.close()

    def test_full_pipeline_stores_cost_tracking(self, tmp_path):
        """Diagnosis cost (tokens, USD) should be persisted."""
        fd, db_path = self._make_fd_with_diagnosis(
            tmp_path, category="INFRA", root_cause="OOM", confidence=0.95,
        )

        try:
            raise MemoryError("Cannot allocate memory")
        except Exception as e:
            report_id = fd.report(e, severity="critical")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        diag = conn.execute(
            "SELECT tokens_used, cost_usd, llm_model FROM diagnoses WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        assert diag is not None
        assert diag["tokens_used"] == 100
        assert diag["cost_usd"] == 0.001
        conn.close()

    def test_status_returns_healthy_summary(self, tmp_path):
        """status() should report healthy state and zero counts on fresh init."""
        fd, _ = self._make_fd_with_diagnosis(
            tmp_path, category="INFRA", root_cause="test", confidence=0.95,
        )
        s = fd.status()
        assert s["healthy"] is True
        assert s["flow_name"] == "executor-planner"
        assert s["reports_today"] == 0
        assert s["diagnoses_today"] == 0
        assert s["diagnosis_cost_today_usd"] == 0.0

    def test_status_reflects_activity(self, tmp_path):
        """status() should reflect reports and diagnoses after report() is called."""
        fd, _ = self._make_fd_with_diagnosis(
            tmp_path, category="INFRA", root_cause="test", confidence=0.95,
        )

        try:
            raise RuntimeError("test error for status")
        except Exception as e:
            fd.report(e, severity="error")

        s = fd.status()
        assert s["reports_today"] >= 1
        assert s["diagnoses_today"] >= 1
        assert s["diagnosis_cost_today_usd"] > 0

    def test_log_summary_returns_string(self, tmp_path):
        """log_summary() should return a readable one-liner."""
        fd, _ = self._make_fd_with_diagnosis(
            tmp_path, category="INFRA", root_cause="test", confidence=0.95,
        )
        summary = fd.log_summary()
        assert "flow-doctor" in summary
        assert "executor-planner" in summary
        assert "reports=" in summary

    def test_daily_cost_cap_blocks_diagnosis(self, tmp_path):
        """Once daily cost cap is exceeded, further diagnoses are degraded."""
        db_path = str(tmp_path / "cost_cap_test.db")

        # Use a tiny cost cap ($0.002) — our fake provider costs $0.001 per call
        fd = flow_doctor.FlowDoctor.from_config(
            flow_name="executor-planner",
            repo="test-org/test-repo",
            store={"type": "sqlite", "path": db_path},
            notify=[],
            rate_limits={
                "max_alerts_per_day": 50,
                "max_diagnosed_per_day": 50,
                "dedup_cooldown_minutes": 1,
            },
            diagnosis={
                "enabled": True,
                "api_key": "fake-key",
                "max_daily_cost_usd": 0.002,
            },
            remediation={"enabled": False},
        )
        fd._diagnosis_provider = _FakeProvider("INFRA", "test", 0.95)

        # First two calls should produce diagnoses ($0.001 each = $0.002 total)
        ids = []
        for exc_cls in (ValueError, TypeError, RuntimeError):
            try:
                raise exc_cls("test error")
            except Exception as e:
                ids.append(fd.report(e, severity="error"))

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        diag_count = conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0]
        degraded_count = conn.execute(
            "SELECT COUNT(*) FROM actions WHERE status = 'degraded' AND target LIKE '%cost cap%'"
        ).fetchone()[0]
        conn.close()

        # Should have some diagnoses and at least one degraded action
        assert diag_count >= 1, "At least one diagnosis should succeed"
        assert degraded_count >= 1, "Cost cap should block at least one diagnosis"

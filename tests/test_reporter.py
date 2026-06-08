"""Tests for the FlowDoctor client: report(), guard(), monitor(), capture_logs()."""

import logging
import tempfile

import flow_doctor
from flow_doctor.core.client import FlowDoctor
from flow_doctor.core.config import load_config


def _make_fd(db_path=None, **kwargs):
    """Create a FlowDoctor with a temp SQLite DB."""
    if db_path is None:
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
    config = load_config(
        flow_name=kwargs.get("flow_name", "test-flow"),
        store=f"sqlite://{db_path}",
        **{k: v for k, v in kwargs.items() if k != "flow_name"},
    )
    return FlowDoctor(config), db_path


class TestReport:
    def test_report_exception(self):
        fd, _ = _make_fd()
        try:
            raise ValueError("test error")
        except ValueError as e:
            report_id = fd.report(e)

        assert report_id is not None
        reports = fd.history(limit=1)
        assert len(reports) == 1
        assert reports[0].error_type == "ValueError"
        assert reports[0].error_message == "test error"

    def test_report_string_message(self):
        fd, _ = _make_fd()
        report_id = fd.report("Pipeline produced 0 results")
        assert report_id is not None

        reports = fd.history(limit=1)
        assert reports[0].error_message == "Pipeline produced 0 results"
        assert reports[0].error_type is None

    def test_report_with_severity(self):
        fd, _ = _make_fd()
        report_id = fd.report("Low candidate count", severity="warning")
        assert report_id is not None

        reports = fd.history(limit=1)
        assert reports[0].severity == "warning"

    def test_report_with_context(self):
        fd, _ = _make_fd()
        report_id = fd.report(
            "Scanner returned 0 candidates",
            severity="warning",
            context={"tickers_scanned": 900, "candidates_found": 0},
        )
        assert report_id is not None

        reports = fd.history(limit=1)
        assert reports[0].context["user"]["tickers_scanned"] == 900

    def test_report_never_crashes(self):
        """report() must NEVER crash the caller, even with bad input."""
        fd, _ = _make_fd()

        # None error
        result = fd.report(None)
        # Arbitrary object
        result = fd.report(42)
        # Should not raise
        assert True

    def test_report_dedup_suppresses(self):
        """Second report with same error signature should be suppressed."""
        fd, _ = _make_fd()

        # Use string reports for deterministic signatures
        id1 = fd.report("Identical error message", severity="error")
        id2 = fd.report("Identical error message", severity="error")

        assert id1 is not None
        assert id2 is None  # suppressed by dedup

    def test_report_scrubs_secrets(self):
        """Secrets in traceback and context should be scrubbed."""
        fd, _ = _make_fd()
        try:
            key = "AKIAIOSFODNN7EXAMPLE"
            raise ValueError(f"Failed with key {key}")
        except ValueError as e:
            fd.report(e)

        reports = fd.history(limit=1)
        assert "AKIAIOSFODNN7EXAMPLE" not in (reports[0].traceback or "")


class TestGuard:
    def test_guard_reports_and_reraises(self):
        fd, _ = _make_fd()
        raised = False
        try:
            with fd.guard():
                raise RuntimeError("guard test")
        except RuntimeError:
            raised = True

        assert raised, "guard() must re-raise the original exception"
        reports = fd.history(limit=1)
        assert len(reports) == 1
        assert reports[0].error_type == "RuntimeError"

    def test_guard_no_error(self):
        fd, _ = _make_fd()
        with fd.guard():
            x = 1 + 1
        # No report should be created
        reports = fd.history(limit=1)
        assert len(reports) == 0

    def test_guard_reraises_exact_exception(self):
        """guard() must re-raise the ORIGINAL exception, not a FlowDoctor one."""
        fd, _ = _make_fd()
        original = ValueError("original error")
        caught = None
        try:
            with fd.guard():
                raise original
        except ValueError as e:
            caught = e

        assert caught is original


class TestMonitor:
    def test_monitor_reports_and_reraises(self):
        fd, _ = _make_fd()

        @fd.monitor
        def failing_func():
            raise KeyError("missing key")

        raised = False
        try:
            failing_func()
        except KeyError:
            raised = True

        assert raised, "@monitor must re-raise the original exception"
        reports = fd.history(limit=1)
        assert len(reports) == 1
        assert reports[0].error_type == "KeyError"

    def test_monitor_no_error(self):
        fd, _ = _make_fd()

        @fd.monitor
        def good_func():
            return 42

        result = good_func()
        assert result == 42
        reports = fd.history(limit=1)
        assert len(reports) == 0

    def test_monitor_preserves_function_metadata(self):
        fd, _ = _make_fd()

        @fd.monitor
        def my_handler(event, context):
            """Lambda handler docstring."""
            return event

        assert my_handler.__name__ == "my_handler"
        assert "Lambda handler" in my_handler.__doc__

    def test_monitor_reraises_exact_exception(self):
        fd, _ = _make_fd()
        original = TypeError("type error")

        @fd.monitor
        def bad():
            raise original

        caught = None
        try:
            bad()
        except TypeError as e:
            caught = e

        assert caught is original


class TestCaptureLogs:
    def test_capture_logs_basic(self):
        fd, _ = _make_fd()
        logger = logging.getLogger("test.capture")
        logger.setLevel(logging.DEBUG)

        with fd.capture_logs(level=logging.INFO, logger_name="test.capture"):
            logger.info("Starting scan...")
            logger.warning("Low results")

            # Now report an error
            fd.report("Something went wrong", severity="error")

        reports = fd.history(limit=1)
        assert reports[0].logs is not None
        assert "Starting scan" in reports[0].logs
        assert "Low results" in reports[0].logs

    def test_capture_logs_does_not_mutate_logger(self):
        """capture_logs should not change existing handler configuration."""
        fd, _ = _make_fd()
        logger = logging.getLogger("test.nomutate")
        original_handlers = list(logger.handlers)

        with fd.capture_logs(logger_name="test.nomutate"):
            pass

        assert logger.handlers == original_handlers

    def test_capture_logs_context_manager_cleanup(self):
        fd, _ = _make_fd()
        assert fd._log_handler is None

        with fd.capture_logs():
            assert fd._log_handler is not None

        assert fd._log_handler is None


class TestInit:
    def test_init_function(self):
        """flow_doctor.FlowDoctor.from_config() should return a FlowDoctor instance."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd = flow_doctor.FlowDoctor.from_config(
            flow_name="test-init",
            store=f"sqlite://{f.name}",
        )
        assert isinstance(fd, FlowDoctor)
        assert fd.config.flow_name == "test-init"


class TestCascade:
    def test_cascade_tags_report(self):
        """When upstream failed, downstream report should be tagged."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name

        # Create upstream flow doctor and report a failure
        upstream_fd, _ = _make_fd(db_path=db_path, flow_name="research-lambda")
        upstream_fd.report("Research pipeline failed", severity="error")

        # Create downstream flow doctor with dependency
        downstream_config = load_config(
            flow_name="predictor-training",
            store=f"sqlite://{db_path}",
            dependencies=["research-lambda"],
        )
        downstream_fd = FlowDoctor(downstream_config)

        # Report a downstream failure
        report_id = downstream_fd.report("Predictor training failed", severity="error")
        assert report_id is not None

        reports = downstream_fd.history(limit=1)
        assert reports[0].cascade_source == "research-lambda"

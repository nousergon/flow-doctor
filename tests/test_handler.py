"""Tests for FlowDoctorHandler — logging.Handler integration."""

import logging
import tempfile
import time
from unittest.mock import patch

from flow_doctor.core.client import FlowDoctor
from flow_doctor.core.config import load_config
from flow_doctor.core.handler import FlowDoctorHandler


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


def _flush(handler, timeout=2):
    """Shutdown handler and wait for queue to drain."""
    handler.shutdown(timeout=timeout)


class TestEmit:
    def test_emit_error_record(self):
        fd, _ = _make_fd()
        handler = FlowDoctorHandler(fd, level=logging.ERROR)
        logger = logging.getLogger("test.handler.emit")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            logger.error("Something broke")
        finally:
            logger.removeHandler(handler)
            _flush(handler)

        reports = fd.history(limit=10)
        assert len(reports) == 1
        assert reports[0].error_message == "Something broke"

    def test_emit_with_exc_info(self):
        fd, _ = _make_fd()
        handler = FlowDoctorHandler(fd, level=logging.ERROR)
        logger = logging.getLogger("test.handler.exc")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            try:
                raise ValueError("kaboom")
            except ValueError:
                logger.exception("Caught an error")
        finally:
            logger.removeHandler(handler)
            _flush(handler)

        reports = fd.history(limit=10)
        assert len(reports) == 1
        assert reports[0].error_type == "ValueError"
        assert reports[0].error_message == "kaboom"

    def test_severity_mapping(self):
        fd, _ = _make_fd(dedup_cooldown_minutes=0)
        handler = FlowDoctorHandler(fd, level=logging.WARNING)
        logger = logging.getLogger("test.handler.severity")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            logger.critical("crit msg")
            logger.warning("warn msg")
        finally:
            logger.removeHandler(handler)
            _flush(handler)

        reports = fd.history(limit=10)
        severities = {r.error_message: r.severity for r in reports}
        assert severities["crit msg"] == "critical"
        assert severities["warn msg"] == "warning"

    def test_below_level_not_reported(self):
        fd, _ = _make_fd()
        handler = FlowDoctorHandler(fd, level=logging.ERROR)
        logger = logging.getLogger("test.handler.below")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            logger.warning("This should be ignored")
        finally:
            logger.removeHandler(handler)
            _flush(handler)

        reports = fd.history(limit=10)
        assert len(reports) == 0

    def test_dedup_through_handler(self):
        fd, _ = _make_fd()
        handler = FlowDoctorHandler(fd, level=logging.ERROR)
        logger = logging.getLogger("test.handler.dedup")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            logger.error("duplicate error")
            logger.error("duplicate error")
        finally:
            logger.removeHandler(handler)
            _flush(handler)

        reports = fd.history(limit=10)
        assert len(reports) == 1

    def test_dedup_through_handler_varying_iso_timestamp(self):
        """Log-captured errors with per-tick ISO timestamps collapse to one report."""
        fd, _ = _make_fd()
        handler = FlowDoctorHandler(fd, level=logging.ERROR)
        logger = logging.getLogger("test.handler.dedup.iso")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        msg_a = (
            "nav_series point refused — Event timestamp 2026-07-06T20:00:16.706630+00:00 "
            "belongs to session 2026-07-07, not the labeled session 2026-07-06"
        )
        msg_b = (
            "nav_series point refused — Event timestamp 2026-07-06T20:01:17.137669+00:00 "
            "belongs to session 2026-07-07, not the labeled session 2026-07-06"
        )

        try:
            logger.error(msg_a)
            logger.error(msg_b)
        finally:
            logger.removeHandler(handler)
            _flush(handler)

        reports = fd.history(limit=10)
        assert len(reports) == 1


class TestNonBlocking:
    def test_emit_returns_fast(self):
        fd, _ = _make_fd()

        original_report = fd.report

        def slow_report(*args, **kwargs):
            time.sleep(0.5)
            return original_report(*args, **kwargs)

        handler = FlowDoctorHandler(fd, level=logging.ERROR)

        with patch.object(fd, "report", side_effect=slow_report):
            start = time.monotonic()
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname="test.py",
                lineno=1, msg="fast emit", args=(), exc_info=None,
            )
            handler.emit(record)
            elapsed = time.monotonic() - start

        _flush(handler, timeout=5)
        assert elapsed < 0.1, f"emit() took {elapsed:.3f}s — should be non-blocking"


class TestPatternFiltering:
    def test_exclude_pattern(self):
        fd, _ = _make_fd()
        handler = FlowDoctorHandler(
            fd, level=logging.ERROR,
            exclude_patterns=[r"^Connection reset"],
        )
        logger = logging.getLogger("test.handler.exclude")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            logger.error("Connection reset by peer")
            logger.error("Real error happened")
        finally:
            logger.removeHandler(handler)
            _flush(handler)

        reports = fd.history(limit=10)
        assert len(reports) == 1
        assert reports[0].error_message == "Real error happened"

    def test_include_pattern(self):
        fd, _ = _make_fd()
        handler = FlowDoctorHandler(
            fd, level=logging.ERROR,
            include_patterns=[r"CRITICAL_FLOW"],
        )
        logger = logging.getLogger("test.handler.include")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            logger.error("Some random error")
            logger.error("CRITICAL_FLOW: pipeline failed")
        finally:
            logger.removeHandler(handler)
            _flush(handler)

        reports = fd.history(limit=10)
        assert len(reports) == 1
        assert "CRITICAL_FLOW" in reports[0].error_message


class TestSafety:
    def test_emit_never_crashes(self):
        fd, _ = _make_fd()
        handler = FlowDoctorHandler(fd, level=logging.ERROR)

        # Break fd so report() would fail
        fd._store = None
        fd._dedup = None

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="test.py",
            lineno=1, msg="should not crash", args=(), exc_info=None,
        )
        # Must not raise
        handler.emit(record)
        _flush(handler)


class TestGetHandler:
    def test_get_handler_convenience(self):
        fd, _ = _make_fd()
        handler = fd.get_handler()
        assert isinstance(handler, FlowDoctorHandler)
        handler.close()

    def test_get_handler_with_level(self):
        fd, _ = _make_fd()
        handler = fd.get_handler(level=logging.WARNING)
        assert handler.level == logging.WARNING
        handler.close()


class TestShutdown:
    def test_shutdown_drains_queue(self):
        fd, _ = _make_fd(dedup_cooldown_minutes=0)
        handler = FlowDoctorHandler(fd, level=logging.ERROR, queue_size=200)

        for i in range(5):
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname="test.py",
                lineno=1, msg=f"error number {i}", args=(), exc_info=None,
            )
            handler.emit(record)

        handler.shutdown(timeout=5)

        reports = fd.history(limit=20)
        assert len(reports) == 5

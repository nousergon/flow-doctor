"""Tests for FlowDoctorProtocol, flow_doctor.context(), and report_async()."""

from __future__ import annotations

import asyncio
import tempfile

import flow_doctor
from flow_doctor import FlowDoctor, FlowDoctorProtocol


def _make_fd(db_path: str) -> FlowDoctor:
    return FlowDoctor.builder("ctx-test").with_store(path=db_path).build()


# ---------------------------------------------------------------------------
# FlowDoctorProtocol
# ---------------------------------------------------------------------------


def test_flow_doctor_satisfies_protocol_at_runtime():
    """FlowDoctor (concrete) must pass a runtime isinstance() check
    against the Protocol, so consumers can swap in test doubles."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _make_fd(f.name)
        assert isinstance(fd, FlowDoctorProtocol)


def test_protocol_is_runtime_checkable():
    """``@runtime_checkable`` lets us isinstance-check non-FlowDoctor
    objects that happen to implement the surface — important for the
    RecordingFlowDoctor double landing in a follow-up commit."""

    class _StubDoctor:
        def report(self, error=None, *, severity="error", context=None, logs=None, message=None):
            return "stub"

        def guard(self):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield

            return _cm()

        def monitor(self, func=None, **kwargs):
            return func

        async def report_async(self, error=None, *, severity="error", context=None, logs=None, message=None):
            return "stub-async"

    assert isinstance(_StubDoctor(), FlowDoctorProtocol)


# ---------------------------------------------------------------------------
# flow_doctor.context() + contextvars propagation
# ---------------------------------------------------------------------------


def test_context_manager_propagates_flow_name_and_stage_to_report():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _make_fd(f.name)
        with flow_doctor.context(flow_name="morning-signal", stage="ingest"):
            report_id = fd.report(ValueError("boom"))
        assert report_id is not None
        reports = fd.history()
        assert reports[0].context.get("flow_name") == "morning-signal"
        assert reports[0].context.get("stage") == "ingest"


def test_context_nesting_inner_scope_shadows_outer():
    """Inner context wins for keys it specifies; unspecified keys
    fall through from the outer scope."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _make_fd(f.name)
        with flow_doctor.context(flow_name="morning-signal", stage="ingest"):
            with flow_doctor.context(stage="rank"):
                report_id = fd.report(ValueError("inner"))
        assert report_id is not None
        reports = fd.history()
        ctx = reports[0].context
        assert ctx.get("flow_name") == "morning-signal"  # from outer
        assert ctx.get("stage") == "rank"  # shadowed by inner


def test_context_resets_after_exit():
    """After the ``with`` block exits, contextvars must be back to
    the prior state — leaks between tests would be a correctness bug."""
    from flow_doctor.core._context import current_flow_name, current_stage

    assert current_flow_name() is None
    assert current_stage() is None
    with flow_doctor.context(flow_name="x", stage="y"):
        assert current_flow_name() == "x"
        assert current_stage() == "y"
    assert current_flow_name() is None
    assert current_stage() is None


def test_context_extra_kwargs_merged_into_report_context():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _make_fd(f.name)
        with flow_doctor.context(flow_name="t", run_id="run-42"):
            fd.report("synthetic")
        ctx = fd.history()[0].context
        assert ctx.get("flow_name") == "t"
        assert ctx.get("run_id") == "run-42"


# ---------------------------------------------------------------------------
# report_async()
# ---------------------------------------------------------------------------


def test_report_async_persists_a_report():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _make_fd(f.name)

        async def _run():
            return await fd.report_async(ValueError("async boom"))

        report_id = asyncio.run(_run())
        assert report_id is not None
        reports = fd.history()
        assert reports[0].error_type == "ValueError"


def test_report_async_inherits_contextvars_across_thread_boundary():
    """asyncio.to_thread (used internally by report_async) snapshots
    contextvars via contextvars.copy_context() before dispatching the
    worker. The flow_doctor.context() values set in the calling task
    must therefore land on the persisted report."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = _make_fd(f.name)

        async def _run():
            with flow_doctor.context(flow_name="async-flow", stage="rank"):
                return await fd.report_async("rank failure")

        report_id = asyncio.run(_run())
        assert report_id is not None
        ctx = fd.history()[0].context
        assert ctx.get("flow_name") == "async-flow"
        assert ctx.get("stage") == "rank"

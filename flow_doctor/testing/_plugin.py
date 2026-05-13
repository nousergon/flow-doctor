"""Pytest plugin entry point.

Registered via ``[project.entry-points.pytest11]`` in ``pyproject.toml``.
Once ``flow-doctor`` is installed, the ``flow_doctor_recorder`` fixture
is auto-discoverable in any pytest test file without an ``import``.
"""

from __future__ import annotations

import pytest

from flow_doctor.testing._recording import RecordingFlowDoctor


@pytest.fixture
def flow_doctor_recorder() -> RecordingFlowDoctor:
    """A fresh :class:`RecordingFlowDoctor` per test.

    Usage::

        def test_pipeline_reports_db_errors(flow_doctor_recorder):
            run_pipeline_that_should_fail(flow_doctor_recorder)
            assert len(flow_doctor_recorder.reports) == 1
            assert flow_doctor_recorder.last.exc_type == "DBError"
    """
    return RecordingFlowDoctor()


__all__ = ["flow_doctor_recorder"]

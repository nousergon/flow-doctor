"""Test utilities for downstream consumers of flow-doctor.

This package ships a ``RecordingFlowDoctor`` test double that implements
:class:`flow_doctor.FlowDoctorProtocol` and a pytest plugin that
registers a ``flow_doctor_recorder`` fixture. Downstreams just
``pip install flow-doctor`` — no import required in their test files
— and the fixture is auto-discovered via ``entry_points.pytest11``.

Direct imports are still supported for projects that prefer to be
explicit::

    from flow_doctor.testing import RecordingFlowDoctor, ReportedIncident
"""

from flow_doctor.testing._recording import (
    RecordingFlowDoctor,
    ReportedIncident,
)

__all__ = [
    "RecordingFlowDoctor",
    "ReportedIncident",
]

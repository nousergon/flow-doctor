"""Runtime structural checks for the public doctor protocol."""

from __future__ import annotations

from pathlib import Path

from flow_doctor import FlowDoctor
from flow_doctor import _protocol
from flow_doctor._protocol import FlowDoctorProtocol
from flow_doctor.testing import RecordingFlowDoctor


class _IncompleteDoctor:
    """A runtime double missing report_async; signatures are not checked."""

    def report(self) -> None:
        pass

    def guard(self) -> None:
        pass

    def monitor(self) -> None:
        pass


class _CompleteDoctor(_IncompleteDoctor):
    async def report_async(self) -> None:
        pass


def test_protocol_is_runtime_checkable_and_structural() -> None:
    assert isinstance(_CompleteDoctor(), FlowDoctorProtocol)
    assert not isinstance(_IncompleteDoctor(), FlowDoctorProtocol)


def test_protocol_exports() -> None:
    assert _protocol.__all__ == ["FlowDoctorProtocol"]


def test_concrete_doctor_satisfies_protocol(tmp_path: Path) -> None:
    doctor = FlowDoctor.builder("protocol-test").with_store(path=str(tmp_path / "reports.db")).build()
    assert isinstance(doctor, FlowDoctorProtocol)


def test_recording_doctor_satisfies_protocol() -> None:
    assert isinstance(RecordingFlowDoctor(), FlowDoctorProtocol)

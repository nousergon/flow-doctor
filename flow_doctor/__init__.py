"""Flow Doctor -- call-site error handler for pipeline reliability."""

from flow_doctor.core.client import FlowDoctor, init
from flow_doctor.core.errors import ConfigError, FlowDoctorError
from flow_doctor.core.handler import FlowDoctorHandler
from flow_doctor.core.models import Severity

__all__ = [
    "ConfigError",
    "FlowDoctor",
    "FlowDoctorError",
    "FlowDoctorHandler",
    "Severity",
    "init",
]
__version__ = "0.4.0"

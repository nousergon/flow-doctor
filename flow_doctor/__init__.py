"""Flow Doctor -- call-site error handler for pipeline reliability."""

from flow_doctor.core.builder import FlowDoctorBuilder
from flow_doctor.core.client import FlowDoctor, init
from flow_doctor.core.errors import ConfigError, FlowDoctorError
from flow_doctor.core.handler import FlowDoctorHandler
from flow_doctor.core.models import Severity
from flow_doctor.notify.configs import (
    EmailNotifierConfig,
    GitHubNotifierConfig,
    NotifierConfig,
    S3NotifierConfig,
    SlackNotifierConfig,
)

__all__ = [
    "ConfigError",
    "EmailNotifierConfig",
    "FlowDoctor",
    "FlowDoctorBuilder",
    "FlowDoctorError",
    "FlowDoctorHandler",
    "GitHubNotifierConfig",
    "NotifierConfig",
    "S3NotifierConfig",
    "Severity",
    "SlackNotifierConfig",
    "init",
]
__version__ = "0.4.0"

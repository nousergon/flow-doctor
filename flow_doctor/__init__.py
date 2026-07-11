"""Flow Doctor -- call-site error handler for pipeline reliability."""

from flow_doctor._protocol import FlowDoctorProtocol
from flow_doctor.core._context import context, current_context
from flow_doctor.core.builder import FlowDoctorBuilder
from flow_doctor.core.client import FlowDoctor
from flow_doctor.core.errors import ConfigError, FlowDoctorError
from flow_doctor.core.handler import FlowDoctorHandler
from flow_doctor.core.models import DecisionReason, Severity
from flow_doctor.notify.configs import (
    EmailNotifierConfig,
    GitHubNotifierConfig,
    NotifierConfig,
    S3NotifierConfig,
    SlackNotifierConfig,
    TelegramNotifierConfig,
)

__all__ = [
    "ConfigError",
    "DecisionReason",
    "EmailNotifierConfig",
    "FlowDoctor",
    "FlowDoctorBuilder",
    "FlowDoctorError",
    "FlowDoctorHandler",
    "FlowDoctorProtocol",
    "GitHubNotifierConfig",
    "NotifierConfig",
    "S3NotifierConfig",
    "Severity",
    "SlackNotifierConfig",
    "TelegramNotifierConfig",
    "context",
    "current_context",
]
__version__ = "0.8.4"

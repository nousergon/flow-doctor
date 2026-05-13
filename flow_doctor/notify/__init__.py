"""Notifier package — concrete notifiers + per-channel typed configs."""

from flow_doctor.notify.configs import (
    EmailNotifierConfig,
    GitHubNotifierConfig,
    NotifierConfig,
    S3NotifierConfig,
    SlackNotifierConfig,
    TelegramNotifierConfig,
)

__all__ = [
    "EmailNotifierConfig",
    "GitHubNotifierConfig",
    "NotifierConfig",
    "S3NotifierConfig",
    "SlackNotifierConfig",
    "TelegramNotifierConfig",
]

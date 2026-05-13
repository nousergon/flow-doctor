"""Per-channel typed notifier configs (Pydantic v2 discriminated union).

These are the SOTA-facing replacement for the omnibus
``flow_doctor.core.config.NotifyChannelConfig``. Each notifier channel
gets its own model with only the fields it actually consumes — so
IDE autocomplete and ``mypy --strict`` surface required fields at the
construction site, and a misplaced ``token=`` on an Email config is a
type error rather than a silent typo.

The omnibus ``NotifyChannelConfig`` is still the internal lingua franca
that ``FlowDoctor._init_notifiers`` consumes. Each typed config exposes
``to_channel_config()`` so the builder can fold typed inputs back into
the legacy shape without touching the init code path.
"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from flow_doctor.core.config import NotifyChannelConfig

# Telegram chat_id may be an integer (typical) or a "@channelusername"
# string (public channels only). The typed config preserves the
# union; the legacy omnibus form already accepts ``Union[int, str]``.
TelegramChatId = Union[int, str]


class _NotifierConfigBase(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=False)


class SlackNotifierConfig(_NotifierConfigBase):
    type: Literal["slack"] = "slack"
    webhook_url: Optional[str] = None
    channel: Optional[str] = None

    def to_channel_config(self) -> NotifyChannelConfig:
        return NotifyChannelConfig(
            type="slack",
            webhook_url=self.webhook_url,
            channel=self.channel,
        )


class EmailNotifierConfig(_NotifierConfigBase):
    type: Literal["email"] = "email"
    sender: Optional[str] = None
    # The legacy NotifyChannelConfig.recipients is a CSV string; accept
    # either ``"a@x, b@y"`` or ``["a@x", "b@y"]`` at the typed surface
    # and normalize to CSV on the way down to the omnibus form.
    recipients: Optional[Union[str, List[str]]] = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_password: Optional[str] = None

    @field_validator("recipients", mode="after")
    @classmethod
    def _normalize_recipients(
        cls, v: Optional[Union[str, List[str]]]
    ) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, list):
            cleaned = [str(item).strip() for item in v if item]
            return ", ".join(cleaned) if cleaned else None
        return v

    def to_channel_config(self) -> NotifyChannelConfig:
        return NotifyChannelConfig(
            type="email",
            sender=self.sender,
            recipients=self.recipients,  # already CSV after the validator
            smtp_host=self.smtp_host,
            smtp_port=self.smtp_port,
            smtp_password=self.smtp_password,
        )


class GitHubNotifierConfig(_NotifierConfigBase):
    type: Literal["github"] = "github"
    repo: Optional[str] = None
    token: Optional[str] = None
    labels: Optional[List[str]] = None

    def to_channel_config(self) -> NotifyChannelConfig:
        return NotifyChannelConfig(
            type="github",
            repo=self.repo,
            token=self.token,
            labels=self.labels,
        )


class S3NotifierConfig(_NotifierConfigBase):
    type: Literal["s3"] = "s3"
    bucket: Optional[str] = None
    subsystem: Optional[str] = None
    entry_prefix: str = "changelog/entries"
    default_root_cause_category: str = "code_bug"
    default_resolution_type: Optional[str] = None

    def to_channel_config(self) -> NotifyChannelConfig:
        return NotifyChannelConfig(
            type="s3",
            bucket=self.bucket,
            subsystem=self.subsystem,
            entry_prefix=self.entry_prefix,
            default_root_cause_category=self.default_root_cause_category,
            default_resolution_type=self.default_resolution_type,
        )


class TelegramNotifierConfig(_NotifierConfigBase):
    """Recommended default notifier (since 0.5.0rc2).

    Setup: message ``@BotFather`` → ``/newbot`` → save the bot token.
    Add the bot to your target chat. Look up the ``chat_id`` via
    ``GET https://api.telegram.org/bot<TOKEN>/getUpdates`` after sending
    the bot a message. For forum-style supergroups, also note the
    ``message_thread_id`` of the topic you want notifications routed to.
    """

    type: Literal["telegram"] = "telegram"
    bot_token: Optional[str] = None
    chat_id: Optional[TelegramChatId] = None
    # Forum supergroups support per-topic routing — use this to fan out
    # N flow-doctor flows (am, pm, alpha-engine, predictor, ...) into
    # one chat without N bots.
    message_thread_id: Optional[int] = None
    # Telegram parse_mode for the message body. ``Markdown`` matches
    # the legacy / minimal Markdown flavour; pass ``MarkdownV2`` for
    # the strict escaping mode, ``HTML`` for HTML, or ``None`` for
    # plain text.
    parse_mode: Optional[str] = "Markdown"
    # Silent delivery — Telegram still pushes but without a sound.
    disable_notification: bool = False

    def to_channel_config(self) -> NotifyChannelConfig:
        return NotifyChannelConfig(
            type="telegram",
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            message_thread_id=self.message_thread_id,
            parse_mode=self.parse_mode,
            disable_notification=self.disable_notification,
        )


# Discriminated union of all typed notifier configs. Consumers can
# type-hint as ``NotifierConfig`` and Pydantic will pick the right
# concrete model based on the ``type`` field.
NotifierConfig = Annotated[
    Union[
        SlackNotifierConfig,
        EmailNotifierConfig,
        GitHubNotifierConfig,
        S3NotifierConfig,
        TelegramNotifierConfig,
    ],
    Field(discriminator="type"),
]


__all__ = [
    "EmailNotifierConfig",
    "GitHubNotifierConfig",
    "NotifierConfig",
    "S3NotifierConfig",
    "SlackNotifierConfig",
    "TelegramNotifierConfig",
]

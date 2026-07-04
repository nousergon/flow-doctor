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

    # Per-notifier severity routing, shared by every channel. When None,
    # the dispatcher applies the default set {critical, error}. Set e.g.
    # ["critical", "error", "info"] to also receive healthy-completion
    # pings (notify_success) on this channel.
    notify_on: Optional[List[str]] = None

    # Per-notifier diagnosis-category routing, shared by every channel.
    # Requires Phase 2 diagnosis enabled (DiagnosisConfig.enabled=True) — a
    # report without a diagnosis always passes this gate. When set (e.g.
    # ["code", "config"]), lets a noisy/curated channel (a GitHub issue
    # tracker, a human backlog) opt in to only human-actionable defects
    # while a cheap channel (Telegram/SNS) still pages on everything else
    # (transient/external/infra noise). See FlowDoctor._send_notifications.
    notify_on_category: Optional[List[str]] = None


class SlackNotifierConfig(_NotifierConfigBase):
    type: Literal["slack"] = "slack"
    webhook_url: Optional[str] = None
    channel: Optional[str] = None

    def to_channel_config(self) -> NotifyChannelConfig:
        return NotifyChannelConfig(
            type="slack",
            notify_on=self.notify_on,
            notify_on_category=self.notify_on_category,
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
            notify_on=self.notify_on,
            notify_on_category=self.notify_on_category,
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
    # Toggle 1 — auto-create a GitHub issue on failure. When False, this
    # notifier files no issue (skipped at init). Default True.
    auto_create_issue: bool = True
    # Toggle 2 — auto-create a fix PR for the filed issue. When True, the
    # notifier applies ``fix_label`` to the new issue, firing the
    # flow-doctor-fix Actions workflow (LLM diff -> scope guard -> test
    # gate -> PR) with no human label step. Default False.
    auto_fix_pr: bool = False
    fix_label: str = "flow-doctor:fix"

    def to_channel_config(self) -> NotifyChannelConfig:
        return NotifyChannelConfig(
            type="github",
            notify_on=self.notify_on,
            notify_on_category=self.notify_on_category,
            repo=self.repo,
            token=self.token,
            labels=self.labels,
            auto_create_issue=self.auto_create_issue,
            auto_fix_pr=self.auto_fix_pr,
            fix_label=self.fix_label,
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
            notify_on=self.notify_on,
            notify_on_category=self.notify_on_category,
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
            notify_on=self.notify_on,
            notify_on_category=self.notify_on_category,
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

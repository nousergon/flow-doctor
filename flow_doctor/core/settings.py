"""Typed ``FLOW_DOCTOR_*`` settings contract (pydantic-settings).

This is the canonical, declared schema for the environment variables
flow-doctor resolves credentials from — the institutional replacement for a
hand-rolled ``os.environ`` lookup dict. Each field's ``AliasChoices`` encodes
the documented fallback chain (canonical ``FLOW_DOCTOR_*`` name first, then the
legacy / convention aliases like ``GMAIL_APP_PASSWORD`` or ``GH_TOKEN``), so
the precedence is *declared on the field* rather than buried in a loop.

pydantic-settings resolves each field, in order, from:

1. the process environment,
2. a ``.env`` file (path from ``FLOW_DOCTOR_ENV_FILE``, default ``.env``),
3. a secrets directory (``FLOW_DOCTOR_SECRETS_DIR`` — Docker / Kubernetes
   file-mounted secrets, one file per env-var name).

So a self-hosted deploy gets ``.env`` and file-secret support for free, on top
of plain env vars. This layer is *resolution* only — the named-field
``ConfigError`` messages in ``client.py`` still own the "you are missing X"
fail-loud UX. Fields are typed ``Optional[str]`` to preserve the
string-returning contract the client's downstream coercion (e.g. telegram
``chat_id`` → int) already depends on.
"""

from __future__ import annotations

from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FlowDoctorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",            # overridable per-construction via _env_file
        env_file_encoding="utf-8",
        extra="ignore",             # unrelated FLOW_DOCTOR_* vars don't error
        case_sensitive=False,
    )

    github_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "FLOW_DOCTOR_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"
        ),
    )
    github_repo: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("FLOW_DOCTOR_GITHUB_REPO"),
    )
    smtp_password: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "FLOW_DOCTOR_SMTP_PASSWORD", "GMAIL_APP_PASSWORD"
        ),
    )
    smtp_sender: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("FLOW_DOCTOR_SMTP_SENDER", "EMAIL_SENDER"),
    )
    smtp_recipients: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "FLOW_DOCTOR_SMTP_RECIPIENTS", "EMAIL_RECIPIENTS"
        ),
    )
    slack_webhook: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "FLOW_DOCTOR_SLACK_WEBHOOK", "SLACK_WEBHOOK_URL"
        ),
    )
    anthropic_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "FLOW_DOCTOR_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"
        ),
    )
    s3_bucket: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("FLOW_DOCTOR_S3_BUCKET", "CHANGELOG_BUCKET"),
    )
    telegram_bot_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"
        ),
    )
    telegram_chat_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "FLOW_DOCTOR_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"
        ),
    )
    # JSON-encoded PushSubscription.toJSON() string - no legacy alias, this
    # field is new with the webpush notifier.
    webpush_subscription: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("FLOW_DOCTOR_WEBPUSH_SUBSCRIPTION"),
    )


__all__ = ["FlowDoctorSettings"]

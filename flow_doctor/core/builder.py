"""Typed fluent builder for ``FlowDoctor`` — no yaml required.

The builder is the recommended entry point. Each method returns ``self``
so calls chain, each accepts a typed Pydantic sub-config so IDE
autocomplete and ``mypy --strict`` work end-to-end. For yaml-driven
configuration use ``FlowDoctor.from_config(config_path=...)`` (the
supported replacement for the removed ``init()`` free function).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Union

from flow_doctor.core.config import (
    AutoFixConfig,
    DiagnosisConfig,
    FlowDoctorConfig,
    GitHubConfig,
    HandlerConfig,
    NotifyChannelConfig,
    RateLimitConfig,
    RemediationConfig,
    StoreConfig,
)
from flow_doctor.notify.configs import (
    EmailNotifierConfig,
    GitHubNotifierConfig,
    S3NotifierConfig,
    SlackNotifierConfig,
)

if TYPE_CHECKING:
    from flow_doctor.core.client import FlowDoctor


_PerTypeNotifier = Union[
    SlackNotifierConfig,
    EmailNotifierConfig,
    GitHubNotifierConfig,
    S3NotifierConfig,
]


class FlowDoctorBuilder:
    """Fluent builder for :class:`FlowDoctor`.

    Typical usage::

        fd = (
            FlowDoctor.builder("morning-signal")
            .add_notifier(EmailNotifierConfig(
                sender="x@y.com",
                recipients=["x@y.com"],
                smtp_password=os.environ["GMAIL_APP_PASSWORD"],
            ))
            .with_dedup(cooldown_minutes=60)
            .build()
        )

    Every ``with_*`` / ``add_*`` method returns ``self`` so calls chain.
    ``build()`` materializes a ``FlowDoctorConfig`` and constructs a
    ``FlowDoctor``; ``build_config()`` returns just the config (useful
    for tests and for handing a config off to a custom subclass).
    """

    def __init__(self, flow_name: str):
        self._flow_name = flow_name
        self._repo: Optional[str] = None
        self._owner: Optional[str] = None
        self._notifiers: List[NotifyChannelConfig] = []
        self._store: Optional[StoreConfig] = None
        self._dedup_cooldown_minutes: Optional[int] = None
        self._rate_limits: Optional[RateLimitConfig] = None
        self._diagnosis: Optional[DiagnosisConfig] = None
        self._github: Optional[GitHubConfig] = None
        self._auto_fix: Optional[AutoFixConfig] = None
        self._remediation: Optional[RemediationConfig] = None
        self._handler: Optional[HandlerConfig] = None
        self._dependencies: List[str] = []

    def add_notifier(
        self,
        cfg: Union[_PerTypeNotifier, NotifyChannelConfig],
    ) -> "FlowDoctorBuilder":
        """Append a notifier.

        Accepts a typed per-channel config (``SlackNotifierConfig``,
        ``EmailNotifierConfig``, ``GitHubNotifierConfig``,
        ``S3NotifierConfig``) or the legacy omnibus
        ``NotifyChannelConfig`` (for back-compat with 0.4.0 callers).
        """
        if isinstance(cfg, NotifyChannelConfig):
            self._notifiers.append(cfg)
        else:
            self._notifiers.append(cfg.to_channel_config())
        return self

    def with_repo(
        self, repo: str, *, owner: Optional[str] = None
    ) -> "FlowDoctorBuilder":
        self._repo = repo
        if owner is not None:
            self._owner = owner
        return self

    def with_dedup(self, *, cooldown_minutes: int) -> "FlowDoctorBuilder":
        self._dedup_cooldown_minutes = cooldown_minutes
        return self

    def with_store(
        self,
        *,
        type: str = "sqlite",
        path: str = "flow_doctor.db",
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
    ) -> "FlowDoctorBuilder":
        self._store = StoreConfig(
            type=type, path=path, bucket=bucket, prefix=prefix
        )
        return self

    def with_rate_limits(self, cfg: RateLimitConfig) -> "FlowDoctorBuilder":
        self._rate_limits = cfg
        return self

    def with_diagnosis(self, cfg: DiagnosisConfig) -> "FlowDoctorBuilder":
        self._diagnosis = cfg
        return self

    def with_github(self, cfg: GitHubConfig) -> "FlowDoctorBuilder":
        self._github = cfg
        return self

    def with_auto_fix(self, cfg: AutoFixConfig) -> "FlowDoctorBuilder":
        self._auto_fix = cfg
        return self

    def with_remediation(self, cfg: RemediationConfig) -> "FlowDoctorBuilder":
        self._remediation = cfg
        return self

    def with_handler(self, cfg: HandlerConfig) -> "FlowDoctorBuilder":
        self._handler = cfg
        return self

    def with_dependencies(self, deps: List[str]) -> "FlowDoctorBuilder":
        self._dependencies = list(deps)
        return self

    def build_config(self) -> FlowDoctorConfig:
        """Materialize the ``FlowDoctorConfig`` without instantiating
        ``FlowDoctor``. Useful for tests + for handing the config to
        a custom subclass."""
        kwargs: dict = {
            "flow_name": self._flow_name,
            "notify": list(self._notifiers),
        }
        if self._repo is not None:
            kwargs["repo"] = self._repo
        if self._owner is not None:
            kwargs["owner"] = self._owner
        if self._store is not None:
            kwargs["store"] = self._store
        if self._rate_limits is not None:
            kwargs["rate_limits"] = self._rate_limits
        if self._dedup_cooldown_minutes is not None:
            kwargs["dedup_cooldown_minutes"] = self._dedup_cooldown_minutes
        if self._diagnosis is not None:
            kwargs["diagnosis"] = self._diagnosis
        if self._github is not None:
            kwargs["github"] = self._github
        if self._auto_fix is not None:
            kwargs["auto_fix"] = self._auto_fix
        if self._remediation is not None:
            kwargs["remediation"] = self._remediation
        if self._handler is not None:
            kwargs["handler"] = self._handler
        if self._dependencies:
            kwargs["dependencies"] = self._dependencies
        return FlowDoctorConfig(**kwargs)

    def build(self, *, strict: bool = True) -> "FlowDoctor":
        """Materialize the config and construct a :class:`FlowDoctor`."""
        from flow_doctor.core.client import FlowDoctor

        return FlowDoctor(self.build_config(), strict=strict)


__all__ = ["FlowDoctorBuilder"]

"""Configuration: YAML file + inline Python kwargs.

Backed by Pydantic v2 ``BaseModel`` (since v0.5.0) — field names + defaults
match the prior ``@dataclass``-based shape, so callers constructing
``FlowDoctorConfig(...)``, ``NotifyChannelConfig(...)``, etc. by keyword
keep working unchanged. The benefit of the migration is type validation
at construction time and a stable foundation for the typed builder API
and the Pydantic ``BaseSettings`` env-var contract.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field

from flow_doctor.core.constants import DEFAULT_DIAGNOSIS_MODEL
from flow_doctor.core.errors import ConfigError


class _ConfigModel(BaseModel):
    """Base for flow-doctor config models.

    ``extra="ignore"`` matches the prior dataclass behaviour (unknown
    keys in inline kwargs / yaml were silently dropped). ``validate_assignment``
    is off so test/runtime code that mutates an attribute after construction
    (e.g. ``cfg.market_hours_lockout = False``) keeps working the same way
    it did with dataclasses.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=False)


class NotifyChannelConfig(_ConfigModel):
    """Internal omnibus notifier config — the lingua franca that
    ``FlowDoctor._init_notifiers`` consumes.

    Not part of the public API: construct a typed ``SlackNotifierConfig`` /
    ``EmailNotifierConfig`` / ``GitHubNotifierConfig`` / ``S3NotifierConfig`` /
    ``TelegramNotifierConfig`` from ``flow_doctor.notify`` instead. The
    ``@deprecated`` marker that flagged direct construction was dropped in
    0.6.0 — the typed configs now fold into this model via
    ``to_channel_config()``, so this is purely an internal representation.
    """

    type: str  # "slack", "email", "github", "s3", or "telegram"
    # Per-notifier severity routing. When None, the dispatcher applies the
    # default set {critical, error} (warnings + info skipped). Set e.g.
    # ["critical", "error", "info"] to also receive healthy-completion
    # pings on this channel, or ["warning"] to fan ad-hoc warnings to a
    # separate channel. See FlowDoctor._send_notifications.
    notify_on: Optional[List[str]] = None
    # Per-notifier diagnosis-category routing (requires Phase 2 diagnosis
    # enabled — see DiagnosisConfig). When None, this gate is a no-op and
    # every category reaches the notifier (unchanged pre-0.8.0 behavior).
    # When set (e.g. ["code", "config"]), the notifier only fires for
    # reports whose diagnosis lands in one of these categories — lets a
    # noisy channel (a GitHub issue tracker, a human backlog) opt in to
    # only human-actionable defects while a cheap channel (Telegram/SNS)
    # still pages on everything. If diagnosis didn't run or is unavailable
    # for a given report, the gate is skipped and the notifier fires as if
    # unset — an optional enrichment must never silently blank out an
    # entire channel when its own prerequisite isn't met. See
    # FlowDoctor._send_notifications and DiagnosisConfig.
    notify_on_category: Optional[List[str]] = None
    # Slack fields
    webhook_url: Optional[str] = None
    channel: Optional[str] = None
    # Email fields
    sender: Optional[str] = None
    recipients: Optional[str] = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_password: Optional[str] = None
    # GitHub fields
    repo: Optional[str] = None
    token: Optional[str] = None
    labels: Optional[List[str]] = None
    # Auto-issue toggle: when False, this github notifier files NO issue
    # (the notifier is skipped at init). Lets a consumer keep the block in
    # config but silence issue creation without deleting it. Default True.
    auto_create_issue: bool = True
    # Auto-fix-PR toggle: when True, after filing the issue the notifier
    # applies ``fix_label`` to it, which fires the flow-doctor-fix GitHub
    # Actions workflow (LLM diff -> scope guard -> test gate -> PR) with no
    # human label step. Default False (the human-in-the-loop default).
    auto_fix_pr: bool = False
    fix_label: str = "flow-doctor:fix"
    # S3 fields (writes schema-1.0.0 entries to the system-wide changelog
    # corpus — closes the flow-doctor side of the changelog event-mining
    # coverage gaps roadmap item).
    bucket: Optional[str] = None
    subsystem: Optional[str] = None
    entry_prefix: str = "changelog/entries"
    default_root_cause_category: str = "code_bug"
    default_resolution_type: Optional[str] = None
    # Telegram fields (Bot API). bot_token + chat_id are required at
    # init time; message_thread_id is optional for forum topics.
    bot_token: Optional[str] = None
    chat_id: Optional[Union[int, str]] = None
    message_thread_id: Optional[int] = None
    parse_mode: Optional[str] = "Markdown"
    disable_notification: bool = False
    # Web Push fields. webpush_subscription is required (from config or
    # FLOW_DOCTOR_WEBPUSH_SUBSCRIPTION); the two VAPID fields are optional
    # overrides of krepis.webpush.send_push's own secret-resolved default.
    webpush_subscription: Optional[Dict[str, Any]] = None
    webpush_url: Optional[str] = None
    webpush_vapid_private_key: Optional[str] = None
    webpush_vapid_subject: Optional[str] = None


class StoreConfig(_ConfigModel):
    type: str = "sqlite"
    path: str = "flow_doctor.db"
    bucket: Optional[str] = None
    prefix: Optional[str] = None
    table_name: Optional[str] = None
    region: Optional[str] = None


class RateLimitConfig(_ConfigModel):
    max_diagnosed_per_day: int = 3
    max_issues_per_day: int = 3
    max_alerts_per_day: int = 5
    daily_digest: bool = True
    digest_time: str = "17:00"
    dedup_cooldown_minutes: int = 60


class DiagnosisConfig(_ConfigModel):
    enabled: bool = False
    # "anthropic" (native SDK) or "openai_compat" (any OpenAI-compatible
    # chat-completions endpoint: OpenRouter open-weight models, OpenAI,
    # self-hosted vLLM — set base_url + model accordingly).
    provider: str = "anthropic"
    model: str = DEFAULT_DIAGNOSIS_MODEL
    api_key: Optional[str] = None
    # openai_compat only. base_url defaults to OpenRouter. The per-1M prices
    # are REQUIRED for non-OpenRouter endpoints (OpenRouter reports its own
    # billed cost) — they keep the max_daily_cost_usd cap honest.
    base_url: str = "https://openrouter.ai/api/v1"
    price_in_per_1m: Optional[float] = None
    price_out_per_1m: Optional[float] = None
    confidence_calibration: float = 0.85
    timeout_seconds: int = 30
    max_daily_cost_usd: float = 1.00  # Hard cap on daily LLM spend
    # SFT capture (small-model distillation corpus, config#1541). When LLM
    # capture is enabled via the fleet env switch (LLM_SFT_CAPTURE_ENABLED /
    # ALPHA_ENGINE_DECISION_CAPTURE_ENABLED), each diagnosis call is appended
    # to this JSONL sink as a canonical krepis SFT v3 record tagged
    # producer="flow_doctor_diagnosis". None → the DEFAULT_SFT_SINK_PATH under
    # _sft_raw/. Capture is a no-op unless both the env switch is set AND the
    # optional `krepis` dep is installed (pip install flow-doctor[sft]).
    sft_sink_path: Optional[str] = None


class GitHubConfig(_ConfigModel):
    token: Optional[str] = None
    labels: List[str] = Field(default_factory=lambda: ["flow-doctor"])


class ScopeConfig(_ConfigModel):
    allow: List[str] = Field(default_factory=list)
    deny: List[str] = Field(default_factory=list)


class AutoFixConfig(_ConfigModel):
    enabled: bool = False
    confidence_threshold: float = 0.90
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    test_command: str = "python -m pytest tests/ -x -q"
    dry_run: bool = True
    model: Optional[str] = None


class RemediationConfig(_ConfigModel):
    enabled: bool = False
    dry_run: bool = True  # Log actions without executing
    auto_remediate_min_confidence: float = 0.9
    # Raised from 0.8 → 0.85 in the conservative-defaults revision. The
    # 0.8 threshold was letting through fixes the LLM was only marginally
    # confident about, which produced PRs that humans had to spend effort
    # rejecting. 0.85 keeps the feature useful while cutting false-positive
    # PR volume. Consumers can override per-install in flow-doctor.yaml
    # if they want to be even more conservative (e.g., 0.9).
    fix_pr_min_confidence: float = 0.85
    # Raised from 5 → 2. The original 5/day default was calibrated for
    # a high-volume CI environment where fixes are usually dependency
    # bumps and lint autofixes. For application code, 2/day leaves
    # meaningful headroom for the LLM to propose fixes while keeping
    # review bandwidth manageable — at 5/day a single bad day could
    # produce 25 PRs in a work week, which is a PR-fatigue recipe.
    max_auto_remediations_per_day: int = 2
    max_auto_remediations_per_failure: int = 2
    market_hours_lockout: bool = True
    # Telegram routing for remediation auto-action pings. Preferred
    # path since 0.5.0rc3 — the executor will build a real
    # ``TelegramNotifier`` from these fields and route every remediation
    # action's success / failure through it, picking up bot-token /
    # chat-id / threading / Markdown rendering / target-id audit for
    # free. Leave unset to skip Telegram notification entirely.
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[Union[int, str]] = None
    telegram_message_thread_id: Optional[int] = None
    # Legacy webhook URL — kept for back-compat with 0.4.x configs.
    # Was a misnomer (Telegram doesn't have user-installable webhooks
    # the way Slack does), and the executor's bespoke POST format
    # didn't compose with the rest of the notifier surface. New
    # consumers should use ``telegram_bot_token`` + ``telegram_chat_id``
    # above; this field will be removed in 0.6.0.
    telegram_webhook_url: Optional[str] = None
    s3_audit_bucket: Optional[str] = None
    s3_audit_prefix: str = "flow-doctor/audit"
    # Hard deny list. Repos on this list will NEVER have auto-fix
    # applied, even if remediation.enabled=True and the LLM confidence
    # exceeds thresholds. Use for production-critical repos where a
    # bad auto-fix could cost real money or safety (trading systems,
    # payment processors, medical software). Issue-filing still works
    # for these repos — only code modifications are blocked. Matches
    # GitHub-style "owner/name" or bare "name" (case-insensitive).
    deny_repos: List[str] = Field(default_factory=list)


class HandlerConfig(_ConfigModel):
    level: str = "ERROR"
    include_patterns: List[str] = Field(default_factory=list)
    exclude_patterns: List[str] = Field(default_factory=list)
    queue_size: int = 100


class FlowDoctorConfig(_ConfigModel):
    flow_name: str = "default"
    repo: Optional[str] = None
    owner: Optional[str] = None
    notify: List[NotifyChannelConfig] = Field(default_factory=list)
    store: StoreConfig = Field(default_factory=StoreConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    dependencies: List[str] = Field(default_factory=list)
    dedup_cooldown_minutes: int = 60
    diagnosis: DiagnosisConfig = Field(default_factory=DiagnosisConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    auto_fix: AutoFixConfig = Field(default_factory=AutoFixConfig)
    remediation: RemediationConfig = Field(default_factory=RemediationConfig)
    handler: Optional[HandlerConfig] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_vars(value: str, *, allow_unresolved: bool = False) -> str:
    """Replace ``${VAR_NAME}`` with the environment variable value.

    By default (``allow_unresolved=False``), raises ``ConfigError`` if any
    referenced variable is missing from the environment. This turns YAML
    config bugs into immediate, loud failures instead of silent passthroughs
    where the literal string ``${VAR_NAME}`` ends up being used as a token.

    ``allow_unresolved=True`` preserves the old silent-passthrough behavior
    and exists only for unit tests that want to exercise partial-resolution
    paths without setting real env vars.
    """
    missing: List[str] = []

    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        resolved = os.environ.get(var_name)
        if resolved is None:
            if allow_unresolved:
                return match.group(0)
            missing.append(var_name)
            return match.group(0)
        return resolved

    result = _ENV_VAR_RE.sub(_replacer, value)

    if missing and not allow_unresolved:
        unique_missing = sorted(set(missing))
        names = ", ".join(unique_missing)
        raise ConfigError(
            f"Unresolved environment variable(s) in flow-doctor config: {names}. "
            f"Set these in the process environment before constructing flow-doctor "
            f"(FlowDoctor.from_config / FlowDoctor.builder). "
            f"See the FLOW_DOCTOR_* env var contract in the README."
        )

    return result


def _resolve_dict(d: Any, *, allow_unresolved: bool = False) -> Any:
    """Recursively resolve env vars in a dict/list/string."""
    if isinstance(d, str):
        return _resolve_env_vars(d, allow_unresolved=allow_unresolved)
    if isinstance(d, dict):
        return {k: _resolve_dict(v, allow_unresolved=allow_unresolved) for k, v in d.items()}
    if isinstance(d, list):
        return [_resolve_dict(item, allow_unresolved=allow_unresolved) for item in d]
    return d


def _parse_notify_shorthand(items: List[str]) -> List[NotifyChannelConfig]:
    """Parse shorthand notify list like ['slack:#channel', 'email:addr']."""
    configs = []
    for item in items:
        if item.startswith("slack:"):
            channel = item[len("slack:"):]
            configs.append(NotifyChannelConfig(
                type="slack",
                channel=channel,
                webhook_url=os.environ.get("SLACK_WEBHOOK_URL"),
            ))
        elif item.startswith("email:"):
            addr = item[len("email:"):]
            configs.append(NotifyChannelConfig(
                type="email",
                sender=os.environ.get("EMAIL_SENDER", addr),
                recipients=addr,
                smtp_host="smtp.gmail.com",
                smtp_password=os.environ.get("GMAIL_APP_PASSWORD"),
            ))
        elif item.startswith("github:"):
            repo = item[len("github:"):]
            configs.append(NotifyChannelConfig(
                type="github",
                repo=repo,
                token=os.environ.get("GITHUB_TOKEN"),
            ))
        else:
            configs.append(NotifyChannelConfig(type=item))
    return configs


def _parse_notify_dicts(items: List[Dict]) -> List[NotifyChannelConfig]:
    """Parse YAML notify list of dicts."""
    configs = []
    for item in items:
        item = _resolve_dict(item)
        configs.append(NotifyChannelConfig(
            type=item.get("type", "slack"),
            notify_on=item.get("notify_on"),
            notify_on_category=item.get("notify_on_category"),
            webhook_url=item.get("webhook_url"),
            channel=item.get("channel"),
            sender=item.get("sender"),
            recipients=item.get("recipients"),
            smtp_host=item.get("smtp_host", "smtp.gmail.com"),
            smtp_port=item.get("smtp_port", 587),
            smtp_password=item.get("smtp_password"),
            repo=item.get("repo"),
            token=item.get("token"),
            labels=item.get("labels"),
            auto_create_issue=item.get("auto_create_issue", True),
            auto_fix_pr=item.get("auto_fix_pr", False),
            fix_label=item.get("fix_label", "flow-doctor:fix"),
            bucket=item.get("bucket"),
            subsystem=item.get("subsystem"),
            entry_prefix=item.get("entry_prefix", "changelog/entries"),
            default_root_cause_category=item.get("default_root_cause_category", "code_bug"),
            default_resolution_type=item.get("default_resolution_type"),
            bot_token=item.get("bot_token"),
            chat_id=item.get("chat_id"),
            message_thread_id=item.get("message_thread_id"),
            parse_mode=item.get("parse_mode", "Markdown"),
            disable_notification=item.get("disable_notification", False),
            webpush_subscription=item.get("webpush_subscription"),
            webpush_url=item.get("webpush_url"),
            webpush_vapid_private_key=item.get("webpush_vapid_private_key"),
            webpush_vapid_subject=item.get("webpush_vapid_subject"),
        ))
    return configs


def _parse_store(raw: Any) -> StoreConfig:
    """Parse store config from string or dict."""
    if raw is None:
        return StoreConfig()
    if isinstance(raw, str):
        raw = _resolve_env_vars(raw)
        if raw.startswith("sqlite://"):
            return StoreConfig(type="sqlite", path=raw[len("sqlite://"):])
        if raw.startswith("s3://"):
            parts = raw[len("s3://"):].split("/", 1)
            return StoreConfig(type="s3", bucket=parts[0], prefix=parts[1] if len(parts) > 1 else "")
        if raw.startswith("dynamodb://"):
            return StoreConfig(type="dynamodb", table_name=raw[len("dynamodb://"):])
        return StoreConfig(type="sqlite", path=raw)
    if isinstance(raw, dict):
        raw = _resolve_dict(raw)
        return StoreConfig(
            type=raw.get("type", "sqlite"),
            path=raw.get("path", "flow_doctor.db"),
            bucket=raw.get("bucket"),
            prefix=raw.get("prefix"),
            table_name=raw.get("table_name"),
            region=raw.get("region"),
        )
    return StoreConfig()


def load_config(
    config_path: Optional[str] = None,
    *,
    skip_sections: Sequence[str] = (),
    **kwargs: Any,
) -> FlowDoctorConfig:
    """Load config from YAML file, inline kwargs, or both (kwargs override YAML).

    ``skip_sections`` lists top-level YAML keys to DROP before any env-var
    resolution or parsing. Use it when the caller consumes only a subset of the
    config and the dropped blocks reference ``${VAR}`` that isn't set in this
    runtime — e.g. the fix CLI skips ``notify``/``github`` (it does all GitHub
    work via the ``--token`` arg and never reads those blocks), so unset
    ``${EMAIL_SENDER}`` / ``${FLOW_DOCTOR_GITHUB_TOKEN}`` on a CI runtime don't
    abort the load. Resolution stays STRICT (fail-loud) for every section that
    is kept — a genuinely-missing var the caller DOES use still raises.
    """
    raw: Dict[str, Any] = {}

    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            for section in skip_sections:
                raw.pop(section, None)
            raw = _resolve_dict(raw)

    # Merge inline kwargs (they override YAML values)
    for k, v in kwargs.items():
        if v is not None:
            raw[k] = v

    # Parse notify
    notify_raw = raw.get("notify", [])
    if isinstance(notify_raw, list):
        if notify_raw and isinstance(notify_raw[0], str):
            notify = _parse_notify_shorthand(notify_raw)
        elif notify_raw and isinstance(notify_raw[0], dict):
            notify = _parse_notify_dicts(notify_raw)
        elif notify_raw and isinstance(notify_raw[0], NotifyChannelConfig):
            # Inline kwargs may pass already-constructed NotifyChannelConfig
            # instances (the builder does this); accept them verbatim.
            notify = list(notify_raw)
        else:
            notify = []
    else:
        notify = []

    # Parse store
    store = _parse_store(raw.get("store"))

    # Parse rate limits
    rl_raw = raw.get("rate_limits", {})
    if isinstance(rl_raw, RateLimitConfig):
        rate_limits = rl_raw
    else:
        rate_limits = RateLimitConfig(
            max_diagnosed_per_day=rl_raw.get("max_diagnosed_per_day", 3),
            max_issues_per_day=rl_raw.get("max_issues_per_day", 3),
            max_alerts_per_day=rl_raw.get("max_alerts_per_day", 5),
            daily_digest=rl_raw.get("daily_digest", True),
            digest_time=rl_raw.get("digest_time", "17:00"),
            dedup_cooldown_minutes=rl_raw.get("dedup_cooldown_minutes",
                                               raw.get("dedup_cooldown_minutes", 60)),
        )

    dedup_cooldown = raw.get("dedup_cooldown_minutes", rate_limits.dedup_cooldown_minutes)

    # Parse diagnosis config
    diag_raw = raw.get("diagnosis", {})
    if isinstance(diag_raw, DiagnosisConfig):
        diagnosis_config = diag_raw
    elif isinstance(diag_raw, dict):
        diag_raw = _resolve_dict(diag_raw)
        diagnosis_config = DiagnosisConfig(
            enabled=diag_raw.get("enabled", False),
            provider=diag_raw.get("provider", "anthropic"),
            model=diag_raw.get("model", DEFAULT_DIAGNOSIS_MODEL),
            api_key=diag_raw.get("api_key"),
            base_url=diag_raw.get("base_url", "https://openrouter.ai/api/v1"),
            price_in_per_1m=(
                float(diag_raw["price_in_per_1m"])
                if diag_raw.get("price_in_per_1m") is not None else None
            ),
            price_out_per_1m=(
                float(diag_raw["price_out_per_1m"])
                if diag_raw.get("price_out_per_1m") is not None else None
            ),
            confidence_calibration=float(diag_raw.get("confidence_calibration", 0.85)),
            timeout_seconds=int(diag_raw.get("timeout_seconds", 30)),
            max_daily_cost_usd=float(diag_raw.get("max_daily_cost_usd", 1.00)),
            sft_sink_path=diag_raw.get("sft_sink_path"),
        )
    else:
        diagnosis_config = DiagnosisConfig()

    # Parse github config
    gh_raw = raw.get("github", {})
    if isinstance(gh_raw, GitHubConfig):
        github_config = gh_raw
    elif isinstance(gh_raw, dict):
        gh_raw = _resolve_dict(gh_raw)
        github_config = GitHubConfig(
            token=gh_raw.get("token"),
            labels=gh_raw.get("labels", ["flow-doctor"]),
        )
    else:
        github_config = GitHubConfig()

    # Parse auto_fix config
    af_raw = raw.get("auto_fix", {})
    if isinstance(af_raw, AutoFixConfig):
        auto_fix_config = af_raw
    elif isinstance(af_raw, dict):
        af_raw = _resolve_dict(af_raw)
        scope_raw = af_raw.get("scope", {})
        scope_config = ScopeConfig(
            allow=scope_raw.get("allow", []),
            deny=scope_raw.get("deny", []),
        )
        auto_fix_config = AutoFixConfig(
            enabled=af_raw.get("enabled", False),
            confidence_threshold=float(af_raw.get("confidence_threshold", 0.90)),
            scope=scope_config,
            test_command=af_raw.get("test_command", "python -m pytest tests/ -x -q"),
            dry_run=af_raw.get("dry_run", True),
            model=af_raw.get("model"),
        )
    else:
        auto_fix_config = AutoFixConfig()

    # Parse remediation config
    rem_raw = raw.get("remediation", {})
    if isinstance(rem_raw, RemediationConfig):
        remediation_config = rem_raw
    elif isinstance(rem_raw, dict):
        rem_raw = _resolve_dict(rem_raw)
        # Defaults here match the RemediationConfig model defaults
        # (not inlined) so there's one source of truth. If you change a
        # default, change it in the model at the top of this file.
        _defaults = RemediationConfig()
        deny_repos_raw = rem_raw.get("deny_repos", [])
        if isinstance(deny_repos_raw, str):
            # Allow a single string for the common one-repo case
            deny_repos_raw = [deny_repos_raw]
        remediation_config = RemediationConfig(
            enabled=rem_raw.get("enabled", _defaults.enabled),
            dry_run=rem_raw.get("dry_run", _defaults.dry_run),
            auto_remediate_min_confidence=float(
                rem_raw.get("auto_remediate_min_confidence",
                            _defaults.auto_remediate_min_confidence)),
            fix_pr_min_confidence=float(
                rem_raw.get("fix_pr_min_confidence",
                            _defaults.fix_pr_min_confidence)),
            max_auto_remediations_per_day=int(
                rem_raw.get("max_auto_remediations_per_day",
                            _defaults.max_auto_remediations_per_day)),
            max_auto_remediations_per_failure=int(
                rem_raw.get("max_auto_remediations_per_failure",
                            _defaults.max_auto_remediations_per_failure)),
            market_hours_lockout=rem_raw.get(
                "market_hours_lockout", _defaults.market_hours_lockout),
            telegram_bot_token=rem_raw.get("telegram_bot_token"),
            telegram_chat_id=rem_raw.get("telegram_chat_id"),
            telegram_message_thread_id=rem_raw.get("telegram_message_thread_id"),
            telegram_webhook_url=rem_raw.get("telegram_webhook_url"),
            s3_audit_bucket=rem_raw.get("s3_audit_bucket"),
            s3_audit_prefix=rem_raw.get(
                "s3_audit_prefix", _defaults.s3_audit_prefix),
            deny_repos=list(deny_repos_raw),
        )
    else:
        remediation_config = RemediationConfig()

    # Parse handler config
    handler_raw = raw.get("handler")
    if isinstance(handler_raw, HandlerConfig):
        handler_config = handler_raw
    elif isinstance(handler_raw, dict):
        handler_raw = _resolve_dict(handler_raw)
        handler_config = HandlerConfig(
            level=handler_raw.get("level", "ERROR"),
            include_patterns=handler_raw.get("include_patterns", []),
            exclude_patterns=handler_raw.get("exclude_patterns", []),
            queue_size=int(handler_raw.get("queue_size", 100)),
        )
    else:
        handler_config = None

    return FlowDoctorConfig(
        flow_name=raw.get("flow_name", "default"),
        repo=raw.get("repo"),
        owner=raw.get("owner"),
        notify=notify,
        store=store,
        rate_limits=rate_limits,
        dependencies=raw.get("dependencies", []),
        dedup_cooldown_minutes=dedup_cooldown,
        diagnosis=diagnosis_config,
        github=github_config,
        auto_fix=auto_fix_config,
        remediation=remediation_config,
        handler=handler_config,
    )

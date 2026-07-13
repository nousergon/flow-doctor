"""FlowDoctor client: from_config(), report(), notify_success(), guard(), monitor()."""

from __future__ import annotations

import functools
import logging
import os
import platform
import sys
import traceback as tb_module
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, List, Optional

if TYPE_CHECKING:
    from flow_doctor.core.builder import FlowDoctorBuilder

from flow_doctor.core.config import FlowDoctorConfig, load_config
from flow_doctor.core.dedup import (
    DedupChecker,
    compute_error_signature,
    compute_signature_from_exception,
    compute_signature_from_message,
)
from flow_doctor.core.errors import ConfigError, StorageBackendError
from flow_doctor.core.models import (
    Action,
    ActionStatus,
    ActionType,
    Decision,
    DecisionReason,
    Diagnosis,
    Report,
    Severity,
)
from flow_doctor.core.rate_limiter import CascadeDetector, RateLimiter
from flow_doctor.core.scrubber import Scrubber
from flow_doctor.notify.base import Notifier
from flow_doctor.storage.base import StorageBackend

# Module logger — used to surface notifier failures to the host app's log stream
# instead of only printing to stderr. Host apps catch CRITICAL records via their
# own logging configuration (journalctl, Sentry, Datadog, etc.).
_logger = logging.getLogger("flow_doctor")

# Env var fallback chains, kept as the source for the named-field ConfigError
# messages ("set it via one of: FLOW_DOCTOR_X, Y"). The actual resolution now
# runs through the typed FlowDoctorSettings (pydantic-settings) contract in
# flow_doctor.core.settings, which encodes the same chains as AliasChoices and
# additionally reads a .env file and a secrets directory. FLOW_DOCTOR_* names
# are the canonical contract; the others are conveniences that pick up common
# conventions like the `gh` CLI's GH_TOKEN or GitHub Actions' GITHUB_TOKEN.
_ENV_FALLBACKS: Dict[str, List[str]] = {
    "github_token": ["FLOW_DOCTOR_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"],
    "github_repo": ["FLOW_DOCTOR_GITHUB_REPO"],
    "smtp_password": ["FLOW_DOCTOR_SMTP_PASSWORD", "GMAIL_APP_PASSWORD"],
    "smtp_sender": ["FLOW_DOCTOR_SMTP_SENDER", "EMAIL_SENDER"],
    "smtp_recipients": ["FLOW_DOCTOR_SMTP_RECIPIENTS", "EMAIL_RECIPIENTS"],
    "slack_webhook": ["FLOW_DOCTOR_SLACK_WEBHOOK", "SLACK_WEBHOOK_URL"],
    "anthropic_api_key": ["FLOW_DOCTOR_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"],
    "s3_bucket": ["FLOW_DOCTOR_S3_BUCKET", "CHANGELOG_BUCKET"],
    "telegram_bot_token": ["FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"],
    "telegram_chat_id": ["FLOW_DOCTOR_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"],
}


def _env_fallback(key: str) -> Optional[str]:
    """Resolve a credential field via the typed FLOW_DOCTOR_* settings contract.

    Delegates to :class:`flow_doctor.core.settings.FlowDoctorSettings`
    (pydantic-settings), which resolves ``key`` from — in precedence order —
    the process environment (canonical ``FLOW_DOCTOR_*`` name then the
    documented legacy aliases via ``AliasChoices``), a ``.env`` file
    (``FLOW_DOCTOR_ENV_FILE``, default ``.env``), and a secrets directory
    (``FLOW_DOCTOR_SECRETS_DIR``). Returns the first non-empty value, or None.

    A fresh settings instance is built per call so values stay live (env set
    after import, as tests do, is honoured). Resolution is cheap — a handful
    of lookups at FlowDoctor init.
    """
    from flow_doctor.core.settings import FlowDoctorSettings

    env_file = os.environ.get("FLOW_DOCTOR_ENV_FILE", ".env")
    secrets_dir = os.environ.get("FLOW_DOCTOR_SECRETS_DIR") or None
    try:
        settings = FlowDoctorSettings(_env_file=env_file, _secrets_dir=secrets_dir)
    except Exception:
        # A malformed .env / unreadable secrets dir must not crash credential
        # resolution — fall back to a plain-env settings instance.
        settings = FlowDoctorSettings(_env_file=None, _secrets_dir=None)
    value = getattr(settings, key, None)
    if value is not None and str(value) != "":
        return str(value)
    return None


# Default per-notifier severity routing: critical + error are alerted,
# warnings + info are NOT (info is the healthy-completion ping, opt-in
# only). A notifier overrides this via its config's ``notify_on``.
_DEFAULT_NOTIFY_ON: FrozenSet[str] = frozenset(
    {Severity.CRITICAL.value, Severity.ERROR.value}
)


def _classify_dispatch(d: Dict[str, int]) -> str:
    """Map a ``_send_notifications`` result into a single DecisionReason value.

    Priority: any successful send -> fired; else all matching notifiers
    rate-limited -> rate_limited; else every attempt failed -> delivery_failed;
    else nothing matched the severity -> severity_filtered; else nothing
    matched the diagnosis category -> category_filtered; else nothing was
    configured to receive it -> no_notifiers.
    """
    if d.get("sent", 0) > 0:
        return DecisionReason.FIRED.value
    if d.get("failed", 0) > 0:
        return DecisionReason.DELIVERY_FAILED.value
    if d.get("degraded", 0) > 0:
        return DecisionReason.RATE_LIMITED.value
    if d.get("severity_skipped", 0) > 0:
        return DecisionReason.SEVERITY_FILTERED.value
    if d.get("category_skipped", 0) > 0:
        return DecisionReason.CATEGORY_FILTERED.value
    return DecisionReason.NO_NOTIFIERS.value


def _normalize_notify_on(raw: Optional[List[str]]) -> Optional[FrozenSet[str]]:
    """Normalize a config ``notify_on`` list into a severity set, or None.

    None / empty preserves the default routing. Values are lower-cased and
    stripped so ``["ERROR", " info "]`` works.
    """
    if not raw:
        return None
    return frozenset(str(s).strip().lower() for s in raw if str(s).strip())


def _normalize_notify_on_category(raw: Optional[List[str]]) -> Optional[FrozenSet[str]]:
    """Normalize a config ``notify_on_category`` list into a category set, or None.

    None / empty means "no category gate" — every category (and every report
    with no diagnosis) reaches the notifier. Values are upper-cased and
    stripped so ``["code", " Config "]`` matches the diagnosis provider's
    category strings (``TRANSIENT``/``DATA``/``CODE``/``CONFIG``/
    ``EXTERNAL``/``INFRA``), case-insensitively.
    """
    if not raw:
        return None
    return frozenset(str(s).strip().upper() for s in raw if str(s).strip())


class _LogCaptureHandler(logging.Handler):
    """Non-propagating handler that buffers log records."""

    def __init__(self, level: int = logging.DEBUG):
        super().__init__(level)
        self.records: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            pass


class FlowDoctor:
    """Main Flow Doctor client."""

    @classmethod
    def builder(cls, flow_name: str) -> "FlowDoctorBuilder":
        """Return a fluent builder for constructing a ``FlowDoctor``.

        Preferred entry point for new code — typed, IDE-discoverable,
        no yaml required. See :class:`flow_doctor.core.builder.FlowDoctorBuilder`
        for the full API.

        Example::

            from flow_doctor import FlowDoctor
            from flow_doctor.notify import EmailNotifierConfig

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
        """
        from flow_doctor.core.builder import FlowDoctorBuilder

        return FlowDoctorBuilder(flow_name)

    @classmethod
    def from_config(
        cls,
        config_path: Optional[str] = None,
        *,
        strict: bool = True,
        **kwargs: Any,
    ) -> "FlowDoctor":
        """Construct a ``FlowDoctor`` from a yaml file and/or inline kwargs.

        The supported yaml entry point (replaces the removed
        ``flow_doctor.init()`` free function as of 0.6.0). Prefer
        :meth:`builder` for new code — it's typed and needs no yaml — but
        ``from_config`` is the right tool when configuration is owned by a
        ``flow-doctor.yaml`` file (multi-environment / ops-managed installs).

        Args:
            config_path: Path to a ``flow-doctor.yaml``. Optional — all
                settings can come from ``FLOW_DOCTOR_*`` env vars and/or
                ``**kwargs`` instead.
            strict: If True (default), any config / init error raises. If
                False, errors are logged and flow-doctor runs degraded.
            **kwargs: Inline config overrides (flow_name, repo, notify, ...).

        Raises:
            ConfigError: On invalid config, a missing required notifier
                field, or an unresolved ``${VAR}`` — only when ``strict``.
        """
        config = load_config(config_path=config_path, **kwargs)
        return cls(config, strict=strict)

    def __init__(self, config: FlowDoctorConfig, *, strict: bool = True):
        """Initialize a FlowDoctor instance.

        Args:
            config: Loaded config object.
            strict: If True (default), any misconfiguration failure raises.
                If False, init errors are printed to stderr and the instance
                operates in degraded mode (no notifiers, ``_healthy=False``).
                The strict default is intentional: silent degradation means
                users discover broken error monitoring only during an actual
                incident, which defeats the purpose of the tool. Exception:
                a ``StorageBackendError`` (an infra/runtime failure in the
                store's own init — IAM gap, throttling, network blip, not a
                config mistake) is ALWAYS degraded, regardless of `strict` —
                a telemetry backend must never crash the producer it
                instruments over its own transient failure.
        """
        self.config = config
        self._scrubber = Scrubber()
        self._store: Optional[StorageBackend] = None
        self._notifiers: List[Notifier] = []
        self._dedup: Optional[DedupChecker] = None
        self._rate_limiter: Optional[RateLimiter] = None
        self._cascade_detector: Optional[CascadeDetector] = None
        self._log_handler: Optional[_LogCaptureHandler] = None
        self._diagnosis_provider = None
        self._knowledge_base = None
        self._context_assembler = None
        self._digest_generator = None
        self._decision_gate = None
        self._remediation_executor = None
        self._healthy = False
        # Most recent DecisionReason recorded by report() / notify_event() /
        # notify_success(). Lets a caller distinguish "dispatched to >=1
        # notifier" (FIRED) from every other outcome (severity_filtered,
        # category_filtered, rate_limited, delivery_failed, no_notifiers,
        # deduped) WITHOUT reaching into the store — a non-None report id is
        # returned on every one of those outcomes except `deduped`, so a
        # caller that logs success on "report id is not None" is wrong for
        # e.g. severity_filtered. See `last_dispatch_reason` / `last_dispatched`.
        self._last_dispatch_reason: Optional[str] = None

        try:
            self._store = self._init_store(config)
            self._notifiers = self._init_notifiers(config)
            for _n in self._notifiers:
                _n.validate()
            self._dedup = DedupChecker(self._store, config.dedup_cooldown_minutes)
            self._rate_limiter = RateLimiter(self._store, config.rate_limits)
            self._cascade_detector = CascadeDetector(self._store)

            # Phase 2: diagnosis components
            self._init_diagnosis(config)

            # Phase 3: remediation (decision gate + executor)
            self._init_remediation(config)

            # Daily digest
            from flow_doctor.digest.generator import DigestGenerator
            self._digest_generator = DigestGenerator(self._store)

            self._healthy = True
            self._log_startup()
        except StorageBackendError as exc:
            # Backend runtime/infra failures (IAM gaps, throttling, network
            # blips) in the store's own init are never allowed to crash the
            # calling producer, regardless of `strict` — see the class
            # docstring and nousergon/alpha-engine-config#2465. `strict`
            # governs misconfiguration (missing install/config/secret); it
            # was never meant to let a monitoring dependency's own infra
            # hiccup kill the host workload it was only trying to log for.
            import sys
            print(
                f"[flow-doctor] WARNING: storage backend init failed, operating in "
                f"degraded mode (unconditional — a telemetry backend must never crash "
                f"its caller, regardless of strict=): {exc}",
                file=sys.stderr,
            )
            import traceback as _tb
            _tb.print_exc(file=sys.stderr)
        except Exception:
            if strict:
                raise
            import sys
            print(
                "[flow-doctor] WARNING: initialization failed, operating in degraded mode "
                "(strict=False). This is a best-effort mode — set strict=True to fail loud.",
                file=sys.stderr,
            )
            import traceback as _tb
            _tb.print_exc(file=sys.stderr)

    @staticmethod
    def _init_store(config: FlowDoctorConfig) -> StorageBackend:
        if config.store.type == "sqlite":
            from flow_doctor.storage.sqlite import SQLiteStorage
            store = SQLiteStorage(config.store.path)
            try:
                store.init_schema()
            except Exception as exc:
                # A sqlite init failure here is a filesystem/permissions
                # problem, not a config mistake — see StorageBackendError.
                raise StorageBackendError(
                    f"sqlite store init_schema() failed at {config.store.path!r}: {exc}"
                ) from exc
            return store
        if config.store.type == "dynamodb":
            if not config.store.table_name:
                raise ConfigError(
                    "dynamodb store requires table_name "
                    "(e.g. store: dynamodb://flow-doctor-store or "
                    "store: {type: dynamodb, table_name: flow-doctor-store})"
                )
            from flow_doctor.storage.dynamodb import DynamoDBStorage
            store = DynamoDBStorage(
                table_name=config.store.table_name,
                region=config.store.region,
            )
            try:
                store.init_schema()
            except Exception as exc:
                # An IAM permission gap, throttling, or a network blip while
                # actually calling DynamoDB is an infra/runtime failure, not a
                # config mistake — see StorageBackendError. `table_name` being
                # present is already validated above; anything failing past
                # that point is the backend itself, not misconfiguration.
                raise StorageBackendError(
                    f"dynamodb store init_schema() failed for table "
                    f"{config.store.table_name!r}: {exc}"
                ) from exc
            return store
        raise ValueError(f"Unsupported store type: {config.store.type}")

    @staticmethod
    def _init_notifiers(config: FlowDoctorConfig) -> List[Notifier]:
        """Build notifier instances from config, failing loud on missing fields.

        Each notifier type has a set of required fields. If a notifier is
        listed in ``config.notify`` but any required field is missing (after
        checking env var fallbacks), a ``ConfigError`` is raised naming the
        specific field and the env vars that would satisfy it.

        The old behavior was to silently drop misconfigured notifiers, which
        meant users discovered broken notifications only during an incident.
        The new behavior surfaces config bugs at ``init()`` time.
        """
        notifiers: List[Notifier] = []
        for idx, nc in enumerate(config.notify):
            label = f"notify[{idx}] (type={nc.type})"
            before = len(notifiers)

            if nc.type == "slack":
                webhook = nc.webhook_url or _env_fallback("slack_webhook")
                if not webhook:
                    raise ConfigError(
                        f"{label}: slack notifier requires a webhook_url. "
                        f"Set it in config or via one of: "
                        f"{', '.join(_ENV_FALLBACKS['slack_webhook'])}."
                    )
                from flow_doctor.notify.slack import SlackNotifier
                notifiers.append(SlackNotifier(webhook, nc.channel))

            elif nc.type == "email":
                sender = nc.sender or _env_fallback("smtp_sender")
                recipients = nc.recipients or _env_fallback("smtp_recipients")
                password = nc.smtp_password or _env_fallback("smtp_password")
                missing = []
                if not sender:
                    missing.append(
                        f"sender (or one of: {', '.join(_ENV_FALLBACKS['smtp_sender'])})"
                    )
                if not recipients:
                    missing.append(
                        f"recipients (or one of: {', '.join(_ENV_FALLBACKS['smtp_recipients'])})"
                    )
                if missing:
                    raise ConfigError(
                        f"{label}: email notifier is missing required field(s): "
                        f"{'; '.join(missing)}."
                    )
                from flow_doctor.notify.email import EmailNotifier
                notifiers.append(EmailNotifier(
                    sender=sender,
                    recipients=recipients,
                    smtp_host=nc.smtp_host,
                    smtp_port=nc.smtp_port,
                    smtp_password=password,
                ))

            elif nc.type == "github":
                # Auto-issue toggle: when off, skip building this notifier
                # entirely so it files no issue (and never counts toward
                # dispatch attempts). The config block can stay in place.
                if not getattr(nc, "auto_create_issue", True):
                    continue
                repo = nc.repo or config.repo or _env_fallback("github_repo")
                token = (
                    nc.token
                    or (config.github.token if config.github else None)
                    or _env_fallback("github_token")
                )
                missing = []
                if not repo:
                    missing.append(
                        f"repo (or one of: {', '.join(_ENV_FALLBACKS['github_repo'])})"
                    )
                if not token:
                    missing.append(
                        f"token (or one of: {', '.join(_ENV_FALLBACKS['github_token'])})"
                    )
                if missing:
                    raise ConfigError(
                        f"{label}: github notifier is missing required field(s): "
                        f"{'; '.join(missing)}. "
                        f"The token must have Issues: write permission on the target repo."
                    )
                from flow_doctor.notify.github import GitHubNotifier
                labels = nc.labels or (config.github.labels if config.github else ["flow-doctor"])
                auto_fix_pr = getattr(nc, "auto_fix_pr", False)
                # The flow-doctor-fix GitHub Actions workflow triggers on an
                # `issues: labeled` event IN THE REPO IT LIVES IN — GitHub
                # gives no way to fire it from a differently-named repo's
                # issue without a self-built cross-repo relay. Redirecting
                # the issue destination (e.g. to a centralized backlog repo)
                # is fine for triage, but silently drops auto-fix — warn
                # loudly rather than let that surprise someone at incident
                # time. config.repo is this app's own repo (top-level
                # FlowDoctorConfig.repo); only compare when it's set.
                if auto_fix_pr and config.repo and repo != config.repo:
                    _logger.warning(
                        "%s: auto_fix_pr=True with repo=%r different from this "
                        "app's own repo=%r. The flow-doctor-fix Actions workflow "
                        "only fires on an issues:labeled event in the repo it "
                        "lives in, so no fix PR will be generated unless you've "
                        "built a cross-repo relay (e.g. repository_dispatch). "
                        "The issue will still be filed at %s for triage.",
                        label, repo, config.repo, repo,
                    )
                notifiers.append(GitHubNotifier(
                    repo=repo,
                    token=token,
                    labels=labels,
                    auto_fix_pr=auto_fix_pr,
                    fix_label=getattr(nc, "fix_label", "flow-doctor:fix"),
                ))

            elif nc.type == "s3":
                bucket = nc.bucket or _env_fallback("s3_bucket")
                subsystem = nc.subsystem
                missing = []
                if not bucket:
                    missing.append(
                        f"bucket (or one of: {', '.join(_ENV_FALLBACKS['s3_bucket'])})"
                    )
                if not subsystem:
                    missing.append(
                        "subsystem (one of the alpha-engine-config/changelog/vocab.yaml "
                        "subsystem values: retrieval, agents, predictor, executor, "
                        "backtester, dashboard, research, infrastructure, prompts, "
                        "eval, data_pipeline, telemetry)"
                    )
                if missing:
                    raise ConfigError(
                        f"{label}: s3 notifier is missing required field(s): "
                        f"{'; '.join(missing)}. "
                        f"The calling process's IAM principal must allow s3:PutObject "
                        f"on the bucket's changelog/entries/* prefix."
                    )
                from flow_doctor.notify.s3 import S3Notifier
                notifiers.append(S3Notifier(
                    bucket=bucket,
                    subsystem=subsystem,
                    entry_prefix=nc.entry_prefix,
                    default_root_cause_category=nc.default_root_cause_category,
                    default_resolution_type=nc.default_resolution_type,
                ))

            elif nc.type == "telegram":
                bot_token = nc.bot_token or _env_fallback("telegram_bot_token")
                raw_chat = nc.chat_id
                if raw_chat is None:
                    raw_chat = _env_fallback("telegram_chat_id")
                    # chat_id from env is a string; coerce to int when
                    # it's a numeric id (negative for channels/groups,
                    # positive for users) so the Bot API receives the
                    # right JSON type. ``@channelusername`` style stays str.
                    if raw_chat is not None and (
                        raw_chat.lstrip("-").isdigit()
                    ):
                        raw_chat = int(raw_chat)
                missing = []
                if not bot_token:
                    missing.append(
                        f"bot_token (or one of: "
                        f"{', '.join(_ENV_FALLBACKS['telegram_bot_token'])}). "
                        "Create one via @BotFather → /newbot"
                    )
                if raw_chat in (None, ""):
                    missing.append(
                        f"chat_id (or one of: "
                        f"{', '.join(_ENV_FALLBACKS['telegram_chat_id'])}). "
                        "After messaging the bot, look it up at "
                        "https://api.telegram.org/bot<TOKEN>/getUpdates"
                    )
                if missing:
                    raise ConfigError(
                        f"{label}: telegram notifier is missing required field(s): "
                        f"{'; '.join(missing)}."
                    )
                from flow_doctor.notify.telegram import TelegramNotifier
                notifiers.append(TelegramNotifier(
                    bot_token=bot_token,
                    chat_id=raw_chat,
                    message_thread_id=nc.message_thread_id,
                    parse_mode=nc.parse_mode,
                    disable_notification=nc.disable_notification,
                ))

            else:
                raise ConfigError(
                    f"{label}: unknown notifier type '{nc.type}'. "
                    f"Supported types: slack, email, github, s3, telegram."
                )

            # Stamp the per-notifier severity + category routing onto
            # whatever was just built this iteration (the github
            # auto_create_issue=False branch ``continue``s without
            # appending, so guard on the count).
            if len(notifiers) > before:
                notifiers[-1].notify_on = _normalize_notify_on(
                    getattr(nc, "notify_on", None)
                )
                notifiers[-1].notify_on_category = _normalize_notify_on_category(
                    getattr(nc, "notify_on_category", None)
                )

        return notifiers

    def _init_diagnosis(self, config: FlowDoctorConfig) -> None:
        """Initialize Phase 2 diagnosis components."""
        from flow_doctor.diagnosis.context import ContextAssembler
        from flow_doctor.diagnosis.knowledge_base import KnowledgeBase

        self._knowledge_base = KnowledgeBase(self._store)
        self._context_assembler = ContextAssembler(
            repo=config.repo,
            dependencies=config.dependencies,
        )

        if config.diagnosis.enabled and config.diagnosis.api_key:
            if config.diagnosis.provider == "openai_compat":
                try:
                    from flow_doctor.diagnosis.provider import OpenAICompatProvider
                    self._diagnosis_provider = OpenAICompatProvider(
                        api_key=config.diagnosis.api_key,
                        model=config.diagnosis.model,
                        base_url=config.diagnosis.base_url,
                        confidence_calibration=config.diagnosis.confidence_calibration,
                        timeout_seconds=config.diagnosis.timeout_seconds,
                        price_in_per_1m=config.diagnosis.price_in_per_1m,
                        price_out_per_1m=config.diagnosis.price_out_per_1m,
                        sft_sink_path=config.diagnosis.sft_sink_path,
                    )
                except ImportError:
                    print(
                        "[flow-doctor] WARNING: openai package not installed, diagnosis disabled. "
                        "Install with: pip install flow-doctor[diagnosis-openai]",
                        file=sys.stderr,
                    )
            elif config.diagnosis.provider == "anthropic":
                try:
                    from flow_doctor.diagnosis.provider import AnthropicProvider
                    self._diagnosis_provider = AnthropicProvider(
                        api_key=config.diagnosis.api_key,
                        model=config.diagnosis.model,
                        confidence_calibration=config.diagnosis.confidence_calibration,
                        timeout_seconds=config.diagnosis.timeout_seconds,
                        sft_sink_path=config.diagnosis.sft_sink_path,
                    )
                except ImportError:
                    print(
                        "[flow-doctor] WARNING: anthropic package not installed, diagnosis disabled. "
                        "Install with: pip install flow-doctor[diagnosis]",
                        file=sys.stderr,
                    )
            else:
                raise ConfigError(
                    f"diagnosis.provider must be 'anthropic' or 'openai_compat', "
                    f"got '{config.diagnosis.provider}'"
                )

    def _init_remediation(self, config: FlowDoctorConfig) -> None:
        """Initialize Phase 3 remediation components."""
        if not config.remediation.enabled:
            return

        try:
            from flow_doctor.remediation.decision_gate import DecisionGate, GateConfig
            from flow_doctor.remediation.executor import RemediationExecutor

            gate_config = GateConfig(
                auto_remediate_min_confidence=config.remediation.auto_remediate_min_confidence,
                fix_pr_min_confidence=config.remediation.fix_pr_min_confidence,
                max_auto_remediations_per_day=config.remediation.max_auto_remediations_per_day,
                max_auto_remediations_per_failure=config.remediation.max_auto_remediations_per_failure,
                deny_repos=list(config.remediation.deny_repos),
            )
            if not config.remediation.market_hours_lockout:
                gate_config.market_open_hour = 0
                gate_config.market_close_hour = 0

            self._decision_gate = DecisionGate(config=gate_config, store=self._store)

            # Preferred Telegram path (since 0.5.0rc3): build a real
            # TelegramNotifier from the config and hand it to the
            # executor. Falls back to the legacy webhook URL when only
            # that's configured (for 0.4.x yaml back-compat).
            telegram_notifier = None
            tg_token = (
                config.remediation.telegram_bot_token
                or _env_fallback("telegram_bot_token")
            )
            tg_chat = config.remediation.telegram_chat_id
            if tg_chat is None:
                env_chat = _env_fallback("telegram_chat_id")
                if env_chat is not None and env_chat.lstrip("-").isdigit():
                    tg_chat = int(env_chat)
                elif env_chat is not None:
                    tg_chat = env_chat
            if tg_token and tg_chat not in (None, ""):
                from flow_doctor.notify.telegram import TelegramNotifier
                telegram_notifier = TelegramNotifier(
                    bot_token=tg_token,
                    chat_id=tg_chat,
                    message_thread_id=config.remediation.telegram_message_thread_id,
                )

            self._remediation_executor = RemediationExecutor(
                dry_run=config.remediation.dry_run,
                store=self._store,
                telegram_notifier=telegram_notifier,
                telegram_webhook_url=config.remediation.telegram_webhook_url,
            )
        except Exception as e:
            print(f"[flow-doctor] Remediation init failed: {e}", file=sys.stderr)

    def report(
        self,
        error: Any = None,
        *,
        severity: str = Severity.ERROR.value,
        context: Optional[Dict[str, Any]] = None,
        logs: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Optional[str]:
        """Report an error or message. Never crashes the caller.

        Args:
            error: An exception, string message, or None.
            severity: One of 'critical', 'error', 'warning'.
            context: Arbitrary key-value metadata.
            logs: Log text to attach.
            message: Explicit message string (alternative to passing a string as error).

        Returns:
            The report ID, or None if suppressed by dedup.
        """
        try:
            return self._do_report(error, severity=severity, context=context, logs=logs, message=message)
        except Exception as exc:
            # report() must NEVER crash the caller
            print(f"[flow-doctor] Internal error in report(): {exc}", file=sys.stderr)
            return None

    def notify_success(
        self,
        subject: str,
        body: Optional[str] = None,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Send a healthy-completion / success ping (``Severity.INFO``).

        The public success-path API (since 0.6.0) — closes the gap that
        previously forced consumers to reach into ``fd._notifiers`` to send
        a "pipeline finished OK" message. The ping is persisted like any
        report and routed through the normal notifier dispatch, but at
        ``INFO`` severity it never triggers dedup, diagnosis, or
        remediation, and reaches ONLY notifiers whose ``notify_on`` includes
        ``"info"`` (e.g. ``TelegramNotifierConfig(notify_on=["critical",
        "error", "info"])``). Channels left on the default routing are
        unaffected — no success spam on your failure-only channels.

        Args:
            subject: Short success headline (becomes the message body's lead).
            body: Optional detail text (rendered like attached logs).
            context: Arbitrary key-value metadata.

        Returns:
            The report ID, or None on internal error. Never raises.
        """
        try:
            enriched_context = self._build_context(context)
            report = Report(
                flow_name=self.config.flow_name,
                severity=Severity.INFO.value,
                error_message=subject,
                logs=body,
                context=enriched_context,
            )
            if self._store:
                self._store.save_report(report)
            self._send_notifications(report, is_cascade=False, diagnosis=None)
            return report.id
        except Exception as exc:
            print(f"[flow-doctor] notify_success() error: {exc}", file=sys.stderr)
            return None

    def notify_event(
        self,
        subject: str,
        body: Optional[str] = None,
        *,
        severity: str = Severity.INFO.value,
        context: Optional[Dict[str, Any]] = None,
        dedup_key: Optional[str] = None,
    ) -> Optional[str]:
        """Emit an intentional non-error event (trade alert, SF milestone, etc.).

        Unlike :meth:`notify_success` (always ``INFO`` completion pings), this
        API accepts any ``severity`` and optional ``dedup_key`` for cross-call
        deduplication — the path fleet producers use for Telegram routing
        without misusing ``report()`` on non-exception traffic.

        When ``dedup_key`` is set, the signature is derived from it (same
        normalization as log-message dedup). When unset, no dedup is applied
        (mirrors :meth:`notify_success`). Diagnosis and remediation run only
        for ``critical`` / ``error`` severities.

        Args:
            subject: Short headline (``Report.error_message``).
            body: Optional detail text (``Report.logs``).
            severity: One of ``critical``, ``error``, ``warning``, ``info``.
            context: Arbitrary key-value metadata.
            dedup_key: Optional stable key for dedup/rate-limit signature.

        Returns:
            The report ID, ``None`` if suppressed by dedup or on internal error.
            Never raises.
        """
        try:
            return self._do_notify_event(
                subject,
                body,
                severity=severity,
                context=context,
                dedup_key=dedup_key,
            )
        except Exception as exc:
            print(f"[flow-doctor] notify_event() error: {exc}", file=sys.stderr)
            return None

    async def notify_event_async(
        self,
        subject: str,
        body: Optional[str] = None,
        *,
        severity: str = Severity.INFO.value,
        context: Optional[Dict[str, Any]] = None,
        dedup_key: Optional[str] = None,
    ) -> Optional[str]:
        """Async counterpart of :meth:`notify_event`."""
        import asyncio

        try:
            return await asyncio.to_thread(
                self.notify_event,
                subject,
                body,
                severity=severity,
                context=context,
                dedup_key=dedup_key,
            )
        except Exception as exc:
            print(f"[flow-doctor] notify_event_async() error: {exc}", file=sys.stderr)
            return None

    def last_dispatch_reason(self) -> Optional[str]:
        """``DecisionReason`` value for the most recent report()/notify_event()/
        notify_success() call made on this instance, or ``None`` if none has
        run yet.

        A non-``None`` report id from those calls means "seen and evaluated",
        NOT "delivered" — e.g. ``severity_filtered``/``category_filtered``/
        ``rate_limited``/``delivery_failed``/``no_notifiers`` all also return
        a report id. Check this (or :meth:`last_dispatched`) before logging a
        "sent" message. One value per call; not meaningful under concurrent
        calls to the same ``FlowDoctor`` instance from multiple threads.
        """
        return self._last_dispatch_reason

    def last_dispatched(self) -> bool:
        """True iff the most recent report()/notify_event()/notify_success()
        call actually reached >=1 notifier (``DecisionReason.FIRED``).

        False for every other outcome, including a call that returned a
        non-``None`` report id (severity_filtered, category_filtered,
        rate_limited, delivery_failed, no_notifiers) and for a call that
        hasn't run yet. This is the check a caller wants before logging
        "alert sent" — a report id alone does not mean delivery happened.
        """
        return self._last_dispatch_reason == DecisionReason.FIRED.value

    def _do_notify_event(
        self,
        subject: str,
        body: Optional[str],
        *,
        severity: str,
        context: Optional[Dict[str, Any]],
        dedup_key: Optional[str],
    ) -> Optional[str]:
        """Internal notify_event implementation with optional dedup."""
        enriched_context = self._build_context(context)
        error_signature: Optional[str] = None
        if dedup_key:
            error_signature = compute_signature_from_message(dedup_key)

        if error_signature and self._dedup:
            is_dup, existing_id = self._dedup.is_duplicate(error_signature)
            if is_dup and existing_id:
                self._dedup.record_dedup_hit(existing_id)
                self._record_decision(
                    report_id=existing_id,
                    error_signature=error_signature,
                    reason=DecisionReason.DEDUPED.value,
                )
                return None

        report = Report(
            flow_name=self.config.flow_name,
            severity=severity,
            error_message=subject,
            logs=body,
            context=enriched_context,
            error_signature=error_signature,
        )
        if self._store:
            self._store.save_report(report)

        diagnosis = None
        if severity in (Severity.CRITICAL.value, Severity.ERROR.value):
            diagnosis = self._run_diagnosis(report, cascade_source=None)

        dispatch = self._send_notifications(report, is_cascade=False, diagnosis=diagnosis)
        self._record_decision(
            report_id=report.id,
            error_signature=error_signature,
            reason=_classify_dispatch(dispatch),
        )
        return report.id

    async def notify_success_async(
        self,
        subject: str,
        body: Optional[str] = None,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Async counterpart of :meth:`notify_success`.

        Offloads the sync persist / notify work to a thread so an async
        caller's event loop stays unblocked; the current ``flow_doctor
        .context()`` scope propagates via ``contextvars``.
        """
        import asyncio

        try:
            return await asyncio.to_thread(
                self.notify_success, subject, body, context=context
            )
        except Exception as exc:
            print(
                f"[flow-doctor] notify_success_async() error: {exc}",
                file=sys.stderr,
            )
            return None

    async def report_async(
        self,
        error: Any = None,
        *,
        severity: str = Severity.ERROR.value,
        context: Optional[Dict[str, Any]] = None,
        logs: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Optional[str]:
        """Async counterpart of :meth:`report`.

        Offloads the sync persist / notify / diagnosis work to a thread
        so an async caller's event loop stays unblocked. The thread
        inherits the current ``contextvars`` automatically (``asyncio.to_thread``
        uses ``contextvars.copy_context()``), so any ``flow_doctor.context()``
        scope active in the caller propagates to the recorded report.
        """
        import asyncio

        try:
            return await asyncio.to_thread(
                self._do_report,
                error,
                severity=severity,
                context=context,
                logs=logs,
                message=message,
            )
        except Exception as exc:
            print(
                f"[flow-doctor] Internal error in report_async(): {exc}",
                file=sys.stderr,
            )
            return None

    def _do_report(
        self,
        error: Any,
        *,
        severity: str,
        context: Optional[Dict[str, Any]],
        logs: Optional[str],
        message: Optional[str],
    ) -> Optional[str]:
        """Internal report implementation."""
        # Build the report
        error_type: Optional[str] = None
        error_message: str = ""
        traceback_str: Optional[str] = None
        error_signature: Optional[str] = None

        if isinstance(error, BaseException):
            error_type = type(error).__qualname__
            error_message = str(error)
            if error.__traceback__:
                traceback_str = "".join(tb_module.format_exception(type(error), error, error.__traceback__))
            else:
                traceback_str = "".join(tb_module.format_exception_only(type(error), error))
            error_signature = compute_signature_from_exception(error)
        elif isinstance(error, str):
            error_message = error
            error_signature = compute_error_signature(None, None)
        elif error is None and message:
            error_message = message
            error_signature = compute_error_signature(None, None)
        elif error is None:
            error_message = "Unknown error"
            error_signature = compute_error_signature(None, None)
        else:
            error_message = str(error)
            error_signature = compute_error_signature(None, None)

        # For non-exception string reports, normalize variable tokens
        # (reqIds, conIds, contract symbols, UUIDs) before hashing so that
        # repeated errors differing only in per-call identifiers collapse
        # to one signature and the cooldown window actually engages.
        if error_type is None:
            error_signature = compute_signature_from_message(error_message)

        # Attach captured logs
        captured_logs = logs
        if captured_logs is None and self._log_handler is not None:
            captured_logs = "\n".join(self._log_handler.records)

        # Scrub secrets from traceback and context
        if traceback_str:
            traceback_str = self._scrubber.scrub_string(traceback_str)
        enriched_context = self._build_context(context)

        # Dedup check
        is_dup, existing_id = self._dedup.is_duplicate(error_signature)
        if is_dup and existing_id:
            self._dedup.record_dedup_hit(existing_id)
            self._record_decision(
                report_id=existing_id,
                error_signature=error_signature,
                reason=DecisionReason.DEDUPED.value,
            )
            return None

        # Cascade check
        cascade_source = self._cascade_detector.check_cascade(
            self.config.dependencies,
            self.config.flow_name,
        )

        report = Report(
            flow_name=self.config.flow_name,
            severity=severity,
            error_type=error_type,
            error_message=error_message,
            traceback=traceback_str,
            logs=captured_logs,
            context=enriched_context,
            error_signature=error_signature,
            cascade_source=cascade_source,
        )

        # Persist (always)
        self._store.save_report(report)

        # Phase 2: Diagnosis
        diagnosis = self._run_diagnosis(report, cascade_source)

        # Send notifications (enriched with diagnosis if available)
        dispatch = self._send_notifications(report, cascade_source is not None, diagnosis)

        # Record the dispatch decision so a quiet flow is "saw N, alerted M"
        # rather than indistinguishable from "never ran" (the observability gap).
        self._record_decision(
            report_id=report.id,
            error_signature=error_signature,
            reason=_classify_dispatch(dispatch),
            detail="cascade" if cascade_source else None,
        )

        # Phase 3: Decision gate + remediation
        if diagnosis and self._decision_gate:
            self._run_remediation(report, diagnosis)

        return report.id

    def _record_decision(
        self,
        *,
        reason: str,
        report_id: Optional[str] = None,
        error_signature: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        """Persist one dispatch decision and log it at INFO.

        Best-effort: a store/log failure here must never break the report
        path, but it IS logged at WARNING so the observability layer itself
        fails loud rather than silently.
        """
        # Recorded even if the store write below fails — callers reading
        # last_dispatch_reason() care about the in-process outcome of THIS
        # call, not whether persistence succeeded.
        self._last_dispatch_reason = reason
        sig8 = (error_signature or "")[:8]
        try:
            self._store.save_decision(
                Decision(
                    flow_name=self.config.flow_name,
                    reason=reason,
                    report_id=report_id,
                    error_signature=error_signature,
                    detail=detail,
                )
            )
        except Exception as e:  # noqa: BLE001 - observability must not break reporting
            _logger.warning(
                "flow-doctor: failed to persist decision (reason=%s sig=%s): %s",
                reason, sig8, e,
            )
        _logger.info(
            "flow-doctor decision [%s] reason=%s sig=%s report=%s%s",
            self.config.flow_name, reason, sig8, report_id or "-",
            f" detail={detail}" if detail else "",
        )

    def _build_context(self, user_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Build enriched context with system info and scrubbed env vars."""
        ctx: Dict[str, Any] = {}

        # System info
        ctx["python_version"] = platform.python_version()
        ctx["os"] = f"{platform.system()} {platform.release()}"
        ctx["flow_name"] = self.config.flow_name

        # Scrubbed environment variables (only a relevant subset)
        env_subset = {}
        for key in sorted(os.environ):
            # Only include a reasonable subset
            if key.startswith(("AWS_", "FLOW_", "PYTHON", "PATH", "HOME", "USER", "LANG")):
                env_subset[key] = os.environ[key]
        ctx["environment"] = self._scrubber.scrub_env_vars(env_subset)

        # Auto-pick contextvars stamped via flow_doctor.context(...).
        # Inner scopes shadow outer ones; the keys land at the top level
        # so downstream notifiers and digests can surface ``stage``
        # without crawling into ``user``.
        from flow_doctor.core._context import current_context as _current_context

        ambient = _current_context()
        if ambient:
            # ``flow_name`` here overrides the static config value when the
            # caller explicitly stamped a different flow_name — useful for
            # multi-tenant pipelines that share a single FlowDoctor.
            for k, v in ambient.items():
                ctx[k] = v

        # User-supplied context
        if user_context:
            ctx["user"] = self._scrubber.scrub_dict(user_context)

        return ctx

    def _run_diagnosis(
        self,
        report: Report,
        cascade_source: Optional[str],
    ) -> Optional[Diagnosis]:
        """Run Phase 2 diagnosis pipeline. Returns Diagnosis or None."""
        # Skip diagnosis for warnings and cascades
        if report.severity == Severity.WARNING.value:
            return None
        if cascade_source:
            return None
        if not self._knowledge_base:
            return None

        try:
            # 1. Check knowledge base first (free, no LLM call)
            kb_diagnosis = self._knowledge_base.lookup(
                report.error_signature, report.id, report.flow_name
            )
            if kb_diagnosis:
                self._store.save_diagnosis(kb_diagnosis)
                return kb_diagnosis

            # 2. Rate limit check for LLM diagnosis
            if not self._diagnosis_provider or not self._rate_limiter:
                return None
            decision = self._rate_limiter.check("diagnosis")
            if decision == "degrade":
                # Log the degraded diagnosis action
                action = Action(
                    report_id=report.id,
                    action_type="diagnosis",
                    status=ActionStatus.DEGRADED.value,
                    target="degraded - diagnosis rate limited",
                )
                self._store.save_action(action)
                return None

            # 2b. Daily cost cap check
            max_cost = self.config.diagnosis.max_daily_cost_usd
            if max_cost > 0:
                daily_cost = self._store.get_daily_diagnosis_cost()
                if daily_cost >= max_cost:
                    action = Action(
                        report_id=report.id,
                        action_type="diagnosis",
                        status=ActionStatus.DEGRADED.value,
                        target=f"degraded - daily cost cap ${max_cost:.2f} reached (spent ${daily_cost:.2f})",
                    )
                    self._store.save_action(action)
                    return None

            # 3. Assemble context and call LLM
            git_context = self._load_git_context()
            context = self._context_assembler.assemble(
                report=report,
                git_context=git_context,
            )
            diagnosis = self._diagnosis_provider.diagnose(context, self._context_assembler)
            diagnosis.report_id = report.id

            # 4. Save diagnosis and record the action
            self._store.save_diagnosis(diagnosis)
            action = Action(
                report_id=report.id,
                action_type="diagnosis",
                status=ActionStatus.SENT.value,
                diagnosis_id=diagnosis.id,
            )
            self._store.save_action(action)

            return diagnosis

        except Exception as e:
            print(f"[flow-doctor] Diagnosis failed: {e}", file=sys.stderr)
            return None

    def _load_git_context(self) -> Optional[dict]:
        """Load git context for diagnosis, preferring local over GitHub API."""
        try:
            from flow_doctor.diagnosis.git_context import GitContextLoader

            # Try local git first
            ctx = GitContextLoader.load_local()
            if ctx:
                return ctx

            # Fall back to GitHub API if repo and token are configured
            if self.config.repo and self.config.github and self.config.github.token:
                return GitContextLoader.load_github(
                    self.config.repo, self.config.github.token
                )
        except Exception:
            pass
        return None

    def _run_remediation(self, report: Report, diagnosis: Diagnosis) -> None:
        """Run the decision gate and execute remediation if appropriate."""
        try:
            decision = self._decision_gate.decide(
                diagnosis=diagnosis,
                error_type=report.error_type,
                error_message=report.error_message,
                flow_name=report.flow_name,
            )

            if decision.decision_type.value == "auto_remediate" and self._remediation_executor:
                result = self._remediation_executor.execute(decision)
                if not result.success and not result.dry_run:
                    print(
                        f"[flow-doctor] Remediation failed: {result.error}",
                        file=sys.stderr,
                    )
            elif decision.decision_type.value == "generate_fix_pr":
                # PR generation is handled separately (Phase 4)
                # Log the decision for now
                if self._store:
                    self._store.save_remediation_action(
                        report_id=report.id,
                        diagnosis_id=diagnosis.id,
                        decision_type="generate_fix_pr",
                        playbook_pattern=decision.playbook_match.name if decision.playbook_match else None,
                    )
            elif decision.decision_type.value == "escalate":
                if self._store:
                    self._store.save_remediation_action(
                        report_id=report.id,
                        diagnosis_id=diagnosis.id,
                        decision_type="escalate",
                        playbook_pattern=decision.playbook_match.name if decision.playbook_match else None,
                        output=decision.reason,
                    )

        except Exception as e:
            print(f"[flow-doctor] Remediation pipeline error: {e}", file=sys.stderr)

    def _send_notifications(
        self,
        report: Report,
        is_cascade: bool,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Dict[str, int]:
        """Send notifications, respecting rate limits.

        Returns a dispatch tally ``{attempted, sent, failed, degraded,
        severity_skipped, category_skipped}`` so the caller can record
        exactly why this error did or did not produce an alert.

        Failures are logged at CRITICAL via the ``flow_doctor`` logger so
        they surface in the host app's log stream (journalctl, Sentry,
        Datadog, etc.) instead of only printing to stderr. An aggregate
        CRITICAL is emitted if *all* notifiers failed for this report,
        which is the "flow-doctor itself is broken" signal users most
        need to see.

        Severity routing is per-notifier: each notifier receives this report
        only if ``report.severity`` is in its effective ``notify_on`` set
        (its config's ``notify_on``, or the default {critical, error}). So
        warnings and healthy-completion ``info`` pings reach only the
        channels that explicitly opt in, while critical/error still fan out
        everywhere by default. A notifier skipped by severity does not count
        toward attempted/failed — it intentionally isn't a recipient.

        Category routing is per-notifier and applies AFTER severity: a
        notifier with ``notify_on_category`` set only receives reports whose
        diagnosis (Phase 2, optional) lands in one of those categories. This
        is what lets a curated/noisy channel (e.g. a GitHub issue tracker
        feeding a human backlog) opt in to only human-actionable categories
        (CODE/CONFIG) while a cheap paging channel (Telegram/SNS) still fans
        out on the rest (TRANSIENT/EXTERNAL/INFRA). If diagnosis is
        unavailable for this report (feature disabled, or the diagnosis call
        itself failed), the category gate is skipped entirely and the
        notifier fires as if ``notify_on_category`` were unset — a report
        that failed to get diagnosed must never be silently dropped by a
        gate that depends on that diagnosis.
        """
        attempted = 0
        sent = 0
        degraded = 0
        severity_skipped = 0
        category_skipped = 0
        failed: List[str] = []

        for notifier in self._notifiers:
            effective_notify_on = notifier.notify_on or _DEFAULT_NOTIFY_ON
            if report.severity not in effective_notify_on:
                severity_skipped += 1
                continue

            if notifier.notify_on_category and diagnosis is not None:
                report_category = (diagnosis.category or "").strip().upper()
                if report_category not in notifier.notify_on_category:
                    category_skipped += 1
                    continue

            from flow_doctor.notify.slack import SlackNotifier
            from flow_doctor.notify.email import EmailNotifier
            from flow_doctor.notify.github import GitHubNotifier
            from flow_doctor.notify.s3 import S3Notifier
            from flow_doctor.notify.telegram import TelegramNotifier

            if isinstance(notifier, SlackNotifier):
                action_type = ActionType.SLACK_ALERT.value
            elif isinstance(notifier, EmailNotifier):
                action_type = ActionType.EMAIL_ALERT.value
            elif isinstance(notifier, GitHubNotifier):
                action_type = ActionType.GITHUB_ISSUE.value
            elif isinstance(notifier, S3Notifier):
                action_type = ActionType.S3_ALERT.value
            elif isinstance(notifier, TelegramNotifier):
                action_type = ActionType.TELEGRAM_ALERT.value
            else:
                action_type = "unknown_alert"

            # Rate limit check
            decision = self._rate_limiter.check(action_type)
            if decision == "degrade":
                action = Action(
                    report_id=report.id,
                    action_type=action_type,
                    status=ActionStatus.DEGRADED.value,
                    target="degraded - queued for digest",
                    diagnosis_id=diagnosis.id if diagnosis else None,
                )
                self._store.save_action(action)
                degraded += 1
                continue

            attempted += 1

            # Send. Notifier.send() returns Optional[str] — a target
            # identifier (URL, email recipients, channel) on success, or
            # None on failure. The target is persisted in actions.target
            # so operators can link back to the filed issue / sent email
            # from the DB.
            try:
                target = notifier.send(report, self.config.flow_name, diagnosis)
                action = Action(
                    report_id=report.id,
                    action_type=action_type,
                    status=ActionStatus.SENT.value if target else ActionStatus.FAILED.value,
                    target=target,
                    diagnosis_id=diagnosis.id if diagnosis else None,
                )
                self._store.save_action(action)
                if not target:
                    failed.append(action_type)
                    _logger.critical(
                        "flow-doctor notifier %s returned failure for report %s "
                        "(notifier-specific reason logged separately)",
                        action_type, report.id,
                    )
                else:
                    sent += 1
                    _logger.info(
                        "flow-doctor: notifier %s dispatched report %s -> %s",
                        action_type, report.id, target,
                    )
            except Exception as e:
                failed.append(action_type)
                _logger.critical(
                    "flow-doctor notifier %s raised while sending report %s: %s",
                    action_type, report.id, e, exc_info=True,
                )
                action = Action(
                    report_id=report.id,
                    action_type=action_type,
                    status=ActionStatus.FAILED.value,
                    diagnosis_id=diagnosis.id if diagnosis else None,
                )
                self._store.save_action(action)

        # Aggregate signal: all notifiers failed for this report. This is
        # the "error monitoring is itself broken" case — users MUST see it.
        if attempted > 0 and len(failed) == attempted:
            _logger.critical(
                "flow-doctor: ALL %d notifier(s) failed for report %s (%s). "
                "Error monitoring is currently broken — check flow-doctor "
                "configuration and notifier credentials.",
                attempted, report.id, ", ".join(failed),
            )

        return {
            "attempted": attempted,
            "sent": sent,
            "failed": len(failed),
            "degraded": degraded,
            "severity_skipped": severity_skipped,
            "category_skipped": category_skipped,
        }

    @contextmanager
    def guard(self):
        """Context manager that reports and re-raises any exception.

        Usage:
            with fd.guard():
                run_pipeline()
        """
        try:
            yield
        except Exception as exc:
            try:
                self.report(exc)
            except Exception:
                pass  # report() already guards itself, but belt-and-suspenders
            raise

    def monitor(self, func: Optional[Callable] = None, **kwargs: Any) -> Any:
        """Decorator that reports and re-raises any exception.

        Usage:
            @fd.monitor
            def handler(event, context):
                ...

            # or with arguments:
            @fd.monitor
            def my_func():
                ...
        """
        if func is None:
            # Called with arguments: @fd.monitor(...)
            def decorator(f: Callable) -> Callable:
                return self._wrap_monitor(f)
            return decorator

        # Called without arguments: @fd.monitor
        return self._wrap_monitor(func)

    def _wrap_monitor(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kw: Any) -> Any:
            try:
                return func(*args, **kw)
            except Exception as exc:
                try:
                    self.report(exc)
                except Exception:
                    pass
                raise
        return wrapper

    @contextmanager
    def capture_logs(self, level: int = logging.INFO, logger_name: Optional[str] = None):
        """Context manager that captures log records for attachment to reports.

        Usage:
            with fd.capture_logs():
                logger.info("Starting scan...")
                # ... all logs buffered
        """
        handler = _LogCaptureHandler(level)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

        target_logger = logging.getLogger(logger_name)
        target_logger.addHandler(handler)

        prev_handler = self._log_handler
        self._log_handler = handler
        try:
            yield handler
        finally:
            target_logger.removeHandler(handler)
            self._log_handler = prev_handler

    def get_handler(self, level: int = logging.ERROR, **kwargs: Any) -> "FlowDoctorHandler":
        """Return a logging.Handler that reports ERROR+ records via Flow Doctor.

        Merges defaults from config.handler if present. kwargs override everything.
        """
        from flow_doctor.core.handler import FlowDoctorHandler

        handler_kwargs: Dict[str, Any] = {}
        if self.config.handler:
            handler_kwargs["level"] = getattr(logging, self.config.handler.level, logging.ERROR)
            handler_kwargs["include_patterns"] = self.config.handler.include_patterns
            handler_kwargs["exclude_patterns"] = self.config.handler.exclude_patterns
            handler_kwargs["queue_size"] = self.config.handler.queue_size

        # Explicit level arg overrides config
        handler_kwargs["level"] = level
        handler_kwargs.update(kwargs)

        return FlowDoctorHandler(self, **handler_kwargs)

    def history(self, limit: int = 10) -> List[Report]:
        """Get recent reports for this flow."""
        try:
            return self._store.get_reports(flow_name=self.config.flow_name, limit=limit)
        except Exception as e:
            print(f"[flow-doctor] history() error: {e}", file=sys.stderr)
            return []

    def status(self) -> Dict[str, Any]:
        """Return a summary of flow-doctor's current state.

        Useful for health checks, EOD summaries, and verifying flow-doctor is active.
        """
        result: Dict[str, Any] = {
            "healthy": self._healthy,
            "flow_name": self.config.flow_name,
            "store": self.config.store.type,
        }

        if not self._healthy or not self._store:
            return result

        try:
            result["reports_today"] = self._store.count_reports_today(self.config.flow_name)
            result["diagnoses_today"] = self._store.count_diagnoses_today()
            result["diagnosis_cost_today_usd"] = self._store.get_daily_diagnosis_cost()
            result["remediations_today"] = self._store.count_remediations_today()
            result["diagnosis_enabled"] = self.config.diagnosis.enabled
            result["remediation_enabled"] = self.config.remediation.enabled
            result["remediation_dry_run"] = self.config.remediation.dry_run
            result["notifiers"] = len(self._notifiers)
            result["max_daily_cost_usd"] = self.config.diagnosis.max_daily_cost_usd
            # Decision breakdown — the heartbeat's core: every error seen today,
            # keyed by what flow-doctor decided to do with it. Lets an operator
            # tell "quiet because nothing failed" from "quiet because suppressed".
            breakdown = self._store.decision_breakdown_today(self.config.flow_name)
            result["decisions_today"] = breakdown
            result["errors_seen_today"] = sum(breakdown.values())
        except Exception as e:
            result["status_error"] = str(e)

        return result

    def log_summary(self, logger: Optional[logging.Logger] = None) -> str:
        """Log a one-line summary of today's activity. Returns the summary string.

        Call this at EOD, on shutdown, or periodically to confirm flow-doctor is alive.
        """
        s = self.status()
        parts = [f"flow-doctor [{s.get('flow_name', '?')}]"]

        if not s.get("healthy"):
            parts.append("DEGRADED")
        else:
            # Lead with the seen/alerted/suppressed heartbeat so "alive but
            # quiet" is legible at a glance, then the cost/remediation detail.
            breakdown = s.get("decisions_today", {}) or {}
            parts.append(f"seen={s.get('errors_seen_today', 0)}")
            parts.append(f"fired={breakdown.get(DecisionReason.FIRED.value, 0)}")
            suppressed = sum(
                v for k, v in breakdown.items() if k != DecisionReason.FIRED.value
            )
            if suppressed:
                detail = ",".join(
                    f"{k}={v}" for k, v in sorted(breakdown.items())
                    if k != DecisionReason.FIRED.value
                )
                parts.append(f"suppressed={suppressed}({detail})")
            parts.append(f"reports={s.get('reports_today', 0)}")
            parts.append(f"diagnoses={s.get('diagnoses_today', 0)}")
            cost = s.get("diagnosis_cost_today_usd", 0)
            if cost > 0:
                parts.append(f"cost=${cost:.3f}")
            rem = s.get("remediations_today", 0)
            if rem > 0:
                parts.append(f"remediations={rem}")

        summary = " | ".join(parts)
        target = logger or logging.getLogger("flow_doctor")
        target.info(summary)
        return summary

    def emit_heartbeat(
        self,
        bucket: str,
        *,
        prefix: Optional[str] = None,
    ) -> Optional[str]:
        """Write this flow's end-of-run heartbeat (``status()``) to S3.

        Companion to ``log_summary()``: ``log_summary()`` makes the
        seen/fired/suppressed heartbeat legible in CloudWatch/journalctl;
        ``emit_heartbeat()`` lands the same ``status()`` snapshot at
        ``s3://{bucket}/{prefix}/{flow}/{date}.json`` so a dashboard
        System Health panel can read it (config#646). Call at end-of-run.

        Returns the ``s3://`` URI written, or ``None`` on any failure — a
        heartbeat write never raises into the calling pipeline (the write
        primitive soft-fails by design).
        """
        from flow_doctor.notify.s3 import HEARTBEAT_PREFIX, write_heartbeat

        return write_heartbeat(
            self.status(),
            bucket=bucket,
            flow_name=self.config.flow_name,
            prefix=prefix if prefix is not None else HEARTBEAT_PREFIX,
        )

    def _log_startup(self) -> None:
        """Log startup info so operators can confirm flow-doctor is active."""
        components = []
        components.append(f"store={self.config.store.type}")
        components.append(f"notifiers={len(self._notifiers)}")
        if self.config.diagnosis.enabled:
            components.append(f"diagnosis=on(max_cost=${self.config.diagnosis.max_daily_cost_usd:.2f}/day)")
        else:
            components.append("diagnosis=off")
        if self.config.remediation.enabled:
            mode = "dry-run" if self.config.remediation.dry_run else "LIVE"
            components.append(f"remediation={mode}")
        else:
            components.append("remediation=off")

        msg = f"[flow-doctor] initialized: {self.config.flow_name} ({', '.join(components)})"
        logging.getLogger("flow_doctor").info(msg)

    def digest(self, since: Optional[datetime] = None) -> Optional[str]:
        """Generate and optionally send the daily digest.

        Args:
            since: Cutoff time. Defaults to 24 hours ago.

        Returns:
            The digest content string, or None if nothing to report.
        """
        try:
            if not self._digest_generator:
                return None
            content = self._digest_generator.generate(since)
            if content and self._notifiers:
                self._digest_generator.send(
                    self._notifiers, self.config.flow_name, since
                )
            return content
        except Exception as e:
            print(f"[flow-doctor] digest() error: {e}", file=sys.stderr)
            return None

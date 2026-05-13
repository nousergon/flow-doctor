"""FlowDoctor client: init(), report(), guard(), monitor(), capture_logs()."""

from __future__ import annotations

import functools
import logging
import os
import platform
import sys
import traceback as tb_module
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from flow_doctor.core.builder import FlowDoctorBuilder

from flow_doctor.core.config import FlowDoctorConfig, load_config
from flow_doctor.core.dedup import (
    DedupChecker,
    compute_error_signature,
    compute_signature_from_exception,
    compute_signature_from_message,
)
from flow_doctor.core.errors import ConfigError
from flow_doctor.core.models import Action, ActionStatus, ActionType, Diagnosis, Report, Severity
from flow_doctor.core.rate_limiter import CascadeDetector, RateLimiter
from flow_doctor.core.scrubber import Scrubber
from flow_doctor.notify.base import Notifier
from flow_doctor.storage.base import StorageBackend

# Module logger — used to surface notifier failures to the host app's log stream
# instead of only printing to stderr. Host apps catch CRITICAL records via their
# own logging configuration (journalctl, Sentry, Datadog, etc.).
_logger = logging.getLogger("flow_doctor")

# Env var fallback chains. Each notifier field falls back through this list,
# stopping at the first non-empty value. FLOW_DOCTOR_* names are the canonical
# contract; the others are conveniences that pick up common conventions like
# the `gh` CLI's GH_TOKEN or GitHub Actions' GITHUB_TOKEN.
_ENV_FALLBACKS: Dict[str, List[str]] = {
    "github_token": ["FLOW_DOCTOR_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"],
    "github_repo": ["FLOW_DOCTOR_GITHUB_REPO"],
    "smtp_password": ["FLOW_DOCTOR_SMTP_PASSWORD", "GMAIL_APP_PASSWORD"],
    "smtp_sender": ["FLOW_DOCTOR_SMTP_SENDER", "EMAIL_SENDER"],
    "smtp_recipients": ["FLOW_DOCTOR_SMTP_RECIPIENTS", "EMAIL_RECIPIENTS"],
    "slack_webhook": ["FLOW_DOCTOR_SLACK_WEBHOOK", "SLACK_WEBHOOK_URL"],
    "anthropic_api_key": ["FLOW_DOCTOR_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"],
    "s3_bucket": ["FLOW_DOCTOR_S3_BUCKET", "CHANGELOG_BUCKET"],
}


def _env_fallback(key: str) -> Optional[str]:
    """Return the first non-empty env var from the fallback chain for ``key``."""
    for name in _ENV_FALLBACKS.get(key, []):
        value = os.environ.get(name)
        if value:
            return value
    return None


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

    def __init__(self, config: FlowDoctorConfig, *, strict: bool = True):
        """Initialize a FlowDoctor instance.

        Args:
            config: Loaded config object.
            strict: If True (default), any initialization failure raises.
                If False, init errors are printed to stderr and the instance
                operates in degraded mode (no notifiers, ``_healthy=False``).
                The strict default is intentional: silent degradation means
                users discover broken error monitoring only during an actual
                incident, which defeats the purpose of the tool.
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
            store.init_schema()
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
                notifiers.append(GitHubNotifier(repo=repo, token=token, labels=labels))

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

            else:
                raise ConfigError(
                    f"{label}: unknown notifier type '{nc.type}'. "
                    f"Supported types: slack, email, github, s3."
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
            try:
                from flow_doctor.diagnosis.provider import AnthropicProvider
                self._diagnosis_provider = AnthropicProvider(
                    api_key=config.diagnosis.api_key,
                    model=config.diagnosis.model,
                    confidence_calibration=config.diagnosis.confidence_calibration,
                    timeout_seconds=config.diagnosis.timeout_seconds,
                )
            except ImportError:
                print(
                    "[flow-doctor] WARNING: anthropic package not installed, diagnosis disabled. "
                    "Install with: pip install flow-doctor[diagnosis]",
                    file=sys.stderr,
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
            self._remediation_executor = RemediationExecutor(
                dry_run=config.remediation.dry_run,
                store=self._store,
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
        self._send_notifications(report, cascade_source is not None, diagnosis)

        # Phase 3: Decision gate + remediation
        if diagnosis and self._decision_gate:
            self._run_remediation(report, diagnosis)

        return report.id

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
    ) -> None:
        """Send notifications, respecting rate limits.

        Failures are logged at CRITICAL via the ``flow_doctor`` logger so
        they surface in the host app's log stream (journalctl, Sentry,
        Datadog, etc.) instead of only printing to stderr. An aggregate
        CRITICAL is emitted if *all* notifiers failed for this report,
        which is the "flow-doctor itself is broken" signal users most
        need to see.
        """
        # Warnings don't trigger alerts by default
        if report.severity == Severity.WARNING.value:
            return

        attempted = 0
        failed: List[str] = []

        for notifier in self._notifiers:
            from flow_doctor.notify.slack import SlackNotifier
            from flow_doctor.notify.email import EmailNotifier
            from flow_doctor.notify.github import GitHubNotifier
            from flow_doctor.notify.s3 import S3Notifier

            if isinstance(notifier, SlackNotifier):
                action_type = ActionType.SLACK_ALERT.value
            elif isinstance(notifier, EmailNotifier):
                action_type = ActionType.EMAIL_ALERT.value
            elif isinstance(notifier, GitHubNotifier):
                action_type = ActionType.GITHUB_ISSUE.value
            elif isinstance(notifier, S3Notifier):
                action_type = ActionType.S3_ALERT.value
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


def init(
    config_path: Optional[str] = None,
    *,
    strict: bool = True,
    **kwargs: Any,
) -> FlowDoctor:
    """Initialize Flow Doctor.

    Args:
        config_path: Path to a flow-doctor.yaml config file. Optional —
            flow-doctor can run with zero config files if all required
            settings are provided via FLOW_DOCTOR_* environment variables
            and/or ``**kwargs``.
        strict: If True (default), any configuration or initialization
            error raises immediately. If False, errors are logged and
            flow-doctor runs in degraded mode with no notifiers. Strict
            mode is the default because silent degradation defeats the
            purpose of an error-monitoring tool.
        **kwargs: Inline config overrides (flow_name, repo, owner, notify,
            store, etc.)

    Returns:
        A configured FlowDoctor instance.

    Raises:
        ConfigError: When config is invalid, a notifier is missing required
            fields, or a ``${VAR}`` reference cannot be resolved. Only
            raised when ``strict=True`` (the default).
    """
    config = load_config(config_path=config_path, **kwargs)
    return FlowDoctor(config, strict=strict)

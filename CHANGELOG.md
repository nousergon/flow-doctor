# Changelog

## 0.5.0 (2026-06-08)

First stable release. Finalizes the `0.5.0rc1` → `rc3` plug-and-play arc
(typed Pydantic v2 config, fluent `FlowDoctor.builder()`,
`FlowDoctorProtocol`, the `flow_doctor.testing` pytest plugin, PEP 561
`py.typed`, fail-loud-on-misconfig, the `flow_doctor.otel` adapter, and
the recommended-default `TelegramNotifier`) after a ~4-week production
soak across morning-signal and the alpha-engine fleet. See the rc
entries below for the full per-rc breakdown.

### Added

- **Symmetric INFO log on successful failure-report dispatch.** Every
  successful notifier dispatch now emits a matching `INFO` log, mirroring
  the existing failure-path log, so dispatch is observable in both
  directions (merged post-rc3 via #24).

### Notes

- No API changes versus `0.5.0rc3` beyond the dispatch log above; the rc
  series is superseded by this tag.
- Suite: 394 passing.

## 0.5.0rc3 (2026-05-13)

Cleanup pass before the morning-signal cutover.

### Added

- **`TelegramNotifier.send_raw(text, *, parse_mode=, disable_notification=)`** —
  adjacent flow-doctor subsystems can now POST arbitrary text through
  the same bot + chat + thread + Markdown routing the structured
  `send()` path uses, without conforming to the `Report` shape.
  Returns the standard non-secret `"telegram:<chat_id>[:<thread>]"`
  target identifier (or `None` on failure — never raises).
  ``parse_mode=None`` / ``disable_notification=False`` are honoured as
  explicit overrides via a sentinel default; pass nothing to inherit
  the instance defaults.
- **`RemediationConfig.telegram_bot_token` + `telegram_chat_id` +
  `telegram_message_thread_id`** — first-class Telegram fields for the
  remediation pipeline. `_init_remediation` builds a real
  `TelegramNotifier` from these (with the `FLOW_DOCTOR_TELEGRAM_*`
  env-var fallback chain) and hands it to `RemediationExecutor`.

### Changed

- **`RemediationExecutor`** now accepts a `telegram_notifier:
  TelegramNotifier | None` kwarg in addition to the legacy
  `telegram_webhook_url`. When both are supplied, the notifier wins.
  Remediation pings going through it pick up Markdown rendering,
  threading, target-id audit (`actions.target` row), and the same
  `validate()` preflight as the rest of the notifier surface.
- `examples/smoke_test.py` rewritten to lead with
  `FlowDoctor.builder()` + `TelegramNotifierConfig` (instead of the
  now-`@deprecated` `flow_doctor.init()`). Adds smoke checks for
  `flow_doctor.context()` propagation, `report_async()` from an
  asyncio context, and `flow_doctor.otel.report_to_otel_span_event`
  serialization. All offline (FLOW_DOCTOR_SKIP_PREFLIGHT=1 + fake
  creds + sqlite at temp path).
- `[tool.coverage.run]` section added to `pyproject.toml`. Use the
  canonical `python -m coverage run -m pytest && python -m coverage
  report` instead of `pytest --cov=` — the latter misreports
  module-level statement coverage under editable installs because
  pytest-cov instruments after the import has already happened.

### Deprecated

- **`RemediationConfig.telegram_webhook_url`** is now soft-deprecated.
  Kept for 0.4.x yaml back-compat through the 0.5.x series; consumers
  should migrate to `telegram_bot_token` + `telegram_chat_id` (with
  optional `telegram_message_thread_id`). Will be removed in 0.6.0.

### Coverage

Suite: 393/393 pass (376 prior + 17 new for remediation-Telegram
migration). Project-wide coverage 84% (canonical measurement; the
pytest-cov number that previously read 67% was a tool quirk, not a
real regression).

## 0.5.0rc2 (2026-05-13)

Adds Telegram as the **recommended default notifier** for new consumers.

### Added

- **`TelegramNotifier` + `TelegramNotifierConfig`.** Sends alerts via the
  Telegram Bot API. Setup is two minutes (message `@BotFather` → `/newbot`
  → save the token → grab the `chat_id` from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`). One bot fans out to
  N flows via `chat_id` and the optional `message_thread_id` (forum
  topics in supergroups), no per-channel webhook required.
  Env-var contract: `FLOW_DOCTOR_TELEGRAM_BOT_TOKEN` +
  `FLOW_DOCTOR_TELEGRAM_CHAT_ID` (with `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` as conventional aliases).
  Numeric env chat_ids auto-coerce to `int`; `@channelusername` style
  stays `str`. Persisted action target is the non-secret
  `telegram:<chat_id>[:<thread>]` identifier — never the bot token.
- **`ActionType.TELEGRAM_ALERT`** in the persisted action enum.
- **Telegram parse / payload knobs** in both the typed config and the
  yaml-driven omnibus form: `parse_mode` (default `"Markdown"`),
  `disable_notification`, `message_thread_id`.
- **Preflight bypass parity.** `TelegramNotifier.validate()` calls
  `/getMe` to fail fast on a revoked bot token, with the same
  `FLOW_DOCTOR_SKIP_PREFLIGHT=1` opt-out the other notifiers use for
  tests / offline boot.

### Rationale

For single-dev and small-team ops, Telegram beats SMTP/SES/Slack on
setup cost (no app password, no verified-identity dance, no workspace
admin), routing (per-chat or per-thread is built in), and mobile UX
(push is automatic). Slack / Email / GitHub / S3 stay as alternates;
the change is to which notifier the README + builder examples lead with.

Suite: 376/376 pass (+20 new Telegram tests).

## 0.5.0rc1 (2026-05-13)

Release-candidate cut of the "plug-and-play" release for internal soak.
`pip install flow-doctor==0.5.0rc1` requires `--pre`, so this build
won't accidentally land on consumers pinning `flow-doctor>=0.4` until
0.5.0 final ships. The content below is the planned 0.5.0 changelog
entry verbatim — 0.5.0 final will republish it once the rcN cycle
clears soak.

Three SOTA-target proposals from the plug-and-play planning doc
(Pydantic v2 config, typed contract + testing plugin, ecosystem polish)
land together. Existing 0.4.0 consumers keep working unchanged —
`flow_doctor.init(config_path=...)` is still supported through the
0.5.0 deprecation window. New consumers should adopt
`FlowDoctor.builder(...)` for typed, IDE-discoverable configuration
with no yaml required.

### Added

- **Pydantic v2 config models.** All 11 config dataclasses
  (`FlowDoctorConfig`, `NotifyChannelConfig`, `RateLimitConfig`,
  `DiagnosisConfig`, `RemediationConfig`, etc.) are now `pydantic.BaseModel`
  via a shared `_ConfigModel` base. Field names + defaults preserved so
  existing test fixtures and 0.4.0 callers keep working unchanged.
  Adds `pydantic>=2.0` to runtime deps.
- **Typed per-channel notifier configs.** `SlackNotifierConfig`,
  `EmailNotifierConfig`, `GitHubNotifierConfig`, `S3NotifierConfig` ship
  as Pydantic models exposed as the discriminated union `NotifierConfig`
  via `Field(discriminator="type")`. `EmailNotifierConfig.recipients`
  accepts a CSV string or a list and normalizes via a `field_validator`.
- **`FlowDoctor.builder()` fluent API.** `FlowDoctor.builder(flow_name)`
  returns a `FlowDoctorBuilder` with chainable `add_notifier / with_repo /
  with_dedup / with_store / with_diagnosis / with_github / with_auto_fix /
  with_remediation / with_handler / with_dependencies` methods plus
  `build_config()` and `build(strict=True)`. Recommended entry point for
  new code — typed, IDE-discoverable, no yaml.

  ```python
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
  ```
- **`FlowDoctorProtocol` public contract.** `@runtime_checkable`
  Protocol declaring `report() / guard() / monitor() / report_async()`.
  Consumers type-hint against the Protocol and swap in test doubles
  (e.g. `RecordingFlowDoctor`) with `mypy --strict` + `isinstance()`
  verification.
- **`flow_doctor.context()` contextvars.** Per-task/-thread contextvars
  for `flow_name` / `stage` / arbitrary extras. Inner scopes shadow
  outer ones; the active snapshot is merged into every report's
  `context` at `_build_context()` time. Deep call-stacks no longer
  thread `context=...` explicitly.

  ```python
  with flow_doctor.context(flow_name="morning-signal", stage="rank"):
      run_rank()  # any fd.report() inside picks up flow_name + stage
  ```
- **`FlowDoctor.report_async()`.** Async coroutine running the existing
  sync pipeline via `asyncio.to_thread()`. `contextvars` inherit across
  the thread boundary automatically.
- **`flow_doctor.testing` pytest plugin.** `RecordingFlowDoctor`
  in-memory test double implementing `FlowDoctorProtocol` +
  `ReportedIncident` dataclass with `.clear() / .last / .of_type(exc_name)`
  ergonomic helpers. Pytest fixture `flow_doctor_recorder` registered
  via `[project.entry-points.pytest11]` — downstreams `pip install
  flow-doctor` and the fixture is auto-discoverable in any test file
  with no imports.
- **`flow_doctor.otel.report_to_otel_span_event(report)`.** Pure-Python
  OTel-compatible serialization. Maps `flow_name → resource.service.name`,
  `context["stage"] → event.name`, exception fields → OTel exception
  attributes, severity → severity_text + severity_number, created_at →
  time_unix_nano, context dict flattened with `"context."` prefix.
  No `opentelemetry-*` dep — the actual OTLP exporter is queued for
  v0.6.0.
- **PEP 561 `py.typed` marker.** Ships in the wheel via
  `[tool.setuptools.package-data]` so mypy / pyright treat flow-doctor's
  annotations as authoritative in `--strict` mode.
- **PEP 702 `@deprecated` markers.** `flow_doctor.init()` carries a
  runtime DeprecationWarning + static `__deprecated__` attribute
  pointing at `FlowDoctor.builder()`. `NotifyChannelConfig` carries
  the static-only marker (`category=None`) because the omnibus form
  is still the internal lingua franca the builder folds typed configs
  into. Adds `typing_extensions>=4.5` (PEP 702 backport for Python
  3.9-3.12; stdlib in 3.13+).

### Deprecated

- `flow_doctor.init(config_path=..., **kwargs)` is deprecated in favor
  of `FlowDoctor.builder(...)`. Will be **removed in 0.6.0**. The yaml
  shim continues to work through the 0.5.0 series.
- `NotifyChannelConfig` is deprecated for direct construction in favor
  of the typed `SlackNotifierConfig` / `EmailNotifierConfig` /
  `GitHubNotifierConfig` / `S3NotifierConfig`. Will be **removed in
  0.6.0**. Static-only deprecation — no runtime warning is emitted
  because the omnibus form is still the internal lingua franca.

### Fixed

- Dedup signatures for non-exception string reports now normalize
  variable identifiers (reqId/orderId/permId/clientId/conId, IB contract
  symbol/localSymbol/tradingClass/exchange/primaryExchange/currency/secType,
  UUIDs, AWS request IDs) before hashing. Previously a library that logged
  the same error against many objects — e.g. ib_insync emitting
  `Error 10197 ... reqId=257 ... symbol='D'`, `reqId=261 ... 'LLY'`,
  `reqId=253 ... 'CASY'` for what is operationally a single "competing
  live session" incident — produced a unique signature per message and
  the cooldown window never engaged. Error codes and other semantic
  numbers are preserved so distinct incidents remain distinct.

### Roadmap (deferred to 0.6.0)

- **OTLP exporter notifier.** Direct ship to an OpenTelemetry
  collector via `opentelemetry-exporter-otlp`. Shape already ships in
  0.5.0 via `flow_doctor.otel.report_to_otel_span_event`.
- **`pydantic-settings` BaseSettings env-var injection.** Pydantic-native
  `FLOW_DOCTOR_*` autoload as a parallel path to the existing per-notifier
  `_env_fallback` chain.
- **Hard removal of `flow_doctor.init()` and `NotifyChannelConfig`.**

## 0.3.0 (2026-04-10)

Two independent changes folded into one release because 0.2.0 was the
most recent PyPI publish and no consumers ever pinned an intermediate
build:

1. `Notifier.send()` return type changes to `Optional[str]` so the
   dispatcher can persist the target identifier in `actions.target`.
2. Conservative auto-fix defaults + new `deny_repos` field so consumers
   are safer by default without having to override everything per-install.

### Breaking changes

- `Notifier.send()` now returns `Optional[str]` instead of `bool`. On
  success, the return value is a target identifier string that flow-doctor
  persists in `actions.target`. On failure, the return value is `None`.
  Callers should check truthiness (`if send(...)`) instead of `== True`.
  Subclasses of `Notifier` outside the flow-doctor package need to update
  their `send()` return type. The semantic is backward-compatible at the
  truthiness level — `None` is falsy like `False` was — but strict type
  assertions will fail.
- `RemediationConfig.max_auto_remediations_per_day` default lowered
  from **5 → 2**. Rationale: the old default was calibrated for
  high-volume CI where fixes are dependency bumps. For application
  code, 2/day leaves room for real fixes without PR fatigue. Consumers
  needing looser settings can override per-install in `flow-doctor.yaml`.
- `RemediationConfig.fix_pr_min_confidence` default raised from
  **0.8 → 0.85**. Cuts the long tail of marginal LLM suggestions
  humans were rejecting anyway.
- Same default changes mirrored on `GateConfig` so direct constructions
  inherit the new safer baseline.

### New features

- **`actions.target` populated** for every delivered notification via
  the `Notifier.send() -> Optional[str]` return contract. Previously
  always `None`, so the DB had no link back to filed GitHub issues.
  Notifier-specific target formats:
  - **GitHubNotifier** — full `html_url` from the issue API response
    (e.g., `https://github.com/owner/repo/issues/42`). Falls back to
    `https://github.com/{repo}/issues` if the response unexpectedly
    lacks `html_url`.
  - **EmailNotifier** — comma-joined recipients string.
  - **SlackNotifier** — channel string (e.g., `"#alerts"`) or the
    literal `"slack"` if no channel is configured. **Does not return
    the webhook URL** — that's a secret and should not be persisted
    to the DB.

- **`deny_repos` field** on both `RemediationConfig` and `GateConfig`.
  Hard deny list. Repos matching any entry will ALWAYS escalate
  instead of auto-remediating or generating fix PRs, even when
  `remediation.enabled=True` and confidence exceeds thresholds. Match
  is case-insensitive substring against `diagnosis.context['repo']`,
  `flow_name`, or `diagnosis.flow_name`.

  **Issue-filing on denied repos still works.** Only the auto-fix
  code path (`auto_remediate` + `generate_fix_pr`) is blocked. Use
  case: production-critical repos where a bad auto-fix could cost
  real money or safety (trading systems, payment processors, medical
  software).

  YAML supports both list and scalar forms:
  ```yaml
  remediation:
    enabled: true
    deny_repos:
      - cipher813/alpha-engine        # trading system
      - cipher813/alpha-engine-data   # data pipeline
  # or for a single repo:
  remediation:
    deny_repos: cipher813/alpha-engine
  ```

### Migration from 0.2.0

- If you subclass `Notifier` externally, update your `send()` return
  type from `bool` to `Optional[str]`. None-is-failure semantics are
  preserved.
- If you were relying on the 5/day auto-remediation cap or 0.8
  fix-PR confidence, add explicit overrides in your `flow-doctor.yaml`:
  ```yaml
  remediation:
    max_auto_remediations_per_day: 5
    fix_pr_min_confidence: 0.8
  ```
- If you have production repos where auto-fix is risky, add them to
  `remediation.deny_repos` in your YAML. The defensive block lives in
  the package now, not just in operational discipline.

### Tests

- **`tests/test_action_target.py`** (new, 7 tests) — notifier target
  contract + dispatcher persistence.
- **`tests/test_conservative_autofix.py`** (new, 14 tests) — default
  value pins, YAML loading (list + scalar + missing + override),
  `deny_repos` enforcement across `auto_remediate` + `fix_pr` paths,
  case-insensitive matching, non-matching pass-through, empty list
  no-op.
- Updated 9 pre-existing tests in `test_notifications.py`,
  `test_github_notifier.py`, `test_coverage_gaps.py`, and
  `test_remediation_pipeline.py` for the new contracts.
- **Full suite: 264 tests passing** (243 existing + 21 new across
  the two merged PRs).

## 0.2.0 (2026-04-10)

Fail-loud contract and canonical `FLOW_DOCTOR_*` env var fallbacks. Breaking
changes to previously-silent failure paths.

### Breaking changes

- `FlowDoctor.__init__` and `flow_doctor.init()` now re-raise initialization
  errors by default instead of catching them, printing a warning, and running
  in degraded mode. Opt-in `strict=False` preserves the old behavior.
- `_init_notifiers` raises `ConfigError` when a notifier in `config.notify`
  is missing required fields (token, webhook, sender, etc.). The old behavior
  was to silently drop misconfigured notifiers, which meant users discovered
  broken notifications only during an incident.
- `_resolve_env_vars` raises `ConfigError` on unresolved `${VAR}` references
  in YAML instead of leaving the literal string (which previously ended up
  being passed to notifiers as a credential). Opt-in `allow_unresolved=True`
  for unit tests.

### New features

- **Canonical `FLOW_DOCTOR_*` env var contract** — documented in README.
  Every notifier credential has a fallback chain: config → `FLOW_DOCTOR_*`
  canonical name → common conventions (`GH_TOKEN`, `GMAIL_APP_PASSWORD`,
  `SLACK_WEBHOOK_URL`, `ANTHROPIC_API_KEY`, etc.). Same code works across
  systemd, Docker, CI, and every major deployment target.
- **Env-var-only quickstart** — `flow_doctor.init()` can now run with zero
  config file if all required settings come from env vars. Set
  `FLOW_DOCTOR_GITHUB_REPO` + `FLOW_DOCTOR_GITHUB_TOKEN`, pass a
  `notify=[{"type": "github"}]` kwarg, and you're done.
- **Notifier send failures log at CRITICAL** via the `flow_doctor` logger
  (in addition to existing stderr prints). Host apps see the failure in
  their log stream — journalctl, Sentry, Datadog, whatever.
- **Aggregate-failure signal** — when *all* notifiers fail for a single
  report, `_send_notifications` emits a distinct CRITICAL log message:
  "error monitoring is itself broken." This is the signal users most need
  to see and previously never did.
- **New `flow_doctor.errors` module** with `FlowDoctorError` base class
  and `ConfigError` subclass. Both exported from the package root.

### Migration from 0.1.0

Most users won't need code changes. If you were relying on silent-skip
behavior (notifier listed in config without credentials, unresolved
`${VAR}` references), you'll now get `ConfigError` at startup — fix the
config. If you truly need the old behavior, pass `strict=False` to
`flow_doctor.init()`.

## 0.1.0 (2026-04-09)

Initial release.

### Features

- **Phase 1 — Error Capture**: Exception and message reporting with deduplication,
  rate limiting, and automatic secret scrubbing (AWS keys, tokens, passwords).
- **Phase 2 — LLM Diagnosis**: Root cause analysis via Claude API with confidence
  scoring, knowledge base caching, and git context assembly.
- **Phase 3 — Auto-Remediation**: Decision gate routing (auto-remediate, generate PR,
  escalate, log-only) with configurable playbooks, market hours lockout, and
  daily/per-failure safety limits.
- **Phase 4 — Auto-Fix PRs**: LLM-generated unified diffs with scope guard validation,
  test runner verification, and GitHub PR creation.
- **Notifications**: GitHub issues (with machine-readable metadata), Slack webhooks,
  and SMTP email.
- **Logging Handler**: `FlowDoctorHandler` attaches to Python's logging system for
  non-blocking, async error capture at WARNING+ levels.
- **Storage**: SQLite backend with thread-safe per-thread connections, full schema
  for reports, diagnoses, actions, feedback, known patterns, and fix attempts.
- **CLI**: `flow-doctor generate-fix --issue-number N` for GitHub Actions integration.

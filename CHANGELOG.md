# Changelog

## Unreleased

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

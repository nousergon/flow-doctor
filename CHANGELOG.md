# Changelog

## 0.16.3 (2026-09-09)

### Fixed

- **`GitHubNotifier` now deduplicates before filing** (alpha-engine-config-I10350). Two measured defects over the 28 open `flow-doctor` GitHub issues on one tracker: (1) one emission filed as two issues — an exception wrapper and the finding it wraps, 229us apart in report-creation time (`I8305`/`I8306`); (2) a recurring fingerprint (`NAV MARK CORRECTION applied`) re-filed as a brand-new issue on three consecutive sessions (`I9779`/`I9872`/`I9947`) because local dedup is a per-process cooldown window, not a lookup against the tracker itself.

  1. `flow_doctor.core.dedup.compute_body_fingerprint()` derives a stable fingerprint from the `## Error` block text (digits collapsed to `#` over the first 160 chars), embedded on every filed issue via a `<!-- flow-doctor-fingerprint: ... -->` marker. `GitHubNotifier.send()` searches the tracker's OPEN issues for that fingerprint before filing; a match gets a recurrence comment and an `Occurrences` bump instead of a new issue. A search failure is logged and treated as "not found" — dedup lookup degrading must never suppress a real alert.
  2. `flow_doctor.core.dedup.is_same_finding()` collapses a wrapper/payload pair filed by the same `GitHubNotifier` instance within a few seconds of each other (substring match on the message text) onto the first issue, with no comment and no occurrence bump — it's the same occurrence reaching `send()` twice, not a recurrence.
  3. Every issue body now carries a visible `**Occurrences:** N (first <date>, latest <date>)` line, so a chronic condition reads as chronic instead of as N one-off issues.

## 0.16.2 (2026-08-28)

### Fixed

- **A notifier preflight can no longer block `FlowDoctor.__init__` past its own budget, even mid-call** (alpha-engine-config-I9102). 0.16.0/0.16.1 (I8298) bounded the preflight loop by checking the shared deadline *before starting* each notifier's `validate()` — that did nothing for a call already in flight. `S3Notifier.validate()`, `_put_object()` and `write_heartbeat()` all constructed a bare `boto3.client("s3")`, whose botocore default has no explicit timeout (`connect_timeout=60s`, `read_timeout=60s`, up to 5 retries) — a single degraded S3 call could burn several minutes with no cap. On `alpha-engine-research-eval-rolling-mean`'s 2026-08-28 off-cycle rehearsal, this ran the handler's own 1.6s of completed work behind a 300s Lambda `States.Timeout`, and the trend across the prior ten invocations (20-37s baseline, climbing to 120.2s, 65.3s, then the timeout) showed it degrading in production.

  Two changes close this at the class level, so third-party notifiers are covered too, not just the ones shipped here:

  1. Every S3 client this module constructs now goes through a shared `_bounded_s3_client()` (`connect_timeout=3`, `read_timeout=5`, `max_attempts=2`) instead of an unbounded default.
  2. `FlowDoctor._run_notifier_preflights` now runs each notifier's `validate()` on its own daemon thread and joins it with a **hard** deadline. A notifier that has not returned by its deadline is marked `UNVALIDATED (timed out mid-call)` and preflight moves on without it — the abandoned thread keeps running, but being daemon, it can never again hold up the process that owns it.

  `tests/test_notifier_preflight_budget.py::test_slow_call_in_flight_does_not_block_past_the_budget` is the regression test — it fails (blocks ~5s against a 0.1s budget) against the pre-fix code and passes with the fix.

## 0.16.1 (2026-08-25)

### Added

- **Every preflight-unreachable path now emits a stable log marker** — `[FLOW_DOCTOR_PREFLIGHT_UNREACHABLE]`, with `[FLOW_DOCTOR_PREFLIGHT_UNVALIDATED]` for the separate budget-exhaustion condition (alpha-engine-config-I8298 deliverable 5). A metric filter is how this signal LEAVES a process by a path the failing process does not control: on 2026-08-24 the only alerting transport `alpha-engine-predictor-inference` had was Telegram, which was the thing that was unreachable, so a log-derived metric is the answer to that circularity.

  The first filter written for it matched the prose `"preflight unreachable"` — `TelegramNotifier`'s wording alone. It would have caught the incident that prompted it and nothing else like it: not the class-level guard in `FlowDoctor._run_notifier_preflights` covering every notifier including third-party ones, and not `GitHubNotifier`, which carried the identical `except URLError` defect fixed in 0.16.0. The markers let a filter match the **condition** instead of a sentence, and leave the prose free to change. `tests/test_preflight_log_markers.py` is the contract that keeps them on every path, including an assertion that they stay safe to quote verbatim in a CloudWatch filter pattern.

  Convention mirrors the `[LEGACY_PRICE_READ]` marker already in use on the alpha-engine Lambda log groups.

## 0.16.0 (2026-08-24)

### Fixed

- **A notifier preflight can no longer crash the process it instruments** (alpha-engine-config-I8298). `TelegramNotifier.validate()` treated every failure to reach `api.telegram.org` as a verdict on the bot token and raised `ConfigError`; worse, its handler was `except URLError`, and `TimeoutError` is *not* a `URLError` subclass, so a read timeout escaped uncaught and propagated out of `FlowDoctor.__init__` under `strict=True`. Because `krepis.setup_logging` runs at module level in Lambda handlers, the import itself failed. On 2026-08-24 roughly 90 seconds of an unresponsive `api.telegram.org` took down `alpha-engine-predictor-inference` through five retries, and the postclose trading pipeline ran with both its MarketHoursGate and its DeployDriftCheck degraded — two safety gates lost to one unreachable alerting host.

  The inverse error came from the same conflation: `HTTPError` *is* a `URLError` subclass, so a genuine 401 was reported as a network problem. Preflight now splits the two. A **verdict** — HTTP 401/403, or a 200 carrying `ok: false` — raises `ConfigError` and still fails loud under `strict`. Every **transport** outcome — connect/read timeout, DNS failure, reset, 5xx, 429, a non-JSON 200 from a captive portal — logs a warning and proceeds with the notifier enabled and its credential unverified. That is the contract `GitHubNotifier` and `S3Notifier` already documented and kept.

  `GitHubNotifier.validate()` carried the identical `except URLError` defect and is fixed the same way.

- **The transport-never-crashes-the-caller rule is now enforced at the class level**, in `FlowDoctor._run_notifier_preflights`, so it holds for third-party notifiers too and not only for the ones shipped here. An `OSError`-family failure from any notifier's `validate()` is reported to stderr and preflight continues to the next notifier; `ConfigError`/`RuntimeError` are untouched and still raise under `strict`. This extends to notifiers the carve-out `StorageBackendError` already had: a telemetry dependency must never kill the producer it only instruments over its own transient failure.

### Added

- **Notifier preflight is bounded in time.** Preflights run on the import path of the instrumented process, and on AWS Lambda that import sits inside a hard 10-second INIT budget — the 2026-08-24 failure's first attempt logged `Init Duration: 9999.42 ms  Phase: init  Status: timeout`. The per-call socket timeout drops from a hardcoded 10s to `DEFAULT_PREFLIGHT_TIMEOUT_S` (3s, override with `FLOW_DOCTOR_PREFLIGHT_TIMEOUT_S`), and all notifier preflights now share a total wall-clock budget of 10s (override with `FLOW_DOCTOR_PREFLIGHT_BUDGET_S`). Notifiers skipped because the budget was spent are **named** in a warning rather than silently treated as validated. Both env vars fall back to the default on an unparseable or non-positive value — an unbounded preflight is the failure mode they exist to prevent.

## 0.15.2 (2026-08-21)

### Added

- **A failed diagnosis attempt is now loud on every notifier, not just printed to stderr** (alpha-engine-config-I7789). Previously, when `diagnosis.enabled=True` and the provider call itself raised — a transport error, an exhausted credit balance, a router resolution failure (`RouterUnresolvable`) — `FlowDoctor._run_diagnosis` printed `[flow-doctor] Diagnosis failed: ...` and returned `None`, indistinguishable from diagnosis never having run at all (disabled, warning severity, cascade, rate-limited, cost-capped). A month of weekly-pipeline failure reports carried no diagnosis and nothing said why.

  `Report` gained a new field, `diagnosis_error: Optional[str]`, set on the exception path with the failure's `type(e).__name__: e` string and also recorded as a `FAILED` `Action` (`action_type="diagnosis"`) in the store, queryable the same way a failed notifier send already is. `diagnosis_error` is deliberately a plain string on `Report`, not folded into a synthetic `Diagnosis` object: a `Diagnosis` flowing through `_send_notifications` is subject to `notify_on_category` gating and feeds `_run_remediation`, and neither may fire off a failed attempt.

  Every notifier (`email`, `github`, `telegram`, `slack`, `s3`, `webpush`) now renders `report.diagnosis_error` in an `elif` alongside its existing `if diagnosis:` block, so a diagnosis failure reaches every configured channel exactly like a successful diagnosis would — never silently narrowed by a category gate that assumes a real diagnosis. `S3Notifier` emits `"diagnosis": {"status": "unavailable", "error": ...}` in the changelog-corpus entry, distinct from the key being absent entirely.

## 0.15.1 (2026-08-20)

### Fixed

- **Raised exception messages are now Latin-1-safe.** `awslambdaric` (AWS's Lambda Python Runtime Interface Client, upstream) encodes its `post_init_error` response as Latin-1; a `ConfigError` message containing an em-dash crashed `post_init_error` itself with `UnicodeEncodeError` while reporting the original error, degrading a fully-diagnosable `ConfigError` (alpha-engine-data-collector v341's rollback, alpha-engine-config-I7855) into an opaque `Runtime.ExitError` with no parseable `errorMessage`/`errorType`. Fixed em-dash/arrow literals in `core/config.py`, `core/client.py`, `core/router.py`, `notify/github.py`, `notify/telegram.py`. New source-level guard: `tests/test_error_messages_latin1_safe.py` (alpha-engine-config-I7859).

## 0.15.0 (2026-08-19)

### Changed (breaking)

- **The `anthropic` distribution is no longer a dependency of flow-doctor, anywhere.** `AnthropicProvider` (the native-SDK diagnosis transport) and `FixGenerator`'s `provider="anthropic"` branch are both deleted entirely — not deprecated. `pyproject.toml` no longer declares `anthropic` in any extra (`diagnosis`, `agent`, `all`, `dev`); the `diagnosis` extra, which contained nothing else, is removed outright. `krepis`'s own `flow_doctor` extra floors `flow-doctor[diagnosis,s3]`, so this had been forcing the Anthropic SDK onto every `krepis[flow_doctor]` consumer transitively — including repos that had deliberately removed direct LLM exposure (`crucible-executor`, 2026-05-25).

  A config that still names `provider: anthropic` now fails loudly: `flow_doctor.core.client._init_diagnosis` and `flow_doctor.fix.cli.generate_fix` both refuse it explicitly, naming the replacement (`provider: router` + `model_group`, or `provider: openai_compat` + `base_url`). `FixGenerator(provider="anthropic")` raises `ValueError` at construction.

- **`diagnosis.provider` has NO default at all** (was `"anthropic"`). This was the actual defect the above closes: every fleet config that simply omitted `provider:` silently took a direct, unscanned connection to one vendor, with nothing in its own configuration saying so — the $0 Anthropic account balance (alpha-engine-config-I7460) is what made this visible, not the root cause. `diagnosis.enabled: true` with `provider` unset now raises `ConfigError` at config-LOAD time (`load_config`) and at first use (`FlowDoctor._init_diagnosis`, for directly-constructed configs), naming the two valid values: `router` and `openai_compat`. There is no vendor to fall back to.

- **`FixGenerator.__init__`'s `provider` parameter is now a required keyword-only argument** (`*, provider: str`) — no default value, mirroring `DiagnosisConfig.provider`.

### Added

- **`flow_doctor.fix.generator.FixGenerator` gained a `router` transport** (`provider="router"`, `model_group`), mirroring `flow_doctor.diagnosis.provider.RouterProvider`: resolves a krepis capability class (`low`/`med`/`high`/`ultra`) instead of holding a direct provider key, fails closed (`RouterUnresolvable`) on any resolution outside the compelled routes (`litellm_proxy`, `egress_proxy`), and records cost via `krepis.cost.record_llm_call`. Closes alpha-engine-config-I7014 — 0.13.0 shipped `provider: router` support on the diagnosis side only and explicitly refused it for auto-fix.
- **`flow_doctor.core.router`** (new module) — the krepis-router edge resolution shared by both `RouterProvider` and `FixGenerator`'s router path (`RouterUnresolvable`, `resolve_router_edge`, `COMPELLED_ROUTES`), lifted out of `diagnosis/provider.py` so the compelled-route decision is made in exactly one place. `RouterUnresolvable` remains importable from `flow_doctor.diagnosis.provider` for back-compat.
- **Guard tests** (`tests/test_endpoint_defaults.py`) asserting the `anthropic` distribution never reappears as a dependency in `pyproject.toml`, no packaged module imports it, and `DiagnosisConfig.provider` / `FixGenerator`'s `provider` parameter carry no default value — the sibling of the existing `test_no_provider_endpoint_literal_in_package` guard.

### Fixed

- **`diagnosis.provider="openai_compat"` with no `api_key` set previously did nothing silently** (`FlowDoctor._init_diagnosis` skipped the branch entirely with no message). It now prints the same fail-closed warning shape as the existing `base_url`-missing case.
- Removed a dead `anthropic_api_key` settings field (`flow_doctor.core.settings.FlowDoctorSettings`) and its `_ENV_FALLBACKS` entry in `core/client.py` — measured: never wired to any `_env_fallback()` call site, pure dead code carrying a vendor name forward.

## 0.13.0 (2026-08-12)

### Changed (breaking)

- **`diagnosis.base_url` no longer defaults to a provider endpoint.** It carried the literal `https://openrouter.ai/api/v1` in three places — `DiagnosisConfig.base_url`, the YAML parser's `diag_raw.get("base_url", ...)` fallback, and `FixGenerator.__init__` — so a deployment that set `provider: openai_compat` without naming an endpoint sent its context to OpenRouter silently, with nothing in its own configuration saying so.

  That context is whatever the failure carried: tracebacks, log tails, source excerpts, and for auto-fix the full contents of the affected files and their tests. flow-doctor makes this call from inside its consumers' error path, unattended, at the moment least likely to be watched. A default destination is the wrong shape here even though it is convenient — a library that runs in your error path should not choose your inference vendor.

  0.12.0 added `provider: router` as an opt-in alternative but deliberately left `anthropic` and `openai_compat` "working exactly as before", so the defaults survived it. **This release removes them.**

  **The default is now `None` and an unset endpoint fails closed.** Diagnosis is disabled with the reason on stderr (the capture path must not raise into a consumer already handling a failure); `FixGenerator._complete_openai_compat` raises rather than letting the `openai` SDK fall back to its own default host; and the auto-fix CLI refuses before calling.

  **If you relied on the old default**, set it explicitly to restore the previous behaviour:

  ```yaml
  diagnosis:
    provider: openai_compat
    base_url: https://openrouter.ai/api/v1
  ```

  `provider: anthropic` (the default) and `provider: router` are unaffected — neither ever read `base_url`.

### Fixed

- **Auto-fix no longer sends an Anthropic key to a route nobody selected under `provider: router`.** `fix/cli.py` mapped provider→credential with only `anthropic` and `openai_compat` in mind, so 0.12.0's `router` provider fell through to the `else` branch and used `ANTHROPIC_API_KEY`. A deployment that deliberately holds no direct provider credential must not have one substituted for it. It now refuses with an actionable message; full router support for the fix path is tracked separately.

### Added

- **A source-level guard** (`tests/test_endpoint_defaults.py::test_no_provider_endpoint_literal_in_package`) failing CI if any packaged module contains a provider endpoint URL in a reachable expression. The original defect spanned three modules with a fully green behavioural suite, because nothing asserted on what the default *was*.

## 0.12.0 (2026-08-12)

### Added

- **`diagnosis.provider: router`** — resolves a krepis router capability class (`low`/`med`/`high`/`ultra`, `diagnosis.model_group`) instead of holding a direct Anthropic/OpenRouter key. For self-hosted installs nothing changes — `anthropic` and `openai_compat` keep working exactly as before with your own key. `router` is for deployments that are themselves a krepis consumer and must never hold a direct provider credential: `RouterProvider` resolves through `krepis.router.resolve_group_spec` and refuses to call anything outside the compelled routes (`litellm_proxy`, the authenticated edge; `egress_proxy`, its registry-derived DLP-scanned degraded route) — any other resolution, including a direct-provider fallback the router itself might otherwise choose, raises `RouterUnresolvable` rather than silently placing the call. New optional extra: `pip install flow-doctor[router]`.

## 0.8.8 (2026-07-28)

### Fixed

- **`telegram_alert` and `s3_alert` had no rate-limit budget and silently inherited a hardcoded 10/day, causing a fleet-wide alert blackout.** `RateLimiter.__init__` mapped budgets for `diagnosis`, `github_issue`, `github_pr`, `slack_alert` and `email_alert` only; `check()` then did `self.limits.get(action, 10)`. Both notifiers shipped without being added, so a consumer configuring `max_alerts_per_day: 100` had that value reach only Slack and email — channels it did not use — while Telegram, the only channel anyone read, took the hardcoded 10/day, counted via `count_actions_today` against a store SHARED by every consumer. The budget burned out early each day, so terminal notifications (which happen late in a cycle) were systematically dropped while start-of-run pings got through. Measured in production 2026-07-28: **12 of 13 terminal Step-Functions notifications suppressed across two days, all with distinct signatures — including two consecutive trading-pipeline failures that paged and were never seen** (nousergon/alpha-engine-config#5289). Budgets are now derived from the `ActionType` enum, so a newly-added notifier cannot be silently unbudgeted, and the constructor warns if any `ActionType` lacks one.

- **An unmapped action no longer falls back to a silent small default.** `check()` previously invented a `10`/day budget for any action not in the map — the generative defect above. It now **fails open** (allows) and logs a warning naming the gap. For an alerting library this is the correct failure direction: an extra alert costs noise, a dropped one costs an outage.

### Added

- **`RateLimitConfig.rate_limit_exempt_severities`** (default `["critical", "error"]`) — severities exempt from the daily alert cap, threaded from `Report.severity` through `FlowDoctor._send_notifications` into `RateLimiter.check(action, severity=...)`. A rate limiter that can drop a failure page is an outage amplifier rather than a limiter: repeats of the *same* failure are already suppressed by signature dedup and `dedup_cooldown_minutes`, so anything reaching the daily cap at error/critical is a *distinct* failure — precisely what must not be silenced. Set to `[]` to restore the pre-0.8.8 cap-everything behaviour.

  `RateLimiter.check()` gains an optional `severity` keyword. Existing positional calls (`check(action)`) are unaffected; a test double stubbing `check` must accept the new keyword.


## 0.8.6 (2026-07-21)

### Fixed

- **Telegram and Slack notifiers silently dropped `notify_event(body=...)`.** `body` is stored as `Report.logs`; the email and GitHub notifiers already rendered it, but `telegram.py`/`slack.py`'s `_format_message` only ever rendered the subject line (`error_message`). Any caller relying on `body` for detail lost that detail in the delivered message — the concrete case: alpha-engine's trade alerts arrived as a bare `REDUCE COIN` with no shares/price/trigger, even though `executor/notifier.py` already built a richer body. Both formatters now render `report.logs` (last 20 lines, code block) alongside the existing traceback rendering.

## 0.8.5 (2026-07-13)

### Fixed

- **A storage backend's own runtime failure could crash the calling producer, even under the documented `strict=True` intent.** `FlowDoctor.__init__` previously re-raised ANY exception from `_init_store()` under `strict` — correct for genuine misconfiguration (missing table_name, bad store type), but wrong for an infra/runtime failure in the backend itself (an IAM permission gap, DynamoDB throttling, a network blip). A production incident (nousergon/alpha-engine-config#2465, 2026-07-13): an IAM role missing DynamoDB access crashed a data-collection workload before it did any work, because `strict=True` re-raised the backend's `AccessDeniedException` out of `FlowDoctor.from_config()`. New `StorageBackendError` (a `FlowDoctorError` subclass) is now raised by `_init_store()` specifically for failures from calling the backend (`init_schema()`), distinct from `ConfigError` for actual misconfiguration. `FlowDoctor.__init__` always degrades on `StorageBackendError` — logging loudly to stderr — regardless of `strict`. `strict` continues to govern true misconfiguration exactly as before; it was never meant to let a monitoring dependency's own transient failure crash the process it instruments.

## 0.8.3 (2026-07-06)

### Added

- **`FlowDoctor.last_dispatch_reason()` / `last_dispatch_outcome()` accessor:
  `last_dispatched()`.** `report()` / `notify_event()` / `notify_success()`
  return a non-``None`` report id on every outcome except `deduped` —
  including `severity_filtered`, `category_filtered`, `rate_limited`, and
  `delivery_failed`, where the event was evaluated but reached zero
  notifiers. A caller that logged success on "got a report id back" was
  therefore wrong for those cases (config#1813: a stale 2-notifier
  executor config silently dropped `severity=info` trade alerts while
  the caller logged "Telegram alert sent"). `last_dispatch_reason()`
  returns the `DecisionReason` string for the most recent call on this
  instance (`fired`, `deduped`, `severity_filtered`, `category_filtered`,
  `rate_limited`, `delivery_failed`, `no_notifiers`); `last_dispatched()`
  is the `== "fired"` convenience check. Purely additive — no change to
  any existing method signature or return value.
- **`DecisionReason` now exported from the package root** (`from
  flow_doctor import DecisionReason`) so callers can compare against the
  named values instead of hardcoding strings.

## 0.8.2 (2026-07-06)

### Fixed

- **Dedup signature normalization for ISO-8601 event timestamps.** Log-captured
  errors whose message embeds a per-tick ``Event timestamp 2026-07-06T…`` (e.g.
  ``assert_within_session`` refusals) now collapse to one signature within the
  cooldown window instead of firing a distinct alert every minute. Bare calendar
  dates (session labels without a ``T`` time component) are preserved so
  genuinely different sessions still dedup separately.

## 0.8.1 (2026-07-04)

### Added

- **`notify_event()` / `notify_event_async()`** — intentional non-error
  notifications (trade alerts, SF milestones) with optional `dedup_key`
  for cross-call deduplication. Fleet producers route through flow-doctor
  without misusing `report()` on non-exception traffic (config#1739).
- **DynamoDB storage backend** (`store: dynamodb://{table}` or
  `{type: dynamodb, table_name: ...}`) — shared dedup/rate-limit state
  across Lambda invocations. `init_schema()` creates the table in dev/moto;
  production uses IaC.
- **Telegram notifier → krepis transport** — when `krepis` is installed,
  `TelegramNotifier` delegates to `krepis.telegram.send_message` with
  explicit `bot_token` / `chat_id` / `message_thread_id` overrides;
  urllib fallback remains for self-host installs without krepis.

## 0.8.0 (unreleased)

### Added

- **Per-notifier diagnosis-category routing** (`notify_on_category`, on the
  omnibus `NotifyChannelConfig` and every typed `*NotifierConfig`). Mirrors
  the existing `notify_on` severity gate but filters on the Phase-2
  diagnosis category (`TRANSIENT`/`DATA`/`CODE`/`CONFIG`/`EXTERNAL`/
  `INFRA`) instead of severity. Lets a curated channel (e.g. GitHub issues
  feeding a real backlog) opt in to only human-actionable categories while
  a cheap channel (Telegram/SNS) still fans out on everything else. A
  report with no diagnosis (feature disabled, or the diagnosis call
  itself failed) always passes the gate — an unavailable enrichment must
  never silently blank a channel. New `DecisionReason.CATEGORY_FILTERED`
  distinguishes this from `SEVERITY_FILTERED` in `status()`/`log_summary()`.
- **`auto_fix_pr` cross-repo misconfiguration warning.** Since the
  `flow-doctor-fix` Actions workflow can only trigger on an `issues:
  labeled` event in the repo it lives in, pointing a github notifier's
  `repo` at a different repo than the app's own (`FlowDoctorConfig.repo` /
  `.with_repo(...)`) while `auto_fix_pr=True` silently drops auto-fix. Flow
  Doctor now logs a `WARNING` at init naming both repos, so redirecting
  issues to a centralized backlog doesn't quietly break auto-fix.

## 0.6.2 (unreleased)

### Added

- **End-of-run heartbeat → S3 emitter** (`FlowDoctor.emit_heartbeat()` +
  `flow_doctor.notify.s3.write_heartbeat()`). Writes the `status()`
  snapshot (the seen/fired/suppressed decision breakdown + cost) to
  `s3://{bucket}/_flow_doctor/heartbeat/{flow}/{YYYY-MM-DD}.json` so a
  dashboard System Health panel can tell "alive but quiet" from
  "suppressing X per flow" without scraping CloudWatch/journalctl.
  Companion to `log_summary()` (same `status()` data, durable sink).
  Soft-fails to `None` on any error — a liveness write never raises into
  the pipeline it reports on. Wiring (which call sites invoke it) and the
  dashboard consumer are deliberately left to the caller. (config#646)

## 0.6.0 (2026-06-26)

Cut the **0.6.0 final** release — drop the release-candidate suffix. No
source changes versus `0.6.0rc6`: this promotes the soaked rc line to a
stable tag now that the fleet has run ≥1 clean Saturday+weekday cycle on
`0.6.0rc3`+ (via alpha-engine-lib), satisfying the soak gate. Consumers can
re-pin off `==0.6.0rcN` onto a stable range (`>=0.6.0,<0.7`).

The 0.6.x line delivered (across rc1–rc6): the `init()` → `FlowDoctor.from_config()`
API migration; default-on activation via the lib; the `DecisionReason`
decision trace + heartbeat (`status()`/`log_summary()`); telegram-on-fix-PR
in the fix CLI; three-state auto-fix outcomes; and the world-event-aware
diagnosis prompt.

## 0.6.0rc6 (2026-06-12)

Diagnosis prompt: weigh world-event causes; recent commits are not the presumed culprit.

### Fixed

- **The LLM diagnosis no longer presumes the monitored code is broken.**
  The system prompt framed every report as "a scheduled job has failed",
  pushed recent git changes into context with no weighing instruction, and
  defined `EXTERNAL` only as "third-party API/service down". On a
  working-as-designed data-quality flag — KLA Corp's 10-for-1 stock split
  restating adjusted history by exactly ÷10 — the diagnosis confidently
  blamed a recent commit for a "decimal-place shift error"
  (alpha-engine-data#417–419). The prompt now (a) states the report may be
  an ERROR log record from a completed run flagging an anomaly it was
  designed to flag, (b) names the world-event cause class (provider
  restatements, corporate actions / stock splits with their exact-integer
  ratio signature, delistings, market holidays), (c) instructs that RECENT
  GIT CHANGES are context for the CODE hypothesis only — temporal proximity
  is weak evidence, and (d) requires hypothesis diversity: a CODE verdict
  must carry at least one non-CODE alternative and vice versa. `EXTERNAL`
  now explicitly covers upstream/world events the flow correctly surfaces.


## 0.6.0rc5 (2026-06-11)

Auto-fix outcome is now three-state — a deliberate skip is a notification, not an error.

### Fixed

- **A working-as-intended "no auto-fix" no longer looks like a failure.**
  `flow_doctor.fix.cli generate-fix` previously returned a boolean
  success/failure and exited `1` for *every* non-PR outcome — including the
  cases where flow-doctor correctly decided there was nothing to patch
  (an `EXTERNAL` provider outage, an `INFRA` issue, a credentials `CONFIG`
  issue, below-threshold confidence, the LLM returning `NO_FIX`, a scope-guard
  block, or a fix reverted because it broke tests). That `exit 1` painted the
  CI run red and triggered the workflow's `failure()` step, which posted a
  scary "⚠️ Flow Doctor fix generation failed" comment on top of the accurate
  "not auto-fixable" one. Surfaced by alpha-engine-data#397 (an `EXTERNAL`
  provider-outage diagnosis reported as a fix-generation failure).
- The CLI now returns a three-state `FixOutcome` — `CREATED` / `SKIPPED` /
  `FAILED`. Only `FAILED` (a genuine fixer malfunction: can't reach GitHub,
  can't read the checkout, no API key, diff won't apply, push/PR failed) exits
  non-zero. `SKIPPED` exits `0`, keeps the job green, and posts an
  informational "ℹ️ no auto-fix generated — this is expected, not an error"
  notification instead of a failure comment. Because the skip path now exits
  `0`, the consuming workflow's `failure()` comment no longer fires on a
  correct no-op.

## 0.6.0rc4 (2026-06-10)

Fix-CLI config robustness.

### Fixed

- **The fix CLI no longer aborts on unset env vars in config blocks it never
  uses.** `flow_doctor.fix.cli generate-fix` now loads config with
  `allow_unresolved=True`, so a `${VAR}` in the `notify` block (e.g.
  `${EMAIL_SENDER}` on a CI runtime that has no email creds) no longer raises
  `ConfigError` before any fix work — the fix path only consumes `auto_fix` /
  `diagnosis` and the `--token` arg. Required values are still validated: an
  unresolved `${ANTHROPIC_API_KEY}` literal in `diagnosis.api_key` is treated
  as not-configured and falls back to the environment (clean "No Anthropic API
  key configured" rather than feeding the literal to the client). Surfaced by
  every alpha-engine-data "Flow Doctor Fix" run failing with
  `Unresolved environment variable(s): EMAIL_SENDER`.

### Added

- **`load_config(allow_unresolved=...)`** — opt-in lenient env-var resolution
  for callers that consume only a subset of the config. Default stays strict
  (fail-loud) for the full-runtime path.

## 0.6.0rc3 (2026-06-09)

Observability + fix-PR visibility. Answers "is flow-doctor quiet because nothing
failed, or because it suppressed something?"

### Added

- **Decision trace** — every evaluated error records exactly one
  `DecisionReason` (`fired` / `deduped` / `rate_limited` / `severity_filtered`
  / `delivery_failed` / `no_notifiers`) in a new `decisions` table, logged at
  INFO. The previously-invisible severity-skip branch now leaves a trace.
- **Heartbeat** — `status()` gains `decisions_today` + `errors_seen_today`;
  `log_summary()` leads with `seen=N fired=M suppressed=K(reason=...)` so a
  quiet flow is legible as alive-but-quiet rather than indistinguishable from
  never-ran.
- **`flow_doctor.decision_reason`** optional attribute on
  `report_to_otel_span_event(report, decision_reason=...)`.
- **Telegram ping on auto-fix PR** — the fix CLI now pings Telegram when it
  opens a fix PR (honours a configured `telegram` notifier, falls back to
  `FLOW_DOCTOR_TELEGRAM_*`), so the PR is as visible as the original issue
  alert. Best-effort: never raises (the PR already exists).

## 0.6.0rc2 (2026-06-08)

Continues the 0.6.0 soak. Typed settings contract.

### Added

- **`pydantic-settings` `FLOW_DOCTOR_*` contract** — `flow_doctor.core
  .settings.FlowDoctorSettings` is now the declared, typed source of truth
  for credential resolution. Each field's `AliasChoices` encodes the
  documented fallback chain (canonical `FLOW_DOCTOR_*` name first, then legacy
  aliases like `GMAIL_APP_PASSWORD` / `GH_TOKEN`), so precedence is declared on
  the field rather than buried in a lookup loop.
- **`.env` file + secrets-directory resolution** — credentials now also
  resolve from a `.env` file (`FLOW_DOCTOR_ENV_FILE`, default `.env`) and a
  secrets directory (`FLOW_DOCTOR_SECRETS_DIR` — Docker / Kubernetes
  file-mounted secrets), on top of process env. Turnkey for self-hosted /
  compose deployments. Precedence: process env → `.env` → secrets dir.

### Changed

- `_env_fallback` resolves through `FlowDoctorSettings` instead of a raw
  `os.environ` loop. The named-field `ConfigError` messages are unchanged —
  this layer is resolution, not validation-gating. New core deps:
  `pydantic-settings>=2.0`, `python-dotenv>=1.0` (both light).

### Notes

- Suite: 414 passing (403 + 11 settings tests).
- Still deferred: the OTLP exporter notifier (build-on-demand — see roadmap).

## 0.6.0rc1 (2026-06-08)

Soak release for the 0.6.0 line. Notification-routing + auto-remediation
control, plus the deprecated-API removal.

### Added

- **Healthy-completion API — `FlowDoctor.notify_success(subject, body=None,
  *, context=None)`** (+ async `notify_success_async`). Sends a success
  ping at the new **`Severity.INFO`** level. Persisted like any report but
  never triggers dedup / diagnosis / remediation, and routed only to
  notifiers that opt into `info`. Closes the gap that forced consumers to
  reach into `fd._notifiers` to signal "pipeline finished OK".
- **Per-notifier severity routing — `notify_on`** on every notifier config
  (`TelegramNotifierConfig(notify_on=["critical", "error", "info"])`, etc.).
  When unset, the default is `{critical, error}` (warnings + info skipped),
  preserving prior behaviour. Replaces the hardcoded blanket "skip warnings"
  in the dispatcher, so you can route failures to one channel and success
  pings or ad-hoc warnings to another.
- **Auto-issue toggle — `GitHubNotifierConfig(auto_create_issue=...)`**
  (default `True`). When `False`, the github notifier files no issue and is
  skipped at init (the config block can stay in place).
- **Auto-fix-PR toggle — `GitHubNotifierConfig(auto_fix_pr=...)`** (default
  `False`) + `fix_label` (default `flow-doctor:fix`). When `True`, the
  notifier applies the fix label to each filed issue, firing the existing
  flow-doctor-fix GitHub Actions pipeline (LLM diff → scope guard → test
  gate → PR) with no human label step. Labeling is best-effort — a failure
  never flips the issue-creation success.
- **`FlowDoctor.from_config(config_path=None, *, strict=True, **kwargs)`** —
  the supported yaml entry point (same contract as the removed `init()`).

### Removed (breaking)

- **`flow_doctor.init()`** — removed. Migrate to `FlowDoctor.from_config(...)`
  (identical signature) or the typed `FlowDoctor.builder()`.
- **`@deprecated` marker on the internal `NotifyChannelConfig`** — the
  omnibus model is now purely an internal representation; construct typed
  `*NotifierConfig` objects from `flow_doctor.notify` instead.

### Notes

- Deferred to a later 0.6.0 rc: the OTLP exporter notifier and the
  `pydantic-settings` `FLOW_DOCTOR_*` autoload (both additive).
- Suite: 403 passing.

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

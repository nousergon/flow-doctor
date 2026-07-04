# Flow Doctor

[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-429_passing-brightgreen.svg)]()
[![PyPI](https://img.shields.io/badge/PyPI-v0.6.0rc3-blue.svg)](https://pypi.org/project/flow-doctor/)
[![Typed](https://img.shields.io/badge/typed-PEP_561-blue.svg)]()

Pipeline error handler for Python. Captures exceptions, deduplicates failure signatures, optionally diagnoses root causes with LLMs, routes alerts (Telegram / Slack / email / GitHub / S3 / custom), and can generate fix PRs.

**Typed, IDE-discoverable configuration.** Pydantic v2 models + a fluent `FlowDoctor.builder()` mean you don't need a yaml file — and when you have one, the schema is enforced at load time.

**Fail-loud by default.** Configuration errors — missing tokens, unresolved `${VAR}` references, misconfigured notifiers — raise `ConfigError` at construction time instead of silently degrading. A silently-degraded error monitor defeats the purpose.

```python
from flow_doctor import FlowDoctor, TelegramNotifierConfig

fd = (
    FlowDoctor.builder("morning-signal")
    .add_notifier(TelegramNotifierConfig())  # creds from FLOW_DOCTOR_TELEGRAM_*
    .with_dedup(cooldown_minutes=60)
    .build()
)

with fd.guard():
    run_pipeline()  # exceptions captured, deduplicated, routed, re-raised
```

## How It Works

```
Exception → Capture → Dedup → Diagnose (LLM, opt) → Notify (Telegram/...) → Fix PR (opt)
```

1. **Capture** — exception, traceback, logs, contextvars (`flow_name`, `stage`)
2. **Dedup** — same error signature within cooldown window is suppressed (normalized to ignore reqIds, UUIDs, contract symbols, etc.)
3. **Cascade** — if a declared upstream dependency also failed, tag it and skip diagnosis
4. **Diagnose** *(opt)* — check the knowledge base (free), then call Claude if rate limit allows
5. **Notify** — Telegram / Slack / email / GitHub issue / S3 changelog (rate-limited with daily digest fallback)
6. **Fix** *(opt)* — human adds `flow-doctor:fix` label on a filed issue, triggering automated fix PR generation

## Installation

```bash
pip install flow-doctor                          # core only
pip install "flow-doctor[diagnosis]"             # + LLM diagnosis (anthropic SDK)
pip install "flow-doctor[diagnosis,remediation]" # + auto-remediation (boto3)
pip install "flow-doctor[all]"                   # everything
```

Python 3.9+. Core install is dependency-light (pyyaml, pydantic v2 + pydantic-settings, python-dotenv, typing_extensions); each extra pulls only what that capability needs.

> **0.6.0 is in its rc cycle.** `pip install flow-doctor` resolves to the latest stable (`0.5.0`); add `--pre` (e.g. `pip install --pre flow-doctor`) to pull `0.6.0rc1`, or pin `flow-doctor==0.6.0rc1`. **0.6.0 removes the deprecated `flow_doctor.init()`** — see [Migrating](#migrating).

## Quick Start — `FlowDoctor.builder()` (recommended)

The builder is typed, IDE-discoverable, and works without a yaml file. Notifier credentials fall through to `FLOW_DOCTOR_*` env vars when not passed inline.

```python
from flow_doctor import FlowDoctor, TelegramNotifierConfig

fd = (
    FlowDoctor.builder("morning-signal")
    .add_notifier(TelegramNotifierConfig())  # bot_token + chat_id from env
    .with_dedup(cooldown_minutes=60)
    .build()
)
```

Three idiomatic ways to use the resulting `FlowDoctor` in your pipeline:

```python
# 1. Context manager — exception is captured + re-raised
with fd.guard():
    run_pipeline()

# 2. Decorator
@fd.monitor
def lambda_handler(event, context):
    run_pipeline()

# 3. Direct reporting — never crashes the caller
try:
    run_pipeline()
except Exception as e:
    fd.report(e, context={"date": "2026-05-13"})
```

Async pipelines:

```python
async def run():
    try:
        await pipeline()
    except Exception as exc:
        await fd.report_async(exc)
```

**Contextvars propagate automatically.** Stamp `flow_name` / `stage` once and any `fd.report()` inside picks them up — no need to thread `context=...` through every layer:

```python
import flow_doctor

with flow_doctor.context(flow_name="morning-signal", stage="rank"):
    run_rank()  # any fd.report() inside auto-records flow_name + stage
```

## Healthy completion + severity routing

**`fd.notify_success(...)`** sends a success ping at `Severity.INFO` — for the "pipeline finished OK" signal, not just failures. It's persisted like any report but never triggers diagnosis or remediation, and reaches **only** notifiers that opt into `info` via **`notify_on`**:

```python
fd = (
    FlowDoctor.builder("morning-signal")
    # failures + success pings both go to this channel
    .add_notifier(TelegramNotifierConfig(notify_on=["critical", "error", "info"]))
    .build()
)

with fd.guard():
    run_pipeline()
fd.notify_success("morning-signal done", body="42 stories, 3 searches")
```

`notify_on` is per-notifier. When unset it defaults to `{critical, error}` (warnings and info are skipped), so existing failure-only channels are unaffected. Use it to fan failures to one channel and success/warnings to another. An async `notify_success_async(...)` mirrors `report_async`.

## Category routing — separating capture from alert

Every notifier also has a diagnosis-category filter, **`notify_on_category`**, applied after severity. It requires [LLM diagnosis](#llm-diagnosis) to be enabled — when a report has no diagnosis (feature off, or the diagnosis call itself failed), the gate is skipped and the notifier fires as if unset, so an unavailable enrichment never silently blanks a channel.

The reason this exists: **capturing every error and alerting a human are different jobs**, and conflating them makes your loudest channel (a GitHub issue tracker feeding a real backlog) as noisy as your cheapest one (a log line). A common split:

```python
fd = (
    FlowDoctor.builder("my-service")
    # Capture everything, unfiltered — this is your data-mining/observability
    # record, not something a human triages one at a time.
    .add_notifier(S3NotifierConfig(bucket="my-event-lake", subsystem="my-service"))
    # Page immediately on operational noise that usually just needs a retry
    # or an upstream fix, not a code change — no permanent record needed.
    .add_notifier(TelegramNotifierConfig(
        notify_on_category=["TRANSIENT", "EXTERNAL", "INFRA"],
    ))
    # Only file a GitHub issue — into your real backlog, which can be a
    # different repo entirely — for genuinely fixable defects.
    .add_notifier(GitHubNotifierConfig(
        repo="myorg/central-backlog",
        notify_on_category=["CODE", "CONFIG"],
    ))
    .build()
)
```

Because `repo` is just a string, the GitHub notifier's issue destination is independent of the repo raising the error — file into a centralized backlog repo, a per-team tracker, or anywhere else that fits how your organization triages work. The one constraint: **`auto_fix_pr` requires the issue to land in the same repo as the code**, because the `flow-doctor-fix` GitHub Actions workflow only triggers on an `issues: labeled` event in the repo it lives in (see [Auto-Fix PRs](#auto-fix-prs)). Flow Doctor warns at init if it detects `auto_fix_pr=True` pointed at a different repo than the app's own (`with_repo(...)` / `FlowDoctorConfig.repo`).

`notify_on_category` values are case-insensitive and match the [six diagnosis categories](#llm-diagnosis). Like `notify_on`, it's per-notifier and fully optional — omit it anywhere you want the pre-0.8.0 "every category reaches this notifier" behavior.

## Notifier configs (typed)

Five first-class notifiers ship today, each with its own Pydantic config exposed via the discriminated union `NotifierConfig`:

| Config | Channel | Setup |
|---|---|---|
| `TelegramNotifierConfig` | Telegram Bot API | `@BotFather` → `/newbot` → bot token + `chat_id`. **Recommended default.** |
| `SlackNotifierConfig` | Slack incoming webhook | Slack app → incoming webhook URL |
| `EmailNotifierConfig` | SMTP (Gmail / any) | sender + recipients + SMTP password (Gmail App Password works) |
| `GitHubNotifierConfig` | GitHub issues | PAT with `Issues: write` on the target repo |
| `S3NotifierConfig` | System-wide changelog corpus | Bucket + subsystem; IAM allows `s3:PutObject` on the prefix |

Mix freely:

```python
from flow_doctor import (
    EmailNotifierConfig,
    FlowDoctor,
    GitHubNotifierConfig,
    TelegramNotifierConfig,
)

fd = (
    FlowDoctor.builder("alpha-engine-predictor")
    .add_notifier(TelegramNotifierConfig(message_thread_id=42))   # forum topic
    .add_notifier(GitHubNotifierConfig(repo="me/alpha-engine"))   # token from env
    .add_notifier(EmailNotifierConfig(sender="me@x.com",
                                       recipients=["me@x.com"]))
    .build()
)
```

### Telegram — recommended default

Why Telegram leads the examples:
- **Two-minute setup.** Message `@BotFather` → `/newbot` → save the token. Then DM your bot, then `GET https://api.telegram.org/bot<TOKEN>/getUpdates` and grab `result[0].message.chat.id`.
- **Per-flow routing for free.** One bot, N channels via `chat_id`, or N forum-topic threads via `message_thread_id` in a supergroup.
- **Mobile push is automatic.** No "did the email go to spam" mystery.
- **Token rotation is one `@BotFather` call.** No app password / SES verified identity / Slack workspace admin.

```bash
export FLOW_DOCTOR_TELEGRAM_BOT_TOKEN=1234567890:ABC...
export FLOW_DOCTOR_TELEGRAM_CHAT_ID=-1001234567890  # negative for supergroups/channels
```

```python
FlowDoctor.builder("pipeline").add_notifier(TelegramNotifierConfig()).build()
```

## Testing — `flow_doctor.testing` pytest plugin

The plugin is auto-discovered (registered via `[project.entry-points.pytest11]`). Downstream tests get a `flow_doctor_recorder` fixture with **no imports required**.

```python
def test_pipeline_reports_db_errors(flow_doctor_recorder):
    run_pipeline_that_should_fail(flow_doctor_recorder)
    assert len(flow_doctor_recorder.reports) == 1
    assert flow_doctor_recorder.last.exc_type == "DBError"
    assert flow_doctor_recorder.last.ambient_context["stage"] == "ingest"
```

`flow_doctor_recorder` is a `RecordingFlowDoctor` — it implements `FlowDoctorProtocol`, so wherever production code expects a `FlowDoctorProtocol` you can swap it in directly. It also snapshots any active `flow_doctor.context()` scope onto each captured incident's `ambient_context` field.

Helpers on the recorder: `.clear()`, `.last`, `.of_type(exc_name)`, plus full async support via `await recorder.report_async(...)`.

## Type-checked contract — `FlowDoctorProtocol`

```python
from flow_doctor import FlowDoctorProtocol

def make_pipeline(fd: FlowDoctorProtocol):
    with fd.guard():
        ...
```

`@runtime_checkable`, so `isinstance(fd, FlowDoctorProtocol)` works at runtime as well as at type-check time. Combined with the shipped `py.typed` marker, `mypy --strict` and pyright treat flow-doctor's annotations as authoritative.

## OpenTelemetry — `flow_doctor.otel`

Pure-Python adapter that serializes a `Report` into an OTel `SpanEvent`-shaped dict — ready to ship to a collector via your existing OTLP exporter today. No `opentelemetry-*` dependency in this release; the bundled OTLP exporter notifier is on the 0.6.0 roadmap.

```python
from flow_doctor.otel import report_to_otel_span_event

span_event = report_to_otel_span_event(report)
# {
#   "resource": {"service.name": "<flow_name>"},
#   "name": "<stage>",
#   "time_unix_nano": ...,
#   "severity_text": "ERROR", "severity_number": 17,
#   "attributes": {
#     "exception.type": "ValueError",
#     "exception.message": "...",
#     "exception.stacktrace": "...",
#     "flow_doctor.error_signature": "...",
#     "context.run_id": "...",
#   },
# }
```

## Configuration

### Inline kwargs / builder (recommended)

See the Quick Start. No yaml required.

### YAML file (legacy / multi-environment)

```yaml
flow_name: my-pipeline
repo: owner/repo

notify:
  - type: telegram
    bot_token: ${FLOW_DOCTOR_TELEGRAM_BOT_TOKEN}
    chat_id: -1001234567890
    message_thread_id: 42      # optional: forum-topic routing
  - type: github
    repo: owner/repo
  - type: s3
    bucket: my-changelog-bucket
    subsystem: predictor       # one of the documented vocab values

store:
  type: sqlite
  path: flow_doctor.db

diagnosis:
  enabled: true
  model: claude-sonnet-4-6-20250514
  api_key: ${ANTHROPIC_API_KEY}
  timeout_seconds: 30
  max_daily_cost_usd: 1.00

github:
  token: ${GITHUB_TOKEN}
  labels: [flow-doctor]

rate_limits:
  max_diagnosed_per_day: 3
  max_issues_per_day: 3
  dedup_cooldown_minutes: 60

dependencies:
  - upstream-pipeline

remediation:
  enabled: true
  dry_run: true
  auto_remediate_min_confidence: 0.9

auto_fix:
  enabled: true
  confidence_threshold: 0.90
  test_command: "python -m pytest tests/ -x -q"
  scope:
    allow: ["src/", "lib/"]
    deny: ["*.yaml", "*.yml"]
```

```python
from flow_doctor import FlowDoctor

fd = FlowDoctor.from_config(config_path="flow-doctor.yaml")
```

`${VAR}` references resolve from the process environment at load time. **Unresolved references raise `ConfigError`** — no silent passthrough.

## Environment Variables

flow-doctor reads credentials from environment variables as its primary configuration mechanism. Every notifier has a documented fallback chain: explicit value → `FLOW_DOCTOR_*` canonical name → common conventions.

The contract is a typed `pydantic-settings` model (`flow_doctor.core.settings.FlowDoctorSettings`), so each field resolves — in precedence order — from:

1. the **process environment** (`FLOW_DOCTOR_*` canonical name, then the legacy aliases below),
2. a **`.env` file** (path via `FLOW_DOCTOR_ENV_FILE`, default `.env`),
3. a **secrets directory** (`FLOW_DOCTOR_SECRETS_DIR` — one file per env-var name; Docker / Kubernetes file-mounted secrets).

So a self-hosted / compose deploy can drop a `.env` next to the app or mount file secrets — no code change. Example `.env`:

```dotenv
FLOW_DOCTOR_TELEGRAM_BOT_TOKEN=1234567890:ABC...
FLOW_DOCTOR_TELEGRAM_CHAT_ID=-1001234567890
```

### Canonical contract

| Variable | Used by | Fallback chain | Required when |
|---|---|---|---|
| `FLOW_DOCTOR_TELEGRAM_BOT_TOKEN` | Telegram notifier | `FLOW_DOCTOR_TELEGRAM_BOT_TOKEN` → `TELEGRAM_BOT_TOKEN` | Telegram notifier config has no explicit `bot_token` field |
| `FLOW_DOCTOR_TELEGRAM_CHAT_ID` | Telegram notifier | `FLOW_DOCTOR_TELEGRAM_CHAT_ID` → `TELEGRAM_CHAT_ID` | Telegram notifier config has no explicit `chat_id` field |
| `FLOW_DOCTOR_GITHUB_TOKEN` | GitHub notifier, auto-fix PR creator | `FLOW_DOCTOR_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN` | Any GitHub notifier or auto-fix is configured |
| `FLOW_DOCTOR_GITHUB_REPO` | GitHub notifier | `FLOW_DOCTOR_GITHUB_REPO` | GitHub notifier config has no explicit `repo` field |
| `FLOW_DOCTOR_SMTP_PASSWORD` | Email notifier | `FLOW_DOCTOR_SMTP_PASSWORD` → `GMAIL_APP_PASSWORD` | SMTP requires auth |
| `FLOW_DOCTOR_SMTP_SENDER` | Email notifier | `FLOW_DOCTOR_SMTP_SENDER` → `EMAIL_SENDER` | Email notifier config has no explicit `sender` field |
| `FLOW_DOCTOR_SMTP_RECIPIENTS` | Email notifier | `FLOW_DOCTOR_SMTP_RECIPIENTS` → `EMAIL_RECIPIENTS` | Email notifier config has no explicit `recipients` field |
| `FLOW_DOCTOR_SLACK_WEBHOOK` | Slack notifier | `FLOW_DOCTOR_SLACK_WEBHOOK` → `SLACK_WEBHOOK_URL` | Slack notifier config has no explicit `webhook_url` field |
| `FLOW_DOCTOR_S3_BUCKET` | S3 notifier | `FLOW_DOCTOR_S3_BUCKET` → `CHANGELOG_BUCKET` | S3 notifier config has no explicit `bucket` field |
| `FLOW_DOCTOR_ANTHROPIC_API_KEY` | LLM diagnosis, auto-fix generator | `FLOW_DOCTOR_ANTHROPIC_API_KEY` → `ANTHROPIC_API_KEY` | `diagnosis.enabled: true` or auto-fix is on |
| `FLOW_DOCTOR_SKIP_PREFLIGHT` | All notifiers' `validate()` | (literal) | Set to `1` in tests / offline boot to bypass token/preflight network calls |

**Precedence** for every field is: explicit value in kwargs/yaml → canonical `FLOW_DOCTOR_*` env var → convention fallbacks in the order listed. The first non-empty value wins. Missing values raise `ConfigError` at construction time naming the specific field and the env vars that would satisfy it.

### Env-var-only quickstart — Telegram

Two env vars, two lines of Python, working alerts on the next exception:

```bash
export FLOW_DOCTOR_TELEGRAM_BOT_TOKEN=1234567890:ABC...
export FLOW_DOCTOR_TELEGRAM_CHAT_ID=-1001234567890
```

```python
from flow_doctor import FlowDoctor, TelegramNotifierConfig

fd = FlowDoctor.builder("pipeline").add_notifier(TelegramNotifierConfig()).build()
```

### Strict mode and degraded mode

`FlowDoctor.builder().build()` and `FlowDoctor.from_config()` both default to `strict=True`. Any configuration error (missing required field, unresolved `${VAR}`, unknown notifier type) raises `ConfigError` and prevents startup. This is the recommended default — a non-running flow-doctor is a loud failure; a silently-degraded flow-doctor is a silent one.

If you genuinely want best-effort init that logs errors but keeps running with no notifiers, opt in explicitly:

```python
fd = FlowDoctor.builder("pipeline").build(strict=False)
```

## Logging-handler integration

Attach to Python's logging system if you want every `WARNING+` log to flow through dedup + diagnosis + notify without touching call sites:

```python
import logging
import flow_doctor

fd = flow_doctor.FlowDoctor.builder("pipeline").add_notifier(
    flow_doctor.TelegramNotifierConfig()
).build()

handler = fd.get_handler(level=logging.WARNING)
logging.getLogger().addHandler(handler)

logger.warning("Upstream data is 48h stale")  # → captured + routed
logger.error("S3 backup failed: AccessDenied")
logger.exception("Pipeline crashed")
```

The handler is **non-blocking** — `emit()` enqueues work and returns immediately; a background thread calls `fd.report()` asynchronously.

### Log capture

Attach recent logs to the next error report for richer diagnosis context:

```python
with fd.capture_logs(level=logging.INFO):
    logger.info("Starting scan with 900 tickers...")
    run_pipeline()
    # All captured logs are attached to the next fd.report() call
```

## Features

### Error capture and dedup

- Traceback extraction with frame-based signature hashing
- Configurable cooldown window (default 60 min) — same error captured once, not spammed
- Variable-token normalization: reqIds, conIds, contract symbols, UUIDs, AWS request IDs are stripped before hashing, so a library logging the same error against N objects collapses to one signature
- Cascade detection tags downstream failures caused by upstream dependency outages
- Automatic secret scrubbing (AWS keys, Bearer tokens, passwords in URLs)

### LLM diagnosis

- Structured root cause analysis via Claude: category, confidence, affected files, remediation
- Six categories: `TRANSIENT`, `DATA`, `CODE`, `CONFIG`, `EXTERNAL`, `INFRA`
- Knowledge base caching — known patterns matched for free before calling the LLM
- Git context assembly (recent commits, changed files) for better diagnosis accuracy
- Daily cost cap (default $1.00) and rate limiting (default 3 diagnoses/day)

### Notifications

- **Telegram** — Bot API, per-chat / per-thread routing, mobile push (recommended default)
- **Slack** — webhook-based alerts with severity emoji + diagnosis snippet
- **Email** — SMTP (Gmail/any) with detailed body
- **GitHub issues** — auto-filed with diagnosis, traceback, captured logs, machine-readable metadata
- **S3** — writes schema-1.0.0 entries to a system-wide changelog corpus
- **Daily digest** — summarizes rate-limited / suppressed errors at end of day
- **Custom notifiers** — subclass `flow_doctor.notify.base.Notifier`; the abstract base is a public extension point
- **Category routing** — `notify_on_category` per notifier, gated on diagnosis category, so noisy/curated channels (GitHub issues) can opt in to only human-actionable categories while cheap channels (Telegram/SNS) still page on everything — see [Category routing](#category-routing--separating-capture-from-alert)

### Auto-Fix PRs

Both halves are independently toggleable on the GitHub notifier:

```python
GitHubNotifierConfig(
    repo="me/app",
    auto_create_issue=True,   # toggle 1: file an issue on failure (default True)
    auto_fix_pr=False,        # toggle 2: auto-generate a fix PR for it (default False)
)
```

- **`auto_create_issue`** (default `True`) — file a GitHub issue on failure. Set `False` to silence issue creation without removing the notifier block.
- **`auto_fix_pr`** (default `False`) — when `True`, Flow Doctor applies the `fix_label` (`flow-doctor:fix`) to each filed issue, which fires the fix workflow automatically with **no human label step**. Leave `False` for the human-in-the-loop default below.

> **`auto_fix_pr` requires same-repo issues.** The `flow-doctor-fix` workflow triggers on `issues: labeled` in the repo it's installed in — GitHub gives no built-in way to fire it from an issue filed in a different repo. If you're routing issues to a [centralized backlog repo](#category-routing--separating-capture-from-alert), either leave `auto_fix_pr` off for that notifier (file for triage only) or build your own `repository_dispatch` relay. Flow Doctor logs a `WARNING` at init if `auto_fix_pr=True` is combined with a `repo` that differs from the app's own (set via `.with_repo(...)` or `FlowDoctorConfig.repo`).

The fix-generation pipeline itself is the same either way:

1. An error occurs and Flow Doctor creates a GitHub issue with structured diagnosis
2. The `flow-doctor:fix` label is applied — by a human (default) or automatically (`auto_fix_pr=True`)
3. GitHub Actions triggers `flow-doctor generate-fix`
4. The CLI generates a diff via LLM, validates against scope rules, runs tests
5. If tests pass, a PR is opened. If tests fail, a comment explains what went wrong.

**Safety gates** — fix generation is skipped when:
- Confidence below threshold (default 90%)
- Category is `EXTERNAL` or `INFRA` (nothing to fix in code)
- Config issue involves credentials/secrets
- Generated diff touches files outside configured scope
- Tests fail after applying the fix

### Remediation playbooks

Define patterns that map failure signatures to automated actions:

```python
from flow_doctor.remediation.playbook import (
    Playbook, PlaybookPattern, RemediationAction, RemediationType,
)

my_playbook = Playbook(patterns=[
    PlaybookPattern(
        name="service_down",
        description="App service not responding",
        category="INFRA",
        message_pattern=r"(connection refused|service unavailable)",
        action=RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="Restart the app service",
            commands=["sudo systemctl restart myapp"],
            ssm_target="app-server",
        ),
    ),
])
```

## Auto-Fix CLI

```bash
flow-doctor generate-fix \
  --issue-number 42 \
  --repo owner/repo \
  --token $GITHUB_TOKEN \
  --config flow-doctor.yaml \
  --dry-run
```

### GitHub Actions setup

GitHub only fires a workflow on a repository event (`issues: labeled`) if the
workflow file lives in **that repo's** `.github/workflows/` — there is no
org-level workflow for issue events (org rulesets cover only push/PR). So each
repo needs *a* file. **Recommended: a thin stub that delegates to the reusable
workflow shipped in this repo**, so all the install/run/comment logic lives
here once and a behavior change is a version-pin bump rather than an edit in
every repo. Copy to `.github/workflows/flow-doctor-fix.yml`:

```yaml
name: Flow Doctor Fix
on:
  issues:
    types: [labeled]
jobs:
  fix:
    if: github.event.label.name == 'flow-doctor:fix'
    permissions:
      contents: write
      pull-requests: write
      issues: write
    uses: nousergon/flow-doctor/.github/workflows/fix.yml@v0.6.0rc6   # pin to a tag
    secrets: inherit
```

Inputs (all optional) let you override `python-version`, `requirements-file`,
`config-path`, or pass `extra-pip-install` when the caller's requirements don't
already bring `flow-doctor[diagnosis,s3]`. `secrets: inherit` passes
`ANTHROPIC_API_KEY` (define it once as an org-level Actions secret to avoid
per-repo setup). The repo/org setting **"Allow GitHub Actions to create and
approve pull requests"** must be enabled for the PR step to succeed.

<details><summary>Self-contained alternative (no reusable workflow)</summary>

```yaml
name: Flow Doctor Fix
on:
  issues:
    types: [labeled]
jobs:
  generate-fix:
    if: github.event.label.name == 'flow-doctor:fix'
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install "flow-doctor[diagnosis]"
      - run: |
          python -m flow_doctor.fix.cli generate-fix \
            --issue-number ${{ github.event.issue.number }} \
            --repo ${{ github.repository }} \
            --token $GITHUB_TOKEN
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

</details>

<a name="migrating"></a>

## Migrating to 0.6.0

`flow_doctor.init()` was **removed** in 0.6.0 (it was `@deprecated` through 0.5.x). Two drop-in replacements:

```python
# was: fd = flow_doctor.init(config_path="flow-doctor.yaml")
# now, same yaml + kwargs contract:
fd = FlowDoctor.from_config(config_path="flow-doctor.yaml")

# or the typed, IDE-discoverable builder (no yaml):
fd = (
    FlowDoctor.builder("pipeline")
    .add_notifier(TelegramNotifierConfig())
    .build()
)
```

Existing `flow-doctor.yaml` files keep working unchanged via `from_config`. The omnibus `NotifyChannelConfig` is no longer a public/deprecated surface — construct typed `*NotifierConfig` objects from `flow_doctor.notify` instead.

## Architecture

```
flow_doctor/
  core/           # Client, builder, config (Pydantic v2), models, dedup,
                  # rate limiting, scrubber, logging handler, contextvars
  _protocol.py    # FlowDoctorProtocol public contract
  notify/         # Telegram, Slack, Email, GitHub, S3 — concrete notifiers
                  # + typed Pydantic config models (discriminated union)
  diagnosis/      # LLM provider, context assembly, knowledge base, git context
  digest/         # Daily digest generator
  fix/            # Auto-fix: LLM generator, scope guard, test validator, PR creator, CLI
  remediation/    # Decision gate, executor, playbook patterns
  storage/        # SQLite backend (thread-safe, per-thread connections)
  testing/        # RecordingFlowDoctor + pytest plugin (auto-discovered)
  otel.py         # Report → OTel SpanEvent serialization adapter
  py.typed        # PEP 561 marker — annotations are authoritative for mypy/pyright
```

## Development

```bash
git clone https://github.com/cipher813/flow-doctor.git
cd flow-doctor
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,diagnosis]"

python -m pytest tests/ -x -q                          # 414 tests
python -m coverage run -m pytest && python -m coverage report  # coverage
python examples/smoke_test.py              # end-to-end smoke test
```

## License

[MIT](LICENSE)

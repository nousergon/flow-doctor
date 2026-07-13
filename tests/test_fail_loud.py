"""Tests for the v0.2.0 fail-loud contract.

These tests exercise the behavior changes introduced by the fail-loud
refactor: init raises ConfigError on missing notifier credentials, on
unresolved ``${VAR}`` references, and on unknown notifier types. The
``FLOW_DOCTOR_*`` env var fallback chain is also covered here, as are
``strict=False`` degraded mode and the ``allow_unresolved=True`` escape
hatch for unit tests.
"""

import os
import tempfile

import pytest

from flow_doctor import ConfigError, FlowDoctor
from flow_doctor.core.config import _resolve_env_vars, load_config
from flow_doctor.core.errors import StorageBackendError


# ──────────────────────────────────────────────────────────────────────
# Fixture: scrub FLOW_DOCTOR_* and common env vars between tests so
# tests don't pick up the developer's real environment and mask bugs.
# ──────────────────────────────────────────────────────────────────────
_SCRUBBED = [
    "FLOW_DOCTOR_GITHUB_TOKEN",
    "FLOW_DOCTOR_GITHUB_REPO",
    "FLOW_DOCTOR_SMTP_PASSWORD",
    "FLOW_DOCTOR_SMTP_SENDER",
    "FLOW_DOCTOR_SMTP_RECIPIENTS",
    "FLOW_DOCTOR_SLACK_WEBHOOK",
    "FLOW_DOCTOR_ANTHROPIC_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GMAIL_APP_PASSWORD",
    "EMAIL_SENDER",
    "EMAIL_RECIPIENTS",
    "SLACK_WEBHOOK_URL",
    "ANTHROPIC_API_KEY",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure no stray flow-doctor env vars leak between tests."""
    for name in _SCRUBBED:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def sqlite_store(tmp_path):
    """A throwaway SQLite store config so tests don't touch shared state."""
    return {"type": "sqlite", "path": str(tmp_path / "flow_doctor.db")}


# ──────────────────────────────────────────────────────────────────────
# Unresolved ${VAR} references raise ConfigError
# ──────────────────────────────────────────────────────────────────────
def test_unresolved_env_var_raises():
    """An unset ${VAR} reference should raise ConfigError with the var name."""
    with pytest.raises(ConfigError) as exc_info:
        _resolve_env_vars("${DEFINITELY_NOT_SET_XYZ}")
    assert "DEFINITELY_NOT_SET_XYZ" in str(exc_info.value)


def test_unresolved_env_var_lists_all_missing():
    """Multiple unresolved vars in the same string should all be reported."""
    with pytest.raises(ConfigError) as exc_info:
        _resolve_env_vars("${VAR_A}/${VAR_B}/${VAR_C}")
    msg = str(exc_info.value)
    assert "VAR_A" in msg
    assert "VAR_B" in msg
    assert "VAR_C" in msg


def test_allow_unresolved_preserves_literal():
    """allow_unresolved=True should return the literal ${VAR} for test use."""
    result = _resolve_env_vars("${NOT_SET_AT_ALL}", allow_unresolved=True)
    assert result == "${NOT_SET_AT_ALL}"


def test_resolved_env_var_substitutes(monkeypatch):
    """A set ${VAR} reference should substitute cleanly (unchanged behavior)."""
    monkeypatch.setenv("SOME_TEST_VAR", "hello")
    assert _resolve_env_vars("${SOME_TEST_VAR}") == "hello"


def test_yaml_load_raises_on_unresolved_var():
    """Loading a YAML with an unresolved ${VAR} should raise ConfigError."""
    yaml_content = """
flow_name: test
notify:
  - type: github
    repo: owner/repo
    token: ${UNSET_TOKEN_VAR}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        try:
            with pytest.raises(ConfigError) as exc_info:
                load_config(config_path=f.name)
            assert "UNSET_TOKEN_VAR" in str(exc_info.value)
        finally:
            os.unlink(f.name)


# ──────────────────────────────────────────────────────────────────────
# Missing notifier credentials raise ConfigError at FlowDoctor.from_config() time
# ──────────────────────────────────────────────────────────────────────
def test_github_notifier_missing_token_raises(sqlite_store):
    """github notifier with repo but no token should raise ConfigError."""
    with pytest.raises(ConfigError) as exc_info:
        FlowDoctor.from_config(
            flow_name="test",
            store=sqlite_store,
            notify=[{"type": "github", "repo": "owner/repo"}],
        )
    msg = str(exc_info.value)
    assert "token" in msg.lower()
    assert "FLOW_DOCTOR_GITHUB_TOKEN" in msg


def test_github_notifier_missing_repo_raises(sqlite_store):
    """github notifier with token but no repo should raise ConfigError."""
    with pytest.raises(ConfigError) as exc_info:
        FlowDoctor.from_config(
            flow_name="test",
            store=sqlite_store,
            notify=[{"type": "github", "token": "ghp_xyz"}],
        )
    assert "repo" in str(exc_info.value).lower()


def test_slack_notifier_missing_webhook_raises(sqlite_store):
    """slack notifier without webhook_url should raise ConfigError."""
    with pytest.raises(ConfigError) as exc_info:
        FlowDoctor.from_config(
            flow_name="test",
            store=sqlite_store,
            notify=[{"type": "slack", "channel": "#alerts"}],
        )
    msg = str(exc_info.value)
    assert "webhook" in msg.lower()
    assert "FLOW_DOCTOR_SLACK_WEBHOOK" in msg


def test_email_notifier_missing_sender_raises(sqlite_store):
    """email notifier missing sender should raise ConfigError naming the field."""
    with pytest.raises(ConfigError) as exc_info:
        FlowDoctor.from_config(
            flow_name="test",
            store=sqlite_store,
            notify=[{"type": "email", "recipients": "x@example.com"}],
        )
    msg = str(exc_info.value)
    assert "sender" in msg.lower()


def test_email_notifier_missing_recipients_raises(sqlite_store):
    """email notifier missing recipients should raise ConfigError."""
    with pytest.raises(ConfigError) as exc_info:
        FlowDoctor.from_config(
            flow_name="test",
            store=sqlite_store,
            notify=[{"type": "email", "sender": "alerts@example.com"}],
        )
    assert "recipients" in str(exc_info.value).lower()


def test_unknown_notifier_type_raises(sqlite_store):
    """An unknown notifier type should raise ConfigError."""
    with pytest.raises(ConfigError) as exc_info:
        FlowDoctor.from_config(
            flow_name="test",
            store=sqlite_store,
            notify=[{"type": "carrier_pigeon"}],
        )
    assert "carrier_pigeon" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────────────
# FLOW_DOCTOR_* env var fallback chain resolves credentials
# ──────────────────────────────────────────────────────────────────────
def test_github_token_from_flow_doctor_env(monkeypatch, sqlite_store):
    """FLOW_DOCTOR_GITHUB_TOKEN should satisfy a github notifier missing token."""
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "ghp_fallback_123")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "owner/repo"}],
    )
    assert len(fd._notifiers) == 1
    assert fd._notifiers[0].token == "ghp_fallback_123"


def test_github_token_from_gh_token_convention(monkeypatch, sqlite_store):
    """GH_TOKEN (gh CLI convention) should satisfy a github notifier."""
    monkeypatch.setenv("GH_TOKEN", "ghp_gh_cli")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "owner/repo"}],
    )
    assert fd._notifiers[0].token == "ghp_gh_cli"


def test_github_token_from_github_token_convention(monkeypatch, sqlite_store):
    """GITHUB_TOKEN (GitHub Actions convention) should satisfy a github notifier."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_actions")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "owner/repo"}],
    )
    assert fd._notifiers[0].token == "ghp_actions"


def test_github_token_flow_doctor_takes_precedence_over_gh_token(
    monkeypatch, sqlite_store
):
    """FLOW_DOCTOR_GITHUB_TOKEN should win over GH_TOKEN when both are set."""
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "primary")
    monkeypatch.setenv("GH_TOKEN", "fallback")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "owner/repo"}],
    )
    assert fd._notifiers[0].token == "primary"


def test_explicit_token_takes_precedence_over_env(monkeypatch, sqlite_store):
    """A token set in notify config should win over env vars."""
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "from_env")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[
            {"type": "github", "repo": "owner/repo", "token": "from_config"}
        ],
    )
    assert fd._notifiers[0].token == "from_config"


def test_github_repo_from_env(monkeypatch, sqlite_store):
    """FLOW_DOCTOR_GITHUB_REPO should satisfy a github notifier missing repo."""
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_REPO", "env/repo")
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "ghp_xyz")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github"}],
    )
    assert fd._notifiers[0].repo == "env/repo"


def test_slack_webhook_from_flow_doctor_env(monkeypatch, sqlite_store):
    """FLOW_DOCTOR_SLACK_WEBHOOK should satisfy a slack notifier."""
    monkeypatch.setenv(
        "FLOW_DOCTOR_SLACK_WEBHOOK", "https://hooks.slack.com/services/XXX"
    )
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "slack"}],
    )
    assert fd._notifiers[0].webhook_url.endswith("/XXX")


def test_slack_webhook_from_legacy_env(monkeypatch, sqlite_store):
    """SLACK_WEBHOOK_URL (legacy convention) should satisfy a slack notifier."""
    monkeypatch.setenv(
        "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/LEGACY"
    )
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "slack"}],
    )
    assert fd._notifiers[0].webhook_url.endswith("/LEGACY")


def test_email_password_from_flow_doctor_env(monkeypatch, sqlite_store):
    """FLOW_DOCTOR_SMTP_PASSWORD should be picked up for email notifier."""
    monkeypatch.setenv("FLOW_DOCTOR_SMTP_PASSWORD", "app_password_xyz")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[
            {
                "type": "email",
                "sender": "alerts@example.com",
                "recipients": "oncall@example.com",
            }
        ],
    )
    assert fd._notifiers[0].smtp_password == "app_password_xyz"


def test_email_sender_and_recipients_from_env(monkeypatch, sqlite_store):
    """EMAIL_SENDER + EMAIL_RECIPIENTS env vars should satisfy an email notifier."""
    monkeypatch.setenv("EMAIL_SENDER", "alerts@example.com")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "oncall@example.com")
    monkeypatch.setenv("FLOW_DOCTOR_SMTP_PASSWORD", "pw")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "email"}],
    )
    assert fd._notifiers[0].sender == "alerts@example.com"


# ──────────────────────────────────────────────────────────────────────
# strict=False preserves degraded mode
# ──────────────────────────────────────────────────────────────────────
def test_strict_false_swallows_init_errors(sqlite_store):
    """strict=False should suppress ConfigError and run in degraded mode."""
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "owner/repo"}],  # missing token
        strict=False,
    )
    assert fd._healthy is False
    assert fd._notifiers == []


def test_strict_true_is_default(sqlite_store):
    """The default (no strict kwarg) should raise on misconfiguration."""
    with pytest.raises(ConfigError):
        FlowDoctor.from_config(
            flow_name="test",
            store=sqlite_store,
            notify=[{"type": "github", "repo": "owner/repo"}],
        )


# ──────────────────────────────────────────────────────────────────────
# StorageBackendError (infra/runtime failure) always degrades, even under
# strict=True — a telemetry backend must never crash the calling producer
# over its OWN transient failure. Regression coverage for
# nousergon/alpha-engine-config#2465: an IAM permission gap on a shared
# DynamoDB flow-doctor table crashed a production data-collection workload
# because `strict=True` re-raised the backend's AccessDeniedException.
# ──────────────────────────────────────────────────────────────────────
def test_dynamodb_missing_table_name_still_raises_configerror(monkeypatch):
    """A missing table_name is misconfiguration — must still raise ConfigError
    under strict=True, unaffected by the StorageBackendError carve-out."""
    with pytest.raises(ConfigError, match="table_name"):
        FlowDoctor.from_config(
            flow_name="test",
            store={"type": "dynamodb"},
            notify=[{"type": "github", "repo": "owner/repo", "token": "ghp_xyz"}],
        )


def test_dynamodb_backend_failure_degrades_even_when_strict_true(monkeypatch):
    """An init_schema() failure from the backend itself (e.g. IAM AccessDenied)
    must NEVER crash the caller, even with the default strict=True — this is
    the exact production incident this test guards against."""
    pytest.importorskip("boto3")
    from botocore.exceptions import ClientError
    from flow_doctor.storage.dynamodb import DynamoDBStorage

    def _boom(self):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "DescribeTable",
        )

    monkeypatch.setattr(DynamoDBStorage, "init_schema", _boom)

    fd = FlowDoctor.from_config(
        flow_name="test",
        store={"type": "dynamodb", "table_name": "flow-doctor-store"},
        notify=[{"type": "github", "repo": "owner/repo", "token": "ghp_xyz"}],
        strict=True,
    )
    assert fd._healthy is False
    assert fd._store is None


def test_dynamodb_backend_failure_logs_loudly(monkeypatch, capsys):
    """The degraded-mode message must be visible on stderr, not silently swallowed."""
    pytest.importorskip("boto3")
    from botocore.exceptions import ClientError
    from flow_doctor.storage.dynamodb import DynamoDBStorage

    def _boom(self):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "DescribeTable",
        )

    monkeypatch.setattr(DynamoDBStorage, "init_schema", _boom)

    FlowDoctor.from_config(
        flow_name="test",
        store={"type": "dynamodb", "table_name": "flow-doctor-store"},
        notify=[{"type": "github", "repo": "owner/repo", "token": "ghp_xyz"}],
        strict=True,
    )
    captured = capsys.readouterr()
    assert "storage backend init failed" in captured.err
    assert "AccessDeniedException" in captured.err


def test_dynamodb_backend_error_is_storage_backend_error_subclass():
    """StorageBackendError must be a FlowDoctorError so existing broad
    ``except FlowDoctorError`` callers still catch it if it ever surfaces."""
    from flow_doctor.core.errors import FlowDoctorError

    assert issubclass(StorageBackendError, FlowDoctorError)


# ──────────────────────────────────────────────────────────────────────
# Successful init smoke tests
# ──────────────────────────────────────────────────────────────────────
def test_github_notifier_full_config_succeeds(sqlite_store):
    """A fully-specified github notifier should init cleanly."""
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[
            {
                "type": "github",
                "repo": "owner/repo",
                "token": "ghp_xyz",
                "labels": ["bot", "urgent"],
            }
        ],
    )
    assert fd._healthy is True
    assert len(fd._notifiers) == 1
    assert fd._notifiers[0].repo == "owner/repo"
    assert fd._notifiers[0].labels == ["bot", "urgent"]


def test_env_var_only_quickstart(monkeypatch, sqlite_store):
    """The README env-var-only quickstart should work end-to-end."""
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_REPO", "cipher813/test")
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "ghp_env_only")
    fd = FlowDoctor.from_config(
        flow_name="env-only-test",
        store=sqlite_store,
        notify=[{"type": "github"}],
    )
    assert fd._healthy is True
    assert fd._notifiers[0].repo == "cipher813/test"
    assert fd._notifiers[0].token == "ghp_env_only"

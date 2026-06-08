"""0.6.0rc2 typed FLOW_DOCTOR_* settings contract (pydantic-settings):
canonical/alias precedence, .env file, and secrets-directory resolution."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from flow_doctor.core.client import _env_fallback
from flow_doctor.core.settings import FlowDoctorSettings

# Every env name the resolution paths under test might read, cleared per test
# so a stray CI env var can't leak in.
_ENV_NAMES = [
    "FLOW_DOCTOR_S3_BUCKET", "CHANGELOG_BUCKET",
    "FLOW_DOCTOR_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
    "FLOW_DOCTOR_ENV_FILE", "FLOW_DOCTOR_SECRETS_DIR",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield


def test_canonical_env_name_resolves(monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_S3_BUCKET", "canonical")
    assert _env_fallback("s3_bucket") == "canonical"


def test_legacy_alias_resolves(monkeypatch):
    monkeypatch.setenv("CHANGELOG_BUCKET", "legacy")
    assert _env_fallback("s3_bucket") == "legacy"


def test_canonical_beats_legacy_alias(monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_S3_BUCKET", "canonical")
    monkeypatch.setenv("CHANGELOG_BUCKET", "legacy")
    assert _env_fallback("s3_bucket") == "canonical"


def test_github_token_alias_chain(monkeypatch):
    # GH_TOKEN is the middle of the chain; with the canonical unset it wins.
    monkeypatch.setenv("GH_TOKEN", "gh")
    assert _env_fallback("github_token") == "gh"


def test_missing_returns_none():
    assert _env_fallback("s3_bucket") is None


def test_dotenv_file_resolution(monkeypatch, tmp_path):
    env_file = tmp_path / "flow.env"
    env_file.write_text("FLOW_DOCTOR_S3_BUCKET=from-dotenv\n")
    monkeypatch.setenv("FLOW_DOCTOR_ENV_FILE", str(env_file))
    assert _env_fallback("s3_bucket") == "from-dotenv"


def test_process_env_beats_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / "flow.env"
    env_file.write_text("FLOW_DOCTOR_S3_BUCKET=from-dotenv\n")
    monkeypatch.setenv("FLOW_DOCTOR_ENV_FILE", str(env_file))
    monkeypatch.setenv("FLOW_DOCTOR_S3_BUCKET", "from-process-env")
    assert _env_fallback("s3_bucket") == "from-process-env"


def test_secrets_dir_resolution(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    # pydantic-settings reads one file per env-var name in the secrets dir.
    (secrets / "FLOW_DOCTOR_S3_BUCKET").write_text("from-secret-file")
    monkeypatch.setenv("FLOW_DOCTOR_SECRETS_DIR", str(secrets))
    assert _env_fallback("s3_bucket") == "from-secret-file"


def test_malformed_env_file_degrades_to_process_env(monkeypatch, tmp_path):
    # A missing/unreadable env_file path must not crash resolution.
    monkeypatch.setenv("FLOW_DOCTOR_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    monkeypatch.setenv("FLOW_DOCTOR_S3_BUCKET", "still-resolves")
    assert _env_fallback("s3_bucket") == "still-resolves"


def test_settings_model_is_declarative():
    """The typed contract exposes the credential fields directly (IDE/mypy)."""
    s = FlowDoctorSettings(_env_file=None)
    for field in ("github_token", "smtp_password", "telegram_bot_token", "s3_bucket"):
        assert hasattr(s, field)


def test_end_to_end_notifier_resolves_token_from_dotenv(monkeypatch, tmp_path):
    """A github notifier with no inline token resolves it from a .env file —
    proving the settings layer is wired into FlowDoctor init."""
    from flow_doctor import FlowDoctor, GitHubNotifierConfig
    from flow_doctor.notify.github import GitHubNotifier

    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    env_file = tmp_path / "flow.env"
    env_file.write_text("FLOW_DOCTOR_GITHUB_TOKEN=tok-from-dotenv\n")
    monkeypatch.setenv("FLOW_DOCTOR_ENV_FILE", str(env_file))

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = (
            FlowDoctor.builder("dotenv-e2e")
            .with_store(path=f.name)
            .add_notifier(GitHubNotifierConfig(repo="o/r"))  # no token inline
            .build()
        )
    gh = next(n for n in fd._notifiers if isinstance(n, GitHubNotifier))
    assert gh.token == "tok-from-dotenv"

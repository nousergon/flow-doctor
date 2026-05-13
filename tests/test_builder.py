"""Tests for the FlowDoctor.builder() fluent API + typed notifier configs.

Mirrors the morning-signal cutover use case in private/plug-and-play-260513.md:
a downstream consumer constructs a FlowDoctor without yaml, in 5 typed lines.
"""

from __future__ import annotations

import tempfile

import pytest

from flow_doctor import (
    EmailNotifierConfig,
    FlowDoctor,
    FlowDoctorBuilder,
    GitHubNotifierConfig,
    S3NotifierConfig,
    SlackNotifierConfig,
)
from flow_doctor.core.config import (
    DiagnosisConfig,
    FlowDoctorConfig,
    GitHubConfig,
    NotifyChannelConfig,
    RateLimitConfig,
)


# ---------------------------------------------------------------------------
# Per-type notifier configs
# ---------------------------------------------------------------------------


def test_email_notifier_config_normalizes_list_to_csv():
    cfg = EmailNotifierConfig(
        sender="x@y.com",
        recipients=["a@y.com", "b@y.com", " c@y.com "],
        smtp_password="secret",
    )
    assert cfg.recipients == "a@y.com, b@y.com, c@y.com"


def test_email_notifier_config_accepts_csv_string_unchanged():
    cfg = EmailNotifierConfig(
        sender="x@y.com", recipients="a@y.com, b@y.com"
    )
    assert cfg.recipients == "a@y.com, b@y.com"


def test_email_notifier_config_to_channel_config_round_trip():
    cfg = EmailNotifierConfig(
        sender="x@y.com",
        recipients=["a@y.com"],
        smtp_password="secret",
        smtp_host="smtp.example.com",
        smtp_port=25,
    )
    legacy = cfg.to_channel_config()
    assert isinstance(legacy, NotifyChannelConfig)
    assert legacy.type == "email"
    assert legacy.sender == "x@y.com"
    assert legacy.recipients == "a@y.com"
    assert legacy.smtp_host == "smtp.example.com"
    assert legacy.smtp_port == 25
    assert legacy.smtp_password == "secret"


def test_slack_notifier_config_to_channel_config():
    cfg = SlackNotifierConfig(webhook_url="https://hooks", channel="#ops")
    legacy = cfg.to_channel_config()
    assert legacy.type == "slack"
    assert legacy.webhook_url == "https://hooks"
    assert legacy.channel == "#ops"


def test_github_notifier_config_to_channel_config():
    cfg = GitHubNotifierConfig(
        repo="owner/repo", token="ghs_xxx", labels=["bug", "flow-doctor"]
    )
    legacy = cfg.to_channel_config()
    assert legacy.type == "github"
    assert legacy.repo == "owner/repo"
    assert legacy.token == "ghs_xxx"
    assert legacy.labels == ["bug", "flow-doctor"]


def test_s3_notifier_config_to_channel_config_preserves_changelog_fields():
    cfg = S3NotifierConfig(
        bucket="alpha-engine-research",
        subsystem="predictor",
        entry_prefix="changelog/entries",
        default_root_cause_category="data_quality",
        default_resolution_type="config",
    )
    legacy = cfg.to_channel_config()
    assert legacy.type == "s3"
    assert legacy.bucket == "alpha-engine-research"
    assert legacy.subsystem == "predictor"
    assert legacy.entry_prefix == "changelog/entries"
    assert legacy.default_root_cause_category == "data_quality"
    assert legacy.default_resolution_type == "config"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def test_builder_returns_builder_instance():
    builder = FlowDoctor.builder("test-flow")
    assert isinstance(builder, FlowDoctorBuilder)


def test_builder_methods_return_self_for_chaining():
    builder = FlowDoctor.builder("test-flow")
    assert builder.with_repo("owner/repo") is builder
    assert builder.with_dedup(cooldown_minutes=30) is builder
    assert (
        builder.add_notifier(SlackNotifierConfig(webhook_url="https://x"))
        is builder
    )


def test_builder_build_config_assembles_typed_inputs():
    config = (
        FlowDoctor.builder("morning-signal")
        .with_repo("owner/repo", owner="@brian")
        .add_notifier(
            EmailNotifierConfig(
                sender="x@y.com",
                recipients=["x@y.com"],
                smtp_password="secret",
            )
        )
        .with_dedup(cooldown_minutes=60)
        .build_config()
    )
    assert isinstance(config, FlowDoctorConfig)
    assert config.flow_name == "morning-signal"
    assert config.repo == "owner/repo"
    assert config.owner == "@brian"
    assert config.dedup_cooldown_minutes == 60
    assert len(config.notify) == 1
    notif = config.notify[0]
    assert notif.type == "email"
    assert notif.sender == "x@y.com"
    assert notif.recipients == "x@y.com"
    assert notif.smtp_password == "secret"


def test_builder_accepts_legacy_notify_channel_config():
    """Back-compat: existing 0.4.0 callers that construct a raw
    NotifyChannelConfig can still pass it to add_notifier()."""
    legacy = NotifyChannelConfig(type="slack", webhook_url="https://x")
    config = FlowDoctor.builder("test").add_notifier(legacy).build_config()
    assert config.notify[0] is legacy


def test_builder_unspecified_sections_use_defaults():
    config = FlowDoctor.builder("min").build_config()
    assert config.flow_name == "min"
    assert config.notify == []
    assert config.store.type == "sqlite"
    assert config.diagnosis.enabled is False
    assert config.remediation.enabled is False


def test_builder_with_rate_limits_overrides_defaults():
    config = (
        FlowDoctor.builder("test")
        .with_rate_limits(RateLimitConfig(max_alerts_per_day=42))
        .build_config()
    )
    assert config.rate_limits.max_alerts_per_day == 42


def test_builder_with_diagnosis_and_github_compose():
    config = (
        FlowDoctor.builder("test")
        .with_diagnosis(DiagnosisConfig(enabled=True, api_key="sk-xxx"))
        .with_github(GitHubConfig(token="ghs_xxx"))
        .build_config()
    )
    assert config.diagnosis.enabled is True
    assert config.diagnosis.api_key == "sk-xxx"
    assert config.github.token == "ghs_xxx"


def test_builder_with_dependencies_replaces_list():
    config = (
        FlowDoctor.builder("test")
        .with_dependencies(["pkg-a", "pkg-b"])
        .build_config()
    )
    assert config.dependencies == ["pkg-a", "pkg-b"]


def test_builder_build_constructs_flow_doctor_instance():
    """End-to-end: builder.build() returns a working FlowDoctor.

    Uses sqlite at a temp path + no notifiers so we don't touch the network.
    """
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        fd = (
            FlowDoctor.builder("test-flow")
            .with_store(path=f.name)
            .build()
        )
        assert fd.config.flow_name == "test-flow"
        assert fd.config.store.path == f.name
        # report() should not crash with zero notifiers configured
        report_id = fd.report("synthetic", severity="error")
        assert report_id is not None


def test_builder_multiple_notifiers_preserved_in_order():
    config = (
        FlowDoctor.builder("test")
        .add_notifier(SlackNotifierConfig(webhook_url="https://slack"))
        .add_notifier(EmailNotifierConfig(sender="x@y.com", recipients="x@y.com"))
        .add_notifier(GitHubNotifierConfig(repo="o/r", token="ghs_xxx"))
        .build_config()
    )
    assert [n.type for n in config.notify] == ["slack", "email", "github"]


# ---------------------------------------------------------------------------
# Discriminated union round-trip
# ---------------------------------------------------------------------------


def test_notifier_config_discriminated_union_picks_correct_type():
    """When deserializing a dict, Pydantic must pick the right concrete
    config based on the ``type`` discriminator."""
    from pydantic import TypeAdapter

    from flow_doctor.notify import NotifierConfig

    adapter = TypeAdapter(NotifierConfig)
    parsed = adapter.validate_python(
        {"type": "email", "sender": "x@y.com", "recipients": "x@y.com"}
    )
    assert isinstance(parsed, EmailNotifierConfig)
    assert parsed.sender == "x@y.com"

    parsed = adapter.validate_python(
        {"type": "slack", "webhook_url": "https://x", "channel": "#ops"}
    )
    assert isinstance(parsed, SlackNotifierConfig)
    assert parsed.channel == "#ops"


def test_notifier_config_union_rejects_unknown_type():
    """Discriminator union must surface unknown channel types as a
    pydantic ValidationError, not a silent fallback."""
    from pydantic import TypeAdapter, ValidationError

    from flow_doctor.notify import NotifierConfig

    adapter = TypeAdapter(NotifierConfig)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "telegram", "webhook_url": "x"})

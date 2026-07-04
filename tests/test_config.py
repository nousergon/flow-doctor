"""Tests for configuration loading."""

import os
import tempfile

import pytest

from flow_doctor.core.config import FlowDoctorConfig, load_config
from flow_doctor.core.errors import ConfigError


def test_load_inline_config():
    config = load_config(
        flow_name="test-flow",
        repo="user/repo",
        owner="@user",
    )
    assert config.flow_name == "test-flow"
    assert config.repo == "user/repo"
    assert config.owner == "@user"


def test_load_yaml_config():
    yaml_content = """
flow_name: research-lambda
repo: user/alpha-engine-research
owner: "@user"
dedup_cooldown_minutes: 30
dependencies:
  - upstream-flow
store:
  type: sqlite
  path: /tmp/test_fd.db
rate_limits:
  max_alerts_per_day: 10
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(config_path=f.name)

    os.unlink(f.name)

    assert config.flow_name == "research-lambda"
    assert config.repo == "user/alpha-engine-research"
    assert config.dedup_cooldown_minutes == 30
    assert config.dependencies == ["upstream-flow"]
    assert config.store.type == "sqlite"
    assert config.store.path == "/tmp/test_fd.db"
    assert config.rate_limits.max_alerts_per_day == 10


def test_load_yaml_with_env_vars():
    os.environ["TEST_WEBHOOK_URL"] = "https://hooks.slack.com/test"
    yaml_content = """
flow_name: test
notify:
  - type: slack
    webhook_url: ${TEST_WEBHOOK_URL}
    channel: "#alerts"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(config_path=f.name)

    os.unlink(f.name)
    del os.environ["TEST_WEBHOOK_URL"]

    assert len(config.notify) == 1
    assert config.notify[0].webhook_url == "https://hooks.slack.com/test"
    assert config.notify[0].channel == "#alerts"


_UNSET_VAR_YAML = """
flow_name: test
notify:
  - type: email
    sender: ${DEFINITELY_UNSET_EMAIL_SENDER}
    recipients: ${DEFINITELY_UNSET_EMAIL_RECIPIENTS}
"""


def test_strict_resolution_raises_on_unset_var():
    """Default load is strict — an unset ${VAR} aborts the load (fail-loud)."""
    os.environ.pop("DEFINITELY_UNSET_EMAIL_SENDER", None)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(_UNSET_VAR_YAML)
        f.flush()
        with pytest.raises(ConfigError, match="DEFINITELY_UNSET_EMAIL_SENDER"):
            load_config(config_path=f.name)
    os.unlink(f.name)


def test_skip_sections_drops_block_with_unset_vars():
    """skip_sections lets a caller that doesn't use a block load even when that
    block references unset vars — the block is dropped before resolution."""
    os.environ.pop("DEFINITELY_UNSET_EMAIL_SENDER", None)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(_UNSET_VAR_YAML)
        f.flush()
        config = load_config(config_path=f.name, skip_sections=("notify",))
    os.unlink(f.name)
    assert config.notify == []


def test_skip_sections_keeps_strict_resolution_on_remaining():
    """A var in a NON-skipped section still fails loud."""
    os.environ.pop("DEFINITELY_UNSET_STORE_PATH", None)
    yaml_content = """
flow_name: test
notify:
  - type: email
    sender: ${DEFINITELY_UNSET_EMAIL_SENDER}
store:
  type: sqlite
  path: ${DEFINITELY_UNSET_STORE_PATH}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        with pytest.raises(ConfigError, match="DEFINITELY_UNSET_STORE_PATH"):
            load_config(config_path=f.name, skip_sections=("notify",))
    os.unlink(f.name)


def test_inline_notify_shorthand():
    os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/test"
    config = load_config(
        flow_name="test",
        notify=["slack:#alpha-alerts", "email:user@example.com"],
    )
    del os.environ["SLACK_WEBHOOK_URL"]

    assert len(config.notify) == 2
    assert config.notify[0].type == "slack"
    assert config.notify[0].channel == "#alpha-alerts"
    assert config.notify[1].type == "email"
    assert config.notify[1].recipients == "user@example.com"


def test_store_string_sqlite():
    config = load_config(
        flow_name="test",
        store="sqlite:///tmp/test.db",
    )
    assert config.store.type == "sqlite"
    assert config.store.path == "/tmp/test.db"


def test_store_string_s3():
    config = load_config(
        flow_name="test",
        store="s3://my-bucket/flow-doctor/",
    )
    assert config.store.type == "s3"
    assert config.store.bucket == "my-bucket"
    assert config.store.prefix == "flow-doctor/"


def test_store_string_dynamodb():
    config = load_config(
        flow_name="test",
        store="dynamodb://flow-doctor-store",
    )
    assert config.store.type == "dynamodb"
    assert config.store.table_name == "flow-doctor-store"


def test_store_dict_dynamodb_with_region():
    config = load_config(
        flow_name="test",
        store={"type": "dynamodb", "table_name": "fd-table", "region": "us-west-2"},
    )
    assert config.store.type == "dynamodb"
    assert config.store.table_name == "fd-table"
    assert config.store.region == "us-west-2"


def test_kwargs_override_yaml():
    yaml_content = """
flow_name: from-yaml
repo: yaml/repo
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(config_path=f.name, flow_name="from-kwargs")

    os.unlink(f.name)
    assert config.flow_name == "from-kwargs"


def test_default_config():
    config = load_config()
    assert config.flow_name == "default"
    assert config.store.type == "sqlite"
    assert config.rate_limits.max_diagnosed_per_day == 3
    assert config.dedup_cooldown_minutes == 60

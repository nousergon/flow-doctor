"""Deprecated-API removal contract (0.6.0).

``flow_doctor.init()`` and the ``@deprecated`` marker on the internal
``NotifyChannelConfig`` were removed in 0.6.0. The supported yaml entry
point is now ``FlowDoctor.from_config()``; the typed builder remains the
recommended path. These tests lock the removal so a future refactor can't
silently resurrect the deprecated surface.
"""

from __future__ import annotations

import tempfile
import warnings

import flow_doctor
from flow_doctor import FlowDoctor
from flow_doctor.core.config import NotifyChannelConfig


def test_init_free_function_removed():
    """``flow_doctor.init`` is gone — removed in 0.6.0."""
    assert not hasattr(flow_doctor, "init"), (
        "flow_doctor.init() was removed in 0.6.0; FlowDoctor.from_config() "
        "is the supported yaml entry point."
    )
    assert "init" not in flow_doctor.__all__


def test_from_config_is_the_supported_yaml_entry_point():
    """``FlowDoctor.from_config()`` replaces ``init()`` with the same
    config_path + inline-kwargs contract, and emits no deprecation warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            fd = FlowDoctor.from_config(store={"type": "sqlite", "path": f.name})
    assert isinstance(fd, FlowDoctor)
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep == [], f"from_config should be deprecation-clean, got: {dep}"


def test_notify_channel_config_no_longer_deprecated():
    """The internal omnibus model lost its ``@deprecated`` marker in 0.6.0
    (it is purely an internal representation now). It constructs cleanly
    and carries no ``__deprecated__`` metadata."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = NotifyChannelConfig(type="slack", webhook_url="https://x")
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep == [], f"NotifyChannelConfig must not warn, got: {dep}"
    assert not hasattr(NotifyChannelConfig, "__deprecated__")
    assert cfg.type == "slack"
    assert cfg.webhook_url == "https://x"


def test_builder_path_is_deprecation_clean():
    """The recommended migration path (typed builder) emits no deprecation
    warning, even though it folds typed configs through
    NotifyChannelConfig.to_channel_config()."""
    from flow_doctor import EmailNotifierConfig

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            FlowDoctor.builder("clean-path").with_store(path=f.name).add_notifier(
                EmailNotifierConfig(sender="x@y.com", recipients="x@y.com")
            ).build()
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep == [], f"Builder path should be deprecation-clean, got: {dep}"

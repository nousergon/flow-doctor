"""PEP 702 @deprecated markers on init() and NotifyChannelConfig.

Verifies both the runtime DeprecationWarning (init only) and the
``__deprecated__`` attribute that mypy/pyright read for static surfacing.
"""

from __future__ import annotations

import tempfile
import warnings

import pytest

from flow_doctor import init
from flow_doctor.core.config import NotifyChannelConfig


def test_init_emits_runtime_deprecation_warning():
    """flow_doctor.init() is the primary migration signal — keep the
    runtime DeprecationWarning so 0.4.0 consumers actually see it."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            # Inline kwargs override yaml; store=path keeps it network-free.
            init(store={"type": "sqlite", "path": f.name})
    msgs = [
        str(w.message)
        for w in caught
        if issubclass(w.category, DeprecationWarning)
    ]
    assert any("flow_doctor.init" in m for m in msgs), (
        f"Expected DeprecationWarning mentioning flow_doctor.init, got: {msgs}"
    )
    assert any("FlowDoctor.builder" in m for m in msgs)


def test_init_carries_pep_702_metadata():
    """Type checkers read ``__deprecated__`` for the static surfacing of
    @deprecated. typing_extensions sets it on the wrapped callable."""
    assert hasattr(init, "__deprecated__")
    assert "FlowDoctor.builder" in init.__deprecated__


def test_notify_channel_config_static_deprecation_only():
    """NotifyChannelConfig is marked deprecated for static checkers
    (mypy/pyright surface it at consumer call sites) but does NOT emit
    a runtime DeprecationWarning, because the omnibus form is still
    the internal lingua franca the builder folds typed configs into."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = NotifyChannelConfig(type="slack", webhook_url="https://x")
    dep_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert dep_warnings == [], (
        "NotifyChannelConfig should not emit a runtime DeprecationWarning "
        f"(was marked with category=None), got: {[str(w.message) for w in dep_warnings]}"
    )
    assert cfg.type == "slack"
    assert cfg.webhook_url == "https://x"


def test_notify_channel_config_carries_pep_702_metadata():
    """The static-checker contract: type checkers read ``__deprecated__``
    even when no runtime warning fires."""
    assert hasattr(NotifyChannelConfig, "__deprecated__")
    assert "SlackNotifierConfig" in NotifyChannelConfig.__deprecated__


def test_builder_path_does_not_trip_either_deprecation():
    """The recommended migration path (FlowDoctor.builder() + typed
    notifier configs) must NOT emit either deprecation warning, even
    though the builder internally lifts typed configs through
    NotifyChannelConfig.to_channel_config()."""
    from flow_doctor import EmailNotifierConfig, FlowDoctor

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            FlowDoctor.builder("clean-path").with_store(path=f.name).add_notifier(
                EmailNotifierConfig(sender="x@y.com", recipients="x@y.com")
            ).build()
    dep_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert dep_warnings == [], (
        f"Builder path should be deprecation-clean, got: "
        f"{[str(w.message) for w in dep_warnings]}"
    )

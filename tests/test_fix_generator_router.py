"""Tests for FixGenerator's router path (provider="router") — the
krepis-router integration for fix-generation (alpha-engine-config-I7014),
added in 0.15.0 alongside the deletion of the direct-Anthropic transport.

Mirrors tests/test_router_provider.py (the diagnosis-side RouterProvider
tests) as closely as the different call shape (a plain-text diff contract,
not a JSON diagnosis) allows — both go through the same shared
``flow_doctor.core.router.resolve_router_edge``.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from flow_doctor.core.router import RouterUnresolvable
from flow_doctor.fix.generator import FixGenerator


def _edge_spec(provider="litellm_edge", model="med-deepseek-v4-pro"):
    return SimpleNamespace(provider=provider, model=model)


def _route(route="litellm_proxy", provider="litellm_edge"):
    return {"route": route, "provider": provider, "deployment_id": "d1"}


def _fake_llm_result(text, model="med-deepseek-v4-pro"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=500)
    return SimpleNamespace(
        text=text,
        model=model,
        provider="litellm_edge",
        usage=usage,
        raw_request={"model": model},
        raw_response=MagicMock(),
    )


def _install_fake_krepis(monkeypatch, *, resolve_group_spec, complete_return=None, cost_return=None):
    """Inject fake krepis/krepis.router/krepis.llm/krepis.cost modules.

    Mirrors ``_install_fake_krepis`` in ``tests/test_router_provider.py`` —
    the real ``krepis`` package need not be installed for these tests to run
    (and, per the CI install list, generally isn't).
    """
    router_mod = types.ModuleType("krepis.router")
    router_mod.resolve_group_spec = resolve_group_spec

    llm_client_instance = MagicMock()
    if complete_return is not None:
        llm_client_instance.complete.return_value = complete_return
    llm_client_cls = MagicMock(return_value=llm_client_instance)

    llm_mod = types.ModuleType("krepis.llm")
    llm_mod.LLMClient = llm_client_cls

    cost_mod = types.ModuleType("krepis.cost")
    cost_mod.record_llm_call = MagicMock(
        return_value=cost_return or {"cost_usd": 0.0, "cost_source": "price_card"}
    )

    krepis_pkg = types.ModuleType("krepis")
    krepis_pkg.router = router_mod
    krepis_pkg.llm = llm_mod
    krepis_pkg.cost = cost_mod

    monkeypatch.setitem(sys.modules, "krepis", krepis_pkg)
    monkeypatch.setitem(sys.modules, "krepis.router", router_mod)
    monkeypatch.setitem(sys.modules, "krepis.llm", llm_mod)
    monkeypatch.setitem(sys.modules, "krepis.cost", cost_mod)

    return llm_client_cls, cost_mod.record_llm_call


def _gen(**kw):
    defaults = dict(provider="router", model_group="med")
    defaults.update(kw)
    return FixGenerator(**defaults)


def _generate(gen, diff_text="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"):
    return gen.generate(
        category="CODE",
        root_cause="off-by-one",
        confidence=0.9,
        remediation="fix it",
        affected_files=["x.py"],
        file_contents={"x.py": "a\n"},
        test_contents={},
    )


def test_fix_generator_router_happy_path(monkeypatch):
    diff_text = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    gen = _gen()
    resolve = MagicMock(return_value=(_edge_spec(), _route()))
    client_cls, record_cost = _install_fake_krepis(
        monkeypatch,
        resolve_group_spec=resolve,
        complete_return=_fake_llm_result(diff_text),
        cost_return={"cost_usd": 0.0021, "cost_source": "price_card"},
    )

    result = _generate(gen, diff_text)

    # generate() strips the transport's raw text (trailing newline included).
    assert result == diff_text.strip()
    # Resolved with the openai wire and exec_context from the environment,
    # not inferred.
    _, kwargs = resolve.call_args
    assert kwargs["wire"] == "openai"
    # LLMClient constructed with the resolved spec and a distinct callsite_id
    # from the diagnosis path.
    client_args, client_kwargs = client_cls.call_args
    assert client_args[0].provider == "litellm_edge"
    # Kebab-case, matching the LLM_CALLSITE_REGISTRY row id — on BOTH the
    # client and the cost record (alpha-engine-config-I12312).
    assert client_kwargs["callsite_id"] == "flow-doctor-fix-generation"
    record_cost.assert_called_once()
    assert record_cost.call_args.kwargs["extra_fields"] == {"callsite_id": "flow-doctor-fix-generation"}


def test_fix_generator_router_refuses_non_compelled_route(monkeypatch):
    """A route krepis resolved to a direct provider (its own fallback, not
    the router edge or its registry-derived degraded route) must be refused
    outright — same rule as the diagnosis-side RouterProvider.
    """
    gen = _gen(model_group="high")
    resolve = MagicMock(return_value=(
        _edge_spec(provider="openrouter", model="deepseek/deepseek-v4"),
        _route(route="openrouter", provider="openrouter"),
    ))
    _install_fake_krepis(monkeypatch, resolve_group_spec=resolve)

    with pytest.raises(RouterUnresolvable, match="not a compelled path"):
        _generate(gen)


def test_fix_generator_router_fails_closed_on_resolution_error(monkeypatch):
    """krepis.router raising (unresolvable group, exec_context not covered,
    etc.) must surface as RouterUnresolvable, never a silent fallback.
    """
    gen = _gen(model_group="ultra")
    resolve = MagicMock(side_effect=RuntimeError("no reachable entry for context"))
    _install_fake_krepis(monkeypatch, resolve_group_spec=resolve)

    with pytest.raises(RouterUnresolvable, match="did not resolve"):
        _generate(gen)


def test_fix_generator_router_fails_closed_when_krepis_missing(monkeypatch):
    """`import krepis.router` genuinely failing (package not installed) must
    raise RouterUnresolvable, not proceed with a default endpoint or ambient
    key. Runs against whatever `krepis` state THIS environment actually has.
    """
    monkeypatch.delitem(sys.modules, "krepis", raising=False)
    monkeypatch.delitem(sys.modules, "krepis.router", raising=False)
    gen = _gen()

    if "krepis" in sys.modules or _real_krepis_importable():
        pytest.skip("krepis is actually installed in this environment")

    with pytest.raises(RouterUnresolvable, match="krepis is not installed"):
        _generate(gen)


def _real_krepis_importable() -> bool:
    import importlib

    try:
        importlib.import_module("krepis.router")
    except ImportError:
        return False
    return True


def test_fix_generator_router_degraded_route_still_serves(monkeypatch):
    """`egress_proxy` (the registry-derived direct route a consumer degrades
    to when the router edge's health probe fails) is still a compelled path
    — it must serve, not raise.
    """
    diff_text = "--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-old\n+new\n"
    gen = _gen()
    resolve = MagicMock(return_value=(
        _edge_spec(provider="deepseek", model="deepseek-v4-pro-max"),
        _route(route="egress_proxy", provider="deepseek"),
    ))
    _install_fake_krepis(
        monkeypatch,
        resolve_group_spec=resolve,
        complete_return=_fake_llm_result(diff_text, model="deepseek-v4-pro-max"),
        cost_return={"cost_usd": 0.0009, "cost_source": "price_card"},
    )

    result = _generate(gen, diff_text)

    # generate() strips the transport's raw text (trailing newline included).
    assert result == diff_text.strip()


def test_fix_generator_router_requires_model_group():
    with pytest.raises(ValueError, match="model_group"):
        FixGenerator(provider="router")


def test_fix_generator_provider_no_longer_accepts_anthropic():
    with pytest.raises(ValueError, match="removed in 0.15.0"):
        FixGenerator(api_key="k", provider="anthropic")

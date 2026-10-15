"""Tests for RouterProvider (diagnosis.provider: router) — krepis-router
integration, fail-closed behaviour, and the compelled-route refusal.

krepis is an OPTIONAL dependency (`pip install flow-doctor[router]`) and is
deliberately NOT part of the default CI install (`.[dev,diagnosis]`) — same
reasoning `test_diagnosis_provider.py`'s `_install_fake_openai` documents for
the `openai` package. These tests inject fake `krepis`/`krepis.router`/
`krepis.llm`/`krepis.cost` modules so RouterProvider's own wiring — which
route it accepts, how it fails closed, how it prices a call — is exercised
without requiring the real package, and the genuine-ImportError test below
runs against whatever `krepis` state the environment actually has (absent in
CI, so it covers the real "not installed" path there).
"""

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from flow_doctor.diagnosis.context import ContextAssembler
from flow_doctor.diagnosis.provider import RouterProvider, RouterUnresolvable
from tests.test_diagnosis_provider import _make_context  # noqa: E402


def _edge_spec(provider="litellm_edge", model="med-deepseek-v4-pro"):
    return SimpleNamespace(provider=provider, model=model)


def _route(route="litellm_proxy", provider="litellm_edge"):
    return {"route": route, "provider": provider, "deployment_id": "d1"}


def _fake_llm_result(text, model="med-deepseek-v4-pro", input_tokens=1000, output_tokens=500):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
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

    Mirrors ``_install_fake_openai`` in ``test_diagnosis_provider.py`` — the
    real ``krepis`` package need not be installed for these tests to run
    (and, per the CI install list, generally isn't). Returns the fake
    ``LLMClient`` class's mock instance for callers that want to assert on
    the call it received.
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


def test_router_provider_happy_path(monkeypatch):
    provider = RouterProvider(model_group="med", confidence_calibration=1.0)
    result_json = json.dumps({
        "category": "CODE",
        "root_cause": "off-by-one",
        "confidence": 0.9,
    })
    resolve = MagicMock(return_value=(_edge_spec(), _route()))
    client_cls, record_cost = _install_fake_krepis(
        monkeypatch,
        resolve_group_spec=resolve,
        complete_return=_fake_llm_result(result_json),
        cost_return={"cost_usd": 0.0021, "cost_source": "price_card"},
    )

    diagnosis = provider.diagnose(_make_context(), ContextAssembler())

    assert diagnosis.category == "CODE"
    assert diagnosis.root_cause == "off-by-one"
    assert diagnosis.confidence == 0.9
    assert diagnosis.cost_usd == 0.0021
    assert diagnosis.llm_model == "med-deepseek-v4-pro"
    assert diagnosis.tokens_used == 1500

    # Resolved with the openai wire (the router edge speaks OpenAI-compatible
    # chat completions) and exec_context read from the environment, not
    # inferred.
    _, kwargs = resolve.call_args
    assert kwargs["wire"] == "openai"

    # LLMClient constructed with the resolved spec and a callsite_id.
    client_args, client_kwargs = client_cls.call_args
    assert client_args[0].provider == "litellm_edge"
    # Kebab-case, matching the LLM_CALLSITE_REGISTRY row id — on BOTH the
    # client and the cost record (alpha-engine-config-I12312).
    assert client_kwargs["callsite_id"] == "flow-doctor-diagnosis"
    record_cost.assert_called_once()
    assert record_cost.call_args.kwargs["extra_fields"] == {"callsite_id": "flow-doctor-diagnosis"}


def test_router_provider_refuses_non_compelled_route(monkeypatch):
    """A route krepis resolved to a direct provider (its own fallback, not
    the router edge or its registry-derived degraded route) must be refused
    outright — alpha-engine-config-I6367 forbids direct-OpenRouter linkage,
    and the 2026-07-17 ruling sets the direct-Anthropic budget to $0.
    """
    provider = RouterProvider(model_group="high", confidence_calibration=1.0)
    resolve = MagicMock(return_value=(
        _edge_spec(provider="openrouter", model="deepseek/deepseek-v4"),
        _route(route="openrouter", provider="openrouter"),
    ))
    _install_fake_krepis(monkeypatch, resolve_group_spec=resolve)

    with pytest.raises(RouterUnresolvable, match="not a compelled path"):
        provider.diagnose(_make_context(), ContextAssembler())


def test_router_provider_fails_closed_on_resolution_error(monkeypatch):
    """krepis.router raising (unresolvable group, exec_context not covered,
    etc.) must surface as RouterUnresolvable, never a silent fallback or a
    swallowed exception — model-router-policy R20.
    """
    provider = RouterProvider(model_group="ultra", confidence_calibration=1.0)
    resolve = MagicMock(side_effect=RuntimeError("no reachable entry for context"))
    _install_fake_krepis(monkeypatch, resolve_group_spec=resolve)

    with pytest.raises(RouterUnresolvable, match="did not resolve"):
        provider.diagnose(_make_context(), ContextAssembler())


def test_router_provider_fails_closed_when_krepis_missing(monkeypatch):
    """`import krepis.router` genuinely failing (package not installed) must
    raise RouterUnresolvable, not proceed with a default endpoint or ambient
    key. Runs against whatever `krepis` state THIS environment actually has
    — since the default CI install (`.[dev,diagnosis]`) excludes krepis
    entirely, this exercises the real absent-package path there.
    """
    monkeypatch.delitem(sys.modules, "krepis", raising=False)
    monkeypatch.delitem(sys.modules, "krepis.router", raising=False)
    provider = RouterProvider(model_group="med", confidence_calibration=1.0)

    if "krepis" in sys.modules or _real_krepis_importable():
        pytest.skip("krepis is actually installed in this environment")

    with pytest.raises(RouterUnresolvable, match="krepis is not installed"):
        provider.diagnose(_make_context(), ContextAssembler())


def _real_krepis_importable() -> bool:
    import importlib

    try:
        importlib.import_module("krepis.router")
    except ImportError:
        return False
    return True


def test_router_provider_degraded_route_still_serves(monkeypatch):
    """`egress_proxy` (the registry-derived direct route a consumer degrades
    to when the router edge's health probe fails) is still a compelled path
    per model-router-policy §5 — it must serve, not raise.
    """
    provider = RouterProvider(model_group="med", confidence_calibration=1.0)
    result_json = json.dumps({"category": "INFRA", "root_cause": "oom", "confidence": 0.6})
    resolve = MagicMock(return_value=(
        _edge_spec(provider="deepseek", model="deepseek-v4-pro-max"),
        _route(route="egress_proxy", provider="deepseek"),
    ))
    _install_fake_krepis(
        monkeypatch,
        resolve_group_spec=resolve,
        complete_return=_fake_llm_result(result_json, model="deepseek-v4-pro-max"),
        cost_return={"cost_usd": 0.0009, "cost_source": "price_card"},
    )

    diagnosis = provider.diagnose(_make_context(), ContextAssembler())

    assert diagnosis.category == "INFRA"
    assert diagnosis.cost_usd == 0.0009


def test_router_provider_missing_reported_cost_falls_back_to_price_card(monkeypatch):
    """When the router edge's response carries no usage.cost,
    krepis.cost.record_llm_call recomputes from the registry price card
    rather than flow-doctor guessing a fallback rate — the router can
    resolve to any of several models, so no single static price is knowable
    ahead of the call.
    """
    provider = RouterProvider(model_group="med", confidence_calibration=1.0)
    result_json = json.dumps({"category": "CODE", "root_cause": "x", "confidence": 0.5})
    resolve = MagicMock(return_value=(_edge_spec(), _route()))
    _install_fake_krepis(
        monkeypatch,
        resolve_group_spec=resolve,
        complete_return=_fake_llm_result(result_json),
        cost_return={"cost_usd": 0.0033, "cost_source": "price_card"},
    )

    diagnosis = provider.diagnose(_make_context(), ContextAssembler())

    assert diagnosis.cost_usd == 0.0033

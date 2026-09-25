"""Tests for the diagnosis providers (with mocked LLM transports).

``AnthropicProvider`` was DELETED in 0.15.0 — Brian ruling: flow-doctor must
not depend on the ``anthropic`` distribution or construct a direct-Anthropic
client anywhere (the fleet's Anthropic account carries a $0 budget,
alpha-engine-config-I7460, and every deployment is now a krepis consumer).
The surviving diagnosis transports are ``OpenAICompatProvider`` (any
OpenAI-compatible endpoint — the provider-neutral path external/self-hosted
users take) and ``RouterProvider`` (krepis capability class — see
``tests/test_router_provider.py``). The JSON-extraction helper that used to
live on ``AnthropicProvider._parse_json`` is now the module-level
``_parse_llm_json_response``, shared by both surviving transports.
"""

import json
from unittest.mock import MagicMock

from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext
from flow_doctor.diagnosis.provider import _parse_llm_json_response


def _make_context(**kwargs):
    defaults = dict(
        error_type="ValueError",
        error_message="invalid literal",
        traceback="Traceback...\nValueError: invalid literal",
        flow_name="test-flow",
    )
    defaults.update(kwargs)
    return DiagnosisContext(**defaults)


# --- _parse_llm_json_response (shared JSON-extraction helper) ──────────────


def test_parse_json_from_code_fence():
    data = {"category": "TRANSIENT", "root_cause": "timeout", "confidence": 0.7}
    text = f"Here's my analysis:\n```json\n{json.dumps(data)}\n```"
    result = _parse_llm_json_response(text)
    assert result["category"] == "TRANSIENT"


def test_parse_json_from_braces():
    data = {"category": "INFRA", "root_cause": "OOM", "confidence": 0.8}
    text = f"The diagnosis is: {json.dumps(data)} and that's it."
    result = _parse_llm_json_response(text)
    assert result["category"] == "INFRA"


def test_parse_json_fallback():
    result = _parse_llm_json_response("This is not JSON at all")
    assert result["category"] == "CODE"
    assert result["confidence"] == 0.3


# --- OpenAICompatProvider ---


from flow_doctor.diagnosis.provider import OpenAICompatProvider  # noqa: E402


def _mock_openai_response(content_text, prompt=1000, completion=500, cost=None):
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.cost = cost
    message = MagicMock()
    message.content = content_text
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def _openai_provider(**kw):
    defaults = dict(
        api_key="k",
        model="moonshotai/kimi-k2.6",
        base_url="https://openrouter.ai/api/v1",
        confidence_calibration=1.0,
    )
    defaults.update(kw)
    return OpenAICompatProvider(**defaults)


def test_openai_compat_requires_prices_off_openrouter():
    import pytest

    with pytest.raises(ValueError, match="price_in_per_1m"):
        _openai_provider(base_url="http://10.0.0.12:8000/v1")
    # ...but constructs fine WITH prices
    _openai_provider(base_url="http://10.0.0.12:8000/v1",
                     price_in_per_1m=0.1, price_out_per_1m=0.2)


def _install_fake_openai(monkeypatch, resp):
    """Inject a fake `openai` module (the SDK is an optional extra — tests
    can't rely on it being installed). Returns the mock client."""
    import sys as _sys
    import types as _types

    client = MagicMock()
    client.chat.completions.create.return_value = resp
    fake = _types.ModuleType("openai")
    fake.OpenAI = lambda *a, **kw: client
    monkeypatch.setitem(_sys.modules, "openai", fake)
    return client


def test_openai_compat_diagnose_uses_reported_cost(monkeypatch):
    provider = _openai_provider()
    resp = _mock_openai_response(
        json.dumps({"category": "DATA", "root_cause": "stale cache",
                    "confidence": 0.8}),
        cost=0.00042,
    )
    client = _install_fake_openai(monkeypatch, resp)
    d = provider.diagnose(_make_context(), ContextAssembler())
    kwargs = client.chat.completions.create.call_args.kwargs

    assert d.category == "DATA"
    assert d.root_cause == "stale cache"
    assert d.cost_usd == 0.00042  # provider-reported, not token math
    assert d.llm_model == "moonshotai/kimi-k2.6"
    assert d.tokens_used == 1500
    # openrouter base_url opts into usage accounting
    assert kwargs["extra_body"] == {"usage": {"include": True}}
    assert kwargs["messages"][0]["role"] == "system"


def test_openai_compat_diagnose_configured_prices_when_no_reported_cost(monkeypatch):
    provider = _openai_provider(
        base_url="http://vllm.internal:8000/v1",
        price_in_per_1m=1.0, price_out_per_1m=2.0,
    )
    resp = _mock_openai_response(
        json.dumps({"category": "CODE", "root_cause": "x", "confidence": 0.5}),
        prompt=1_000_000, completion=500_000, cost=None,
    )
    client = _install_fake_openai(monkeypatch, resp)
    d = provider.diagnose(_make_context(), ContextAssembler())
    kwargs = client.chat.completions.create.call_args.kwargs

    assert d.cost_usd == 2.0  # 1M @ $1 + 0.5M @ $2
    assert "extra_body" not in kwargs  # non-openrouter: no OpenRouter opt-in


def test_openai_compat_prices_high_when_openrouter_omits_cost(monkeypatch, capsys):
    # #85 deleted _FALLBACK_PRICES_PER_1M but left this branch reading it, so
    # an OpenRouter response without usage.cost raised NameError.
    from flow_doctor.diagnosis.provider import _FALLBACK_PRICES_PER_1M

    provider = _openai_provider()
    resp = _mock_openai_response(
        json.dumps({"category": "DATA", "root_cause": "x", "confidence": 0.5}),
        prompt=1_000_000, completion=1_000_000, cost=None,
    )
    _install_fake_openai(monkeypatch, resp)
    d = provider.diagnose(_make_context(), ContextAssembler())

    assert d.category == "DATA"
    assert d.cost_usd == sum(_FALLBACK_PRICES_PER_1M)  # 1M in + 1M out
    assert "carried no usage.cost" in capsys.readouterr().err


def test_openai_compat_fenced_json_parses(monkeypatch):
    provider = _openai_provider()
    resp = _mock_openai_response(
        '```json\n{"category": "CONFIG", "root_cause": "bad flag", '
        '"confidence": 0.6}\n```',
        cost=0.0001,
    )
    _install_fake_openai(monkeypatch, resp)
    d = provider.diagnose(_make_context(), ContextAssembler())
    assert d.category == "CONFIG"
    assert d.root_cause == "bad flag"


def test_invalid_category_normalized(monkeypatch):
    provider = _openai_provider()
    resp = _mock_openai_response(
        json.dumps({"category": "UNKNOWN_CATEGORY", "root_cause": "something",
                    "confidence": 0.5}),
        cost=0.0001,
    )
    _install_fake_openai(monkeypatch, resp)
    d = provider.diagnose(_make_context(), ContextAssembler())
    assert d.category == "CODE"  # Falls back to CODE


# ── SFT capture (config#1541) ────────────────────────────────────────────────
# The diagnosis task is the third SFT producer surface (Vires coach +
# morning-signal are already wired). Capture is gated on the fleet env switch
# and is pure telemetry — it must never alter or break a diagnosis. Exercised
# here over OpenAICompatProvider (AnthropicProvider, the original SFT
# call-site, was deleted in 0.15.0).

_CAPTURE_ENV = "LLM_SFT_CAPTURE_ENABLED"


def _run_openai_compat_diagnose(monkeypatch, sink_path):
    provider = _openai_provider(sft_sink_path=str(sink_path))
    resp = _mock_openai_response(
        json.dumps({"category": "CODE", "root_cause": "boom", "confidence": 0.9}),
        cost=0.0002,
    )
    _install_fake_openai(monkeypatch, resp)
    return provider.diagnose(_make_context(), ContextAssembler())


def test_sft_capture_writes_record_when_enabled(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("krepis")  # record-building needs the (optional) SFT dep
    monkeypatch.setenv(_CAPTURE_ENV, "1")
    sink = tmp_path / "_sft_raw" / "flow_doctor_diagnosis.sft.jsonl"

    diagnosis = _run_openai_compat_diagnose(monkeypatch, sink)

    # Diagnosis still returned unchanged.
    assert diagnosis.category == "CODE"
    # Exactly one canonical SFT record landed with the distinct producer tag.
    assert sink.exists()
    lines = sink.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["producer"] == "flow_doctor_diagnosis"
    assert rec["model"]  # provider model recorded
    # The COMPLETE input (system + user) is normalized into input_messages.
    roles = [m["role"] for m in rec["input_messages"]]
    assert roles[0] == "system" and "user" in roles
    assert rec["usage"]["input_tokens"] == 1000
    assert rec["usage"]["output_tokens"] == 500
    assert rec["provenance"]["source"] == "live"


def test_sft_capture_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(_CAPTURE_ENV, raising=False)
    monkeypatch.delenv("ALPHA_ENGINE_DECISION_CAPTURE_ENABLED", raising=False)
    sink = tmp_path / "_sft_raw" / "flow_doctor_diagnosis.sft.jsonl"

    diagnosis = _run_openai_compat_diagnose(monkeypatch, sink)

    assert diagnosis.category == "CODE"
    assert not sink.exists()  # capture is off → nothing written


def test_sft_capture_failure_never_breaks_diagnosis(tmp_path, monkeypatch):
    monkeypatch.setenv(_CAPTURE_ENV, "1")
    # A directory where the sink filename already exists as a DIRECTORY forces a
    # write failure inside capture; the diagnosis must still return cleanly.
    bad = tmp_path / "sink.jsonl"
    bad.mkdir()

    diagnosis = _run_openai_compat_diagnose(monkeypatch, bad)

    assert diagnosis.category == "CODE"

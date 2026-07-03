"""Tests for the diagnosis provider (with mocked Anthropic API)."""

import json
from unittest.mock import MagicMock, patch

from flow_doctor.core.models import Diagnosis
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext
from flow_doctor.diagnosis.provider import AnthropicProvider


def _make_context(**kwargs):
    defaults = dict(
        error_type="ValueError",
        error_message="invalid literal",
        traceback="Traceback...\nValueError: invalid literal",
        flow_name="test-flow",
    )
    defaults.update(kwargs)
    return DiagnosisContext(**defaults)


def _mock_response(content_text, input_tokens=1000, output_tokens=500):
    """Create a mock Anthropic API response."""
    block = MagicMock()
    block.type = "text"
    block.text = content_text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def test_diagnose_parses_json():
    provider = AnthropicProvider(api_key="test-key", confidence_calibration=1.0)
    ctx = _make_context()
    assembler = ContextAssembler()

    response_json = json.dumps({
        "category": "CODE",
        "root_cause": "Invalid type conversion in parser",
        "affected_files": ["parser.py:42"],
        "confidence": 0.90,
        "remediation": "Fix the type conversion",
        "auto_fixable": True,
        "alternative_hypotheses": ["Could be input data issue"],
        "reasoning": "The traceback points to a ValueError in the parser",
    })

    mock_resp = _mock_response(response_json)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = provider.diagnose(ctx, assembler)

    assert isinstance(result, Diagnosis)
    assert result.category == "CODE"
    assert result.root_cause == "Invalid type conversion in parser"
    assert result.confidence == 0.90
    assert result.affected_files == ["parser.py:42"]
    assert result.remediation == "Fix the type conversion"
    assert result.auto_fixable is True
    assert result.alternative_hypotheses == ["Could be input data issue"]
    assert result.source == "llm"
    assert result.tokens_used == 1500
    assert result.cost_usd is not None


def test_confidence_calibration():
    provider = AnthropicProvider(api_key="test-key", confidence_calibration=0.85)
    ctx = _make_context()
    assembler = ContextAssembler()

    response_json = json.dumps({
        "category": "DATA",
        "root_cause": "Missing input file",
        "confidence": 1.0,
    })

    mock_resp = _mock_response(response_json)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = provider.diagnose(ctx, assembler)

    assert result.confidence == 0.85  # 1.0 * 0.85


def test_parse_json_from_code_fence():
    data = {"category": "TRANSIENT", "root_cause": "timeout", "confidence": 0.7}
    text = f"Here's my analysis:\n```json\n{json.dumps(data)}\n```"
    result = AnthropicProvider._parse_json(text)
    assert result["category"] == "TRANSIENT"


def test_parse_json_from_braces():
    data = {"category": "INFRA", "root_cause": "OOM", "confidence": 0.8}
    text = f"The diagnosis is: {json.dumps(data)} and that's it."
    result = AnthropicProvider._parse_json(text)
    assert result["category"] == "INFRA"


def test_parse_json_fallback():
    result = AnthropicProvider._parse_json("This is not JSON at all")
    assert result["category"] == "CODE"
    assert result["confidence"] == 0.3


def test_invalid_category_normalized():
    provider = AnthropicProvider(api_key="test-key", confidence_calibration=1.0)
    ctx = _make_context()
    assembler = ContextAssembler()

    response_json = json.dumps({
        "category": "UNKNOWN_CATEGORY",
        "root_cause": "something",
        "confidence": 0.5,
    })

    mock_resp = _mock_response(response_json)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = provider.diagnose(ctx, assembler)

    assert result.category == "CODE"  # Falls back to CODE


def test_cost_calculation():
    provider = AnthropicProvider(api_key="test-key", confidence_calibration=1.0)
    ctx = _make_context()
    assembler = ContextAssembler()

    response_json = json.dumps({
        "category": "CODE",
        "root_cause": "bug",
        "confidence": 0.9,
    })

    # 10K input, 1K output
    mock_resp = _mock_response(response_json, input_tokens=10000, output_tokens=1000)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = provider.diagnose(ctx, assembler)

    # $3/M input + $15/M output = 10000*3/1M + 1000*15/1M = 0.03 + 0.015 = 0.045
    assert abs(result.cost_usd - 0.045) < 0.001
    assert result.tokens_used == 11000


# --- per-model Anthropic pricing (bug fix: everything was priced at Sonnet) ---


def _diagnose_with_model(model, input_tokens=1_000_000, output_tokens=0):
    provider = AnthropicProvider(api_key="k", model=model, confidence_calibration=1.0)
    resp = _mock_response(json.dumps({"category": "CODE", "root_cause": "x",
                                      "confidence": 0.5}),
                          input_tokens=input_tokens, output_tokens=output_tokens)
    with patch("anthropic.Anthropic") as mock_cls:
        client = MagicMock()
        client.messages.create.return_value = resp
        mock_cls.return_value = client
        return provider.diagnose(_make_context(), ContextAssembler())


def test_haiku_priced_at_haiku_rates_not_sonnet():
    d = _diagnose_with_model("claude-haiku-4-5")
    assert d.cost_usd == 1.0  # 1M input @ $1/M — the old code charged $3


def test_sonnet_priced_at_sonnet_rates():
    d = _diagnose_with_model("claude-sonnet-4-6")
    assert d.cost_usd == 3.0


def test_opus_priced_at_opus_rates():
    d = _diagnose_with_model("claude-opus-4-7")
    assert d.cost_usd == 5.0


def test_unknown_model_priced_conservative(capsys):
    d = _diagnose_with_model("claude-future-9")
    assert d.cost_usd == 5.0  # Opus fallback: the daily cap errs safe
    assert "no price entry" in capsys.readouterr().err


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

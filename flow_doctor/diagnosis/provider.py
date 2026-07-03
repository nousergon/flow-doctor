"""Diagnosis providers: ABC + Anthropic and OpenAI-compatible implementations."""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from typing import Optional

from flow_doctor.core.constants import DEFAULT_DIAGNOSIS_MODEL
from flow_doctor.core.models import Diagnosis
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext


class DiagnosisProvider(ABC):
    """Abstract interface for LLM diagnosis providers."""

    @abstractmethod
    def diagnose(self, context: DiagnosisContext, assembler: ContextAssembler) -> Diagnosis:
        """Run diagnosis on the given context. Returns a Diagnosis object."""


# Valid categories for classification
_VALID_CATEGORIES = {"TRANSIENT", "DATA", "CODE", "CONFIG", "EXTERNAL", "INFRA"}

# Anthropic per-1M-token USD rates, keyed by model-name prefix (first match
# wins). Fixes the pre-2026-07 bug where EVERY model was priced at Sonnet
# rates ($3/$15) — the diagnosis cost feeds the max_daily_cost_usd cap in
# core/client.py, so a Haiku deployment hit its cap 3x early and an Opus one
# 1.7x late.
_ANTHROPIC_PRICES_PER_1M = (
    ("claude-opus", (5.0, 25.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku", (1.0, 5.0)),
)
_FALLBACK_PRICES_PER_1M = (5.0, 25.0)  # unknown model: price HIGH so the daily cap errs safe


def _anthropic_prices(model: str) -> "tuple[float, float]":
    for prefix, prices in _ANTHROPIC_PRICES_PER_1M:
        if model.startswith(prefix):
            return prices
    print(
        f"[flow-doctor] WARNING: no price entry for model '{model}'; "
        f"pricing at Opus rates so the daily cost cap errs conservative",
        file=sys.stderr,
    )
    return _FALLBACK_PRICES_PER_1M


class AnthropicProvider(DiagnosisProvider):
    """Diagnosis provider using Anthropic Claude API."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_DIAGNOSIS_MODEL,
        confidence_calibration: float = 0.85,
        timeout_seconds: int = 30,
    ):
        self.api_key = api_key
        self.model = model
        self.confidence_calibration = confidence_calibration
        self.timeout_seconds = timeout_seconds

    def diagnose(self, context: DiagnosisContext, assembler: ContextAssembler) -> Diagnosis:
        """Call Claude API and parse the structured diagnosis response."""
        import anthropic

        client = anthropic.Anthropic(
            api_key=self.api_key,
            timeout=self.timeout_seconds,
        )

        user_prompt = assembler.build_prompt(context)

        response = client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=assembler.system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Extract text content
        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        # Parse JSON from response
        parsed = self._parse_json(text)

        # Cost from the CONFIGURED model's rates — this figure feeds the
        # max_daily_cost_usd cap, so it must track the actual model.
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        total_tokens = input_tokens + output_tokens
        price_in, price_out = _anthropic_prices(self.model)
        cost = (input_tokens * price_in / 1_000_000) + (output_tokens * price_out / 1_000_000)

        return _build_diagnosis(
            parsed, text, context,
            model=self.model,
            tokens_used=total_tokens,
            cost_usd=cost,
            confidence_calibration=self.confidence_calibration,
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Extract and parse JSON from LLM response text."""
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON block in markdown code fence
        for marker in ("```json", "```"):
            if marker in text:
                start = text.index(marker) + len(marker)
                end = text.index("```", start) if "```" in text[start:] else len(text)
                try:
                    return json.loads(text[start:end].strip())
                except json.JSONDecodeError:
                    pass

        # Try to find JSON object boundaries
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        # Fallback: return minimal dict
        print(f"[flow-doctor] WARNING: Could not parse LLM response as JSON", file=sys.stderr)
        return {"root_cause": text[:500], "category": "CODE", "confidence": 0.3}


def _build_diagnosis(
    parsed: dict,
    text: str,
    context: DiagnosisContext,
    *,
    model: str,
    tokens_used: int,
    cost_usd: float,
    confidence_calibration: float,
) -> Diagnosis:
    """Shared parsed-response → Diagnosis mapping (both providers emit the
    same JSON contract; only the transport differs)."""
    raw_confidence = float(parsed.get("confidence", 0.5))
    calibrated_confidence = raw_confidence * confidence_calibration

    category = parsed.get("category", "CODE").upper()
    if category not in _VALID_CATEGORIES:
        category = "CODE"

    return Diagnosis(
        report_id="",  # Will be set by caller
        flow_name=context.flow_name,
        category=category,
        root_cause=parsed.get("root_cause", text[:500]),
        confidence=calibrated_confidence,
        affected_files=parsed.get("affected_files"),
        remediation=parsed.get("remediation"),
        auto_fixable=parsed.get("auto_fixable", False),
        reasoning=parsed.get("reasoning"),
        alternative_hypotheses=parsed.get("alternative_hypotheses"),
        source="llm",
        llm_model=model,
        tokens_used=tokens_used,
        cost_usd=round(cost_usd, 6),
    )


class OpenAICompatProvider(DiagnosisProvider):
    """Diagnosis provider for any OpenAI-compatible chat-completions endpoint
    (OpenRouter for open-weight models, OpenAI itself, self-hosted vLLM).

    Same JSON diagnosis contract as :class:`AnthropicProvider` — the system
    prompt and response parsing are shared; only the transport differs.

    Cost accounting (feeds the ``max_daily_cost_usd`` cap, so it must never
    silently under-count): OpenRouter reports the actually-billed USD cost in
    ``usage.cost`` when the request opts in (this provider always opts in on
    an openrouter base_url). For OTHER endpoints, ``price_in_per_1m`` /
    ``price_out_per_1m`` are REQUIRED — construction fails loud rather than
    letting an unpriced provider bill under a blind cap.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        confidence_calibration: float = 0.85,
        timeout_seconds: int = 30,
        price_in_per_1m: Optional[float] = None,
        price_out_per_1m: Optional[float] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.confidence_calibration = confidence_calibration
        self.timeout_seconds = timeout_seconds
        self.price_in_per_1m = price_in_per_1m
        self.price_out_per_1m = price_out_per_1m
        if not self._is_openrouter() and (price_in_per_1m is None or price_out_per_1m is None):
            raise ValueError(
                "OpenAICompatProvider: price_in_per_1m + price_out_per_1m are "
                "required for non-OpenRouter endpoints (OpenRouter reports its "
                "own billed cost; other endpoints don't, and an unpriced "
                "provider would bill under a blind max_daily_cost_usd cap)."
            )

    def _is_openrouter(self) -> bool:
        return "openrouter.ai" in (self.base_url or "")

    def diagnose(self, context: DiagnosisContext, assembler: ContextAssembler) -> Diagnosis:
        """Call the OpenAI-compatible endpoint and parse the diagnosis JSON."""
        from openai import OpenAI

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

        kwargs = {}
        if self._is_openrouter():
            # Opt into usage accounting: usage.cost is the actually-billed
            # USD figure (canonical under :floor routing).
            kwargs["extra_body"] = {"usage": {"include": True}}

        response = client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": assembler.system_prompt},
                {"role": "user", "content": assembler.build_prompt(context)},
            ],
            **kwargs,
        )

        text = (response.choices[0].message.content or "").strip()
        parsed = AnthropicProvider._parse_json(text)

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        reported_cost = getattr(usage, "cost", None)
        if reported_cost is not None:
            cost = float(reported_cost)
        elif self.price_in_per_1m is not None and self.price_out_per_1m is not None:
            cost = (
                input_tokens * self.price_in_per_1m / 1_000_000
                + output_tokens * self.price_out_per_1m / 1_000_000
            )
        else:
            # OpenRouter response missing usage.cost (shouldn't happen with the
            # opt-in) — err HIGH rather than free so the daily cap stays honest.
            print(
                "[flow-doctor] WARNING: OpenRouter response carried no usage.cost; "
                "pricing at Opus rates so the daily cost cap errs conservative",
                file=sys.stderr,
            )
            fallback_in, fallback_out = _FALLBACK_PRICES_PER_1M
            cost = (
                input_tokens * fallback_in / 1_000_000
                + output_tokens * fallback_out / 1_000_000
            )

        return _build_diagnosis(
            parsed, text, context,
            model=self.model,
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost,
            confidence_calibration=self.confidence_calibration,
        )

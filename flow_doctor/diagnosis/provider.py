"""Diagnosis providers: ABC + Anthropic implementation."""

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

        # Calculate cost (approximate: Sonnet pricing)
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        total_tokens = input_tokens + output_tokens
        # Sonnet: $3/M input, $15/M output
        cost = (input_tokens * 3.0 / 1_000_000) + (output_tokens * 15.0 / 1_000_000)

        # Apply confidence calibration
        raw_confidence = float(parsed.get("confidence", 0.5))
        calibrated_confidence = raw_confidence * self.confidence_calibration

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
            llm_model=self.model,
            tokens_used=total_tokens,
            cost_usd=round(cost, 6),
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

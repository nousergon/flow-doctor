"""Diagnosis provider using Claude Agent SDK for deep multi-turn analysis.

Uses the Agent SDK's query() to spawn an agent with tool access (Read, Grep,
Glob, Bash) that can investigate failures by reading source code, checking
logs, and inspecting infrastructure state. Falls back to AnthropicProvider
when the Agent SDK is not available.

Requires: pip install claude-agent-sdk
Best used on EC2 where full repo checkouts exist.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

from flow_doctor.core.constants import DEFAULT_DIAGNOSIS_MODEL
from flow_doctor.core.models import Diagnosis
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext
from flow_doctor.diagnosis.provider import DiagnosisProvider, _VALID_CATEGORIES


class AgentSDKProvider(DiagnosisProvider):
    """Diagnosis provider using Claude Agent SDK for multi-turn investigation."""

    def __init__(
        self,
        cwd: Optional[str] = None,
        max_turns: int = 10,
        max_budget_usd: float = 0.20,
        model: str = DEFAULT_DIAGNOSIS_MODEL,
        confidence_calibration: float = 0.85,
    ):
        self.cwd = cwd
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.model = model
        self.confidence_calibration = confidence_calibration

    def diagnose(self, context: DiagnosisContext, assembler: ContextAssembler) -> Diagnosis:
        """Run Agent SDK diagnosis synchronously via anyio."""
        import anyio
        return anyio.from_thread.run(self._diagnose_async, context, assembler)

    async def _diagnose_async(
        self, context: DiagnosisContext, assembler: ContextAssembler,
    ) -> Diagnosis:
        """Async implementation using Agent SDK query()."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage

        prompt = self._build_agent_prompt(context, assembler)

        total_input_tokens = 0
        total_output_tokens = 0
        result_text = ""

        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=self.cwd,
                allowed_tools=["Read", "Glob", "Grep", "Bash"],
                model=self.model,
                max_turns=self.max_turns,
                max_budget_usd=self.max_budget_usd,
                system_prompt=assembler.system_prompt,
            ),
        ):
            if isinstance(message, ResultMessage):
                result_text = message.result or ""
            elif isinstance(message, AssistantMessage) and message.usage:
                total_input_tokens += message.usage.get("input_tokens", 0)
                total_output_tokens += message.usage.get("output_tokens", 0)

        # Parse structured JSON from agent result
        parsed = self._parse_result(result_text)

        # Cost estimate (Sonnet pricing: $3/M input, $15/M output)
        cost = (total_input_tokens * 3.0 / 1_000_000) + (total_output_tokens * 15.0 / 1_000_000)
        total_tokens = total_input_tokens + total_output_tokens

        raw_confidence = float(parsed.get("confidence", 0.5))
        calibrated_confidence = raw_confidence * self.confidence_calibration

        category = parsed.get("category", "CODE").upper()
        if category not in _VALID_CATEGORIES:
            category = "CODE"

        return Diagnosis(
            report_id="",  # Set by caller
            flow_name=context.flow_name,
            category=category,
            root_cause=parsed.get("root_cause", result_text[:500]),
            confidence=calibrated_confidence,
            affected_files=parsed.get("affected_files"),
            remediation=parsed.get("remediation"),
            auto_fixable=parsed.get("auto_fixable", False),
            reasoning=parsed.get("reasoning"),
            alternative_hypotheses=parsed.get("alternative_hypotheses"),
            source="agent_sdk",
            llm_model=self.model,
            tokens_used=total_tokens,
            cost_usd=round(cost, 6),
        )

    def _build_agent_prompt(
        self, context: DiagnosisContext, assembler: ContextAssembler,
    ) -> str:
        """Build the agent prompt with investigation instructions."""
        base_prompt = assembler.build_prompt(context)

        investigation_instructions = """
INVESTIGATION INSTRUCTIONS:
1. Read the traceback and error message carefully
2. If affected files are mentioned, read them to understand the code path
3. Check recent git history for relevant changes (git log --oneline -10)
4. Look for related configuration files or environment issues
5. Check for known failure patterns in the codebase

After investigating, output your diagnosis as a JSON object with these fields:
{
  "category": "TRANSIENT|DATA|CODE|CONFIG|EXTERNAL|INFRA",
  "root_cause": "One-paragraph explanation",
  "affected_module": "research|predictor|executor|backtester|data|dashboard|orchestration",
  "affected_files": ["path/to/file.py:line"],
  "confidence": 0.0-1.0,
  "remediation": "Step-by-step fix instructions",
  "auto_fixable": true/false,
  "auto_fix_type": "restart_service|rerun_step|update_config|code_fix|escalate",
  "alternative_hypotheses": ["Other possible causes"],
  "reasoning": "How you arrived at this diagnosis",
  "cascade_risk": "What downstream modules may be affected"
}

Output ONLY the JSON object, no other text."""

        return base_prompt + "\n\n" + investigation_instructions

    @staticmethod
    def _parse_result(text: str) -> dict:
        """Extract JSON from agent result text."""
        # Try direct parse
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass

        if not text:
            return {"root_cause": "Agent returned no result", "confidence": 0.2}

        # Try to find JSON in code fences
        for marker in ("```json", "```"):
            if marker in text:
                start = text.index(marker) + len(marker)
                end_search = text[start:]
                if "```" in end_search:
                    end = start + end_search.index("```")
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

        return {"root_cause": text[:500], "category": "CODE", "confidence": 0.3}


def is_agent_sdk_available() -> bool:
    """Check if Claude Agent SDK is installed and usable."""
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except ImportError:
        return False

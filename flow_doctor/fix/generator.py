"""Fix generator: calls LLM to produce a unified diff from a diagnosis."""

from __future__ import annotations

import sys
from typing import Dict, List, Optional

from flow_doctor.core.constants import DEFAULT_DIAGNOSIS_MODEL
from flow_doctor.fix.prompts import SYSTEM_PROMPT, build_fix_prompt


class FixGenerator:
    """Generates fix diffs using an LLM."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_DIAGNOSIS_MODEL,
        timeout_seconds: int = 60,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate(
        self,
        category: str,
        root_cause: str,
        confidence: float,
        remediation: Optional[str],
        affected_files: List[str],
        file_contents: Dict[str, str],
        test_contents: Dict[str, str],
        prior_rejections: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Generate a unified diff for the fix.

        Returns:
            The unified diff string, or None if the LLM cannot produce a fix.
        """
        import anthropic

        client = anthropic.Anthropic(
            api_key=self.api_key,
            timeout=self.timeout_seconds,
        )

        user_prompt = build_fix_prompt(
            category=category,
            root_cause=root_cause,
            confidence=confidence,
            remediation=remediation,
            affected_files=affected_files,
            file_contents=file_contents,
            test_contents=test_contents,
            prior_rejections=prior_rejections,
        )

        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        text = text.strip()

        if text == "NO_FIX":
            return None

        # Strip markdown fences if LLM wrapped them
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```diff or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines)

        return text

    @staticmethod
    def extract_files_from_diff(diff: str) -> List[str]:
        """Extract file paths from a unified diff."""
        files = []
        for line in diff.split("\n"):
            if line.startswith("+++ b/"):
                path = line[6:]
                if path and path != "/dev/null":
                    files.append(path)
        return files

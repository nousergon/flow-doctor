"""Prompts for LLM-based fix generation."""

from __future__ import annotations

from typing import Dict, List, Optional

SYSTEM_PROMPT = """\
You are a precise code repair agent. You receive a structured diagnosis of a software error \
along with the affected source files and their test files. Your job is to generate a minimal, \
targeted unified diff that fixes the root cause described in the diagnosis.

Rules:
1. Output ONLY a valid unified diff (the kind produced by `diff -u` or `git diff`). No explanation, \
no markdown fences, no commentary.
2. Only modify files listed in the affected_files field of the diagnosis. Do not touch unrelated files.
3. Make the smallest change that fixes the root cause. Do not refactor, add features, or clean up \
surrounding code.
4. Preserve existing code style, indentation, and conventions.
5. If the fix requires adding an import, place it with the existing imports in the correct \
alphabetical/grouped order.
6. If a prior fix attempt was rejected, learn from the rejection reason and avoid the same mistake.
7. If you cannot produce a confident fix, output exactly the string: NO_FIX
"""

USER_PROMPT_TEMPLATE = """\
## Diagnosis

- **Category:** {category}
- **Root Cause:** {root_cause}
- **Confidence:** {confidence:.0%}
- **Remediation:** {remediation}
- **Affected Files:** {affected_files}

## Affected File Contents

{file_contents}

## Test File Contents

{test_contents}

{rejection_context}
Generate a unified diff that fixes the root cause described above.
"""


def build_fix_prompt(
    category: str,
    root_cause: str,
    confidence: float,
    remediation: str,
    affected_files: List[str],
    file_contents: Dict[str, str],
    test_contents: Dict[str, str],
    prior_rejections: Optional[List[str]] = None,
) -> str:
    """Build the user prompt for fix generation."""
    # Format file contents
    file_sections: List[str] = []
    for path, content in file_contents.items():
        file_sections.append(f"### `{path}`\n```\n{content}\n```")
    file_str = "\n\n".join(file_sections) if file_sections else "(no file contents available)"

    # Format test contents
    test_sections: List[str] = []
    for path, content in test_contents.items():
        test_sections.append(f"### `{path}`\n```\n{content}\n```")
    test_str = "\n\n".join(test_sections) if test_sections else "(no test files found)"

    # Format rejection context
    rejection_ctx = ""
    if prior_rejections:
        items = "\n".join(f"- {r}" for r in prior_rejections)
        rejection_ctx = f"## Prior Rejected Fix Attempts\n\nThe following fixes were previously attempted and rejected. Do not repeat these mistakes:\n{items}\n\n"

    return USER_PROMPT_TEMPLATE.format(
        category=category,
        root_cause=root_cause,
        confidence=confidence,
        remediation=remediation or "Not specified",
        affected_files=", ".join(f"`{f}`" for f in affected_files),
        file_contents=file_str,
        test_contents=test_str,
        rejection_context=rejection_ctx,
    )

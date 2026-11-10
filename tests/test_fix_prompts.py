"""Formatting contracts for the fix generator's prompts."""

from __future__ import annotations

from string import Formatter
from typing import Optional, cast

import pytest

from flow_doctor.fix.prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, build_fix_prompt


def _prompt(
    confidence: float = 0.85,
    remediation: str = "Check the divisor",
    prior_rejections: Optional[list[str]] = None,
) -> str:
    return build_fix_prompt(
        category="CODE",
        root_cause="Division by zero",
        confidence=confidence,
        remediation=remediation,
        affected_files=["main.py", "helper.py"],
        file_contents={},
        test_contents={},
        prior_rejections=prior_rejections,
    )


@pytest.mark.parametrize("confidence, expected", [(0.85, "85%"), (1.0, "100%"), (0.0, "0%")])
def test_confidence_is_a_whole_percent(confidence: float, expected: str) -> None:
    assert f"- **Confidence:** {expected}\n" in _prompt(confidence=confidence)


@pytest.mark.parametrize("remediation", ["", None])
def test_missing_remediation(remediation: Optional[str]) -> None:
    # The generator passes Optional[str], although the prompt's annotation is str.
    assert "- **Remediation:** Not specified\n" in _prompt(remediation=cast(str, remediation))


def test_diagnosis_and_empty_contents() -> None:
    prompt = _prompt()
    assert "- **Category:** CODE\n" in prompt
    assert "- **Root Cause:** Division by zero\n" in prompt
    assert "- **Remediation:** Check the divisor\n" in prompt
    assert "- **Affected Files:** `main.py`, `helper.py`\n" in prompt
    assert "## Affected File Contents\n\n(no file contents available)" in prompt
    assert "## Test File Contents\n\n(no test files found)" in prompt


def test_contents_sections_and_template_substitution() -> None:
    files = {"main.py": "value = 1\nprint(value)", "helper.py": "value = 2\n"}
    tests = {"tests/test_main.py": "assert value == 1", "tests/test_helper.py": "assert value == 2\n"}
    prompt = build_fix_prompt("CODE", "Division by zero", 0.85, "Check the divisor", list(files), files, tests)
    for path, content in {**files, **tests}.items():
        assert f"### `{path}`\n```\n{content}\n```" in prompt

    closing = "Generate a unified diff that fixes the root cause described above.\n"
    assert prompt.endswith(closing)
    assert (
        prompt.index("## Diagnosis")
        < prompt.index("## Affected File Contents")
        < prompt.index("### `main.py`")
        < prompt.index("### `helper.py`")
        < prompt.index("## Test File Contents")
        < prompt.index("### `tests/test_main.py`")
        < prompt.index("### `tests/test_helper.py`")
        < prompt.index(closing)
    )
    for _, field, _, _ in Formatter().parse(USER_PROMPT_TEMPLATE):
        if field is not None:
            assert "{" + field not in prompt


@pytest.mark.parametrize("rejections", [None, []])
def test_no_prior_rejection_section(rejections: Optional[list[str]]) -> None:
    assert "## Prior Rejected Fix Attempts" not in _prompt(prior_rejections=rejections)


def test_prior_rejections_are_ordered_bullets() -> None:
    prompt = _prompt(prior_rejections=["Wrong file", "Incomplete fix"])
    assert prompt.count("## Prior Rejected Fix Attempts") == 1
    assert "\n- Wrong file\n- Incomplete fix\n\n" in prompt
    assert [line for line in prompt.splitlines() if line.startswith("- ") and not line.startswith("- **")] == [
        "- Wrong file", "- Incomplete fix",
    ]


def test_system_prompt_contract() -> None:
    assert SYSTEM_PROMPT.strip()
    assert SYSTEM_PROMPT.endswith("\n")
    assert "NO_FIX" in SYSTEM_PROMPT
    assert "unified diff" in SYSTEM_PROMPT

"""Context assembler: builds structured context for LLM diagnosis."""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from flow_doctor.core.models import KnownPattern, Report


@dataclass
class DiagnosisContext:
    """Structured context sent to the LLM for diagnosis."""
    error_type: Optional[str] = None
    error_message: str = ""
    traceback: Optional[str] = None
    logs: Optional[str] = None
    flow_name: str = ""
    repo: Optional[str] = None
    runtime_info: Optional[str] = None
    dependencies: Optional[List[str]] = None
    dependency_status: Optional[str] = None
    git_log: Optional[str] = None
    changed_files: Optional[str] = None
    known_patterns: Optional[List[str]] = None


# Approximate chars-per-token for budget estimation
_CHARS_PER_TOKEN = 4
_LOG_TOKEN_BUDGET = 30_000
_LOG_CHAR_BUDGET = _LOG_TOKEN_BUDGET * _CHARS_PER_TOKEN


_SYSTEM_PROMPT = """You are a pipeline reliability engineer. A monitored flow has \
reported an error. Diagnose the root cause from the information below. Output \
structured JSON only.

The report is not necessarily a crash: it may be an ERROR-level log record from a \
run that completed — the system flagging an anomaly it was DESIGNED to flag. Before \
attributing the report to a defect, weigh the hypothesis that the code worked as \
intended and the anomaly has a real-world cause: an upstream provider restatement, \
a corporate action (e.g. a stock split restates price history by an exact integer \
ratio — a 90% drop is exactly 10:1), a symbol delisting or ticker change, a market \
holiday, or a provider outage. If the evidence fits a world-event explanation at \
least as well as a code defect, prefer it and say so.

RECENT GIT CHANGES, when present, are context for the CODE hypothesis only — they \
are NOT the presumed culprit. Temporal proximity of a commit to a failure is weak \
evidence on its own; only implicate a commit when the error mechanism plausibly \
runs through the code it changed.

You MUST respond with valid JSON matching this exact schema:
{
  "category": "TRANSIENT|DATA|CODE|CONFIG|EXTERNAL|INFRA",
  "root_cause": "One-paragraph explanation of what went wrong and why",
  "affected_files": ["path/to/file.py:line"],
  "confidence": 0.0-1.0,
  "remediation": "Step-by-step instructions to fix this",
  "auto_fixable": true or false,
  "alternative_hypotheses": ["Other possible causes considered"],
  "reasoning": "Chain of thought: how you arrived at this diagnosis"
}

Categories:
- TRANSIENT: timeout, throttle, network blip — will likely resolve on retry
- DATA: missing or malformed input data
- CODE: logic bug, import error, type error
- CONFIG: environment variable, path, IAM, credential issue
- EXTERNAL: third-party API/service down, or an upstream/world event the flow is
  correctly surfacing (provider restatement, corporate action, delisting, holiday)
- INFRA: OOM, disk full, Lambda limits, resource exhaustion

"alternative_hypotheses" must include at least one non-CODE hypothesis whenever you
choose CODE, and at least one CODE hypothesis whenever you choose another category."""


class ContextAssembler:
    """Builds the LLM prompt from a report and surrounding context."""

    def __init__(
        self,
        repo: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
    ):
        self.repo = repo
        self.dependencies = dependencies or []

    def assemble(
        self,
        report: Report,
        git_context: Optional[Dict[str, str]] = None,
        known_patterns: Optional[List[KnownPattern]] = None,
        dependency_status: Optional[str] = None,
    ) -> DiagnosisContext:
        """Assemble a DiagnosisContext from a report and external context."""
        logs = self._truncate_logs(report.logs) if report.logs else None

        pattern_strs = None
        if known_patterns:
            pattern_strs = [
                f"[{p.category}] {p.root_cause}" + (f" → {p.resolution}" if p.resolution else "")
                for p in known_patterns
            ]

        runtime = f"Python {platform.python_version()}, {platform.system()} {platform.release()}"

        return DiagnosisContext(
            error_type=report.error_type,
            error_message=report.error_message,
            traceback=report.traceback,
            logs=logs,
            flow_name=report.flow_name,
            repo=self.repo,
            runtime_info=runtime,
            dependencies=self.dependencies if self.dependencies else None,
            dependency_status=dependency_status,
            git_log=git_context.get("git_log") if git_context else None,
            changed_files=git_context.get("changed_files") if git_context else None,
            known_patterns=pattern_strs,
        )

    def build_prompt(self, ctx: DiagnosisContext) -> str:
        """Build the user message for the LLM from a DiagnosisContext."""
        sections = []

        # Exception
        if ctx.error_type:
            sections.append(f"EXCEPTION:\n{ctx.error_type}: {ctx.error_message}")
        else:
            sections.append(f"ERROR MESSAGE:\n{ctx.error_message}")

        # Traceback
        if ctx.traceback:
            sections.append(f"TRACEBACK:\n{ctx.traceback}")

        # Logs
        if ctx.logs:
            line_count = ctx.logs.count("\n") + 1
            sections.append(f"CAPTURED LOGS (last {line_count} lines):\n{ctx.logs}")

        # Flow context
        flow_lines = [f"- Name: {ctx.flow_name}"]
        if ctx.repo:
            flow_lines.append(f"- Repo: {ctx.repo}")
        if ctx.runtime_info:
            flow_lines.append(f"- Runtime: {ctx.runtime_info}")
        if ctx.dependencies:
            dep_str = ", ".join(ctx.dependencies)
            status = ctx.dependency_status or "unknown"
            flow_lines.append(f"- Dependencies: {dep_str} (status: {status})")
        sections.append("FLOW CONTEXT:\n" + "\n".join(flow_lines))

        # Git context
        if ctx.git_log:
            sections.append(f"RECENT GIT CHANGES (last 7 days):\n{ctx.git_log}")
        if ctx.changed_files:
            sections.append(f"CHANGED FILES:\n{ctx.changed_files}")

        # Known patterns
        if ctx.known_patterns:
            sections.append(
                "KNOWN FAILURE PATTERNS FOR THIS FLOW:\n"
                + "\n".join(f"- {p}" for p in ctx.known_patterns)
            )

        return "\n\n".join(sections)

    @property
    def system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    @staticmethod
    def _truncate_logs(logs: str) -> str:
        """Smart log truncation within token budget.

        Strategy:
        1. Always include the last N lines (tail of logs near the error)
        2. Include lines matching ERROR/WARNING/Traceback with surrounding context
        3. Truncate to ~30K token budget
        """
        if len(logs) <= _LOG_CHAR_BUDGET:
            return logs

        lines = logs.splitlines()
        important_indices: set = set()

        # Always include last 200 lines
        tail_start = max(0, len(lines) - 200)
        for i in range(tail_start, len(lines)):
            important_indices.add(i)

        # Include ERROR/WARNING/Traceback lines with 3 lines context
        keywords = ("ERROR", "WARNING", "Traceback", "Exception", "CRITICAL")
        for i, line in enumerate(lines):
            if any(kw in line for kw in keywords):
                for j in range(max(0, i - 3), min(len(lines), i + 4)):
                    important_indices.add(j)

        # Build truncated output from sorted indices
        sorted_indices = sorted(important_indices)
        result_lines = []
        prev_idx = -2
        for idx in sorted_indices:
            if idx > prev_idx + 1:
                skipped = idx - prev_idx - 1
                if prev_idx >= 0:
                    result_lines.append(f"... ({skipped} lines omitted) ...")
            result_lines.append(lines[idx])
            prev_idx = idx

        result = "\n".join(result_lines)

        # Final hard truncation if still over budget
        if len(result) > _LOG_CHAR_BUDGET:
            result = result[-_LOG_CHAR_BUDGET:]
            result = "... (truncated) ...\n" + result

        return result

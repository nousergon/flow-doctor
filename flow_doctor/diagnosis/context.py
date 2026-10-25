"""Context assembler: builds structured context for LLM diagnosis."""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
    sha_range_context: Optional[str] = None
    known_patterns: Optional[List[str]] = None
    # The ``Report.id`` this context was assembled from. It is the parent
    # document of every typed source the trace records (``trace_sources``),
    # so an eval can join a diagnosis trace back to its report.
    report_id: Optional[str] = None


#: Typed-citation id of each prompt section, in prompt order. ``flow_context``
#: (name, repo, runtime, dependency list) is configuration this process
#: states about itself, not evidence the model reads, so it is never cited.
CITED_SECTIONS = (
    "exception",
    "error_message",
    "traceback",
    "logs",
    "commit_range",
    "git_log",
    "changed_files",
    "known_patterns",
)


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
choose CODE, and at least one CODE hypothesis whenever you choose another category.

DEPLOY-DRIFT ERRORS: when the error message names two commit SHAs and the prompt
includes DETERMINISTIC COMMIT-RANGE CONTEXT, the context names which SHA is newer
(verified by git merge-base). The newer SHA is the correct checkout target — it
contains the commits the older SHA lacks. Never recommend checking out the older
SHA when the context shows it is the ancestor. If commit-range context is present
but neither SHA is confirmed newer (non-linear history), state that ordering
could not be determined and do NOT recommend a specific checkout direction — label
the remediation as requiring human verification of the correct target SHA."""


class ContextAssembler:
    """Builds the LLM prompt from a report and surrounding context."""

    def __init__(
        self,
        repo: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
    ) -> None:
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
            sha_range_context=git_context.get("sha_range_context") if git_context else None,
            known_patterns=pattern_strs,
            report_id=report.id,
        )

    def build_prompt(self, ctx: DiagnosisContext) -> str:
        """Build the user message for the LLM from a DiagnosisContext."""
        return "\n\n".join(header + body for _, header, body in self._sections(ctx))

    def _sections(self, ctx: DiagnosisContext) -> List[Tuple[str, str, str]]:
        """``(name, header, body)`` for each prompt section, in prompt order.

        :meth:`build_prompt` joins exactly these, so :func:`trace_sources`
        locates each body in the prompt without re-rendering it.
        """
        sections: List[Tuple[str, str, str]] = []

        # Exception
        if ctx.error_type:
            sections.append(("exception", "EXCEPTION:\n", f"{ctx.error_type}: {ctx.error_message}"))
        else:
            sections.append(("error_message", "ERROR MESSAGE:\n", ctx.error_message))

        # Traceback
        if ctx.traceback:
            sections.append(("traceback", "TRACEBACK:\n", ctx.traceback))

        # Logs
        if ctx.logs:
            line_count = ctx.logs.count("\n") + 1
            sections.append(("logs", f"CAPTURED LOGS (last {line_count} lines):\n", ctx.logs))

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
        sections.append(("flow_context", "FLOW CONTEXT:\n", "\n".join(flow_lines)))

        # Commit-range context (deterministic, always placed before git-log)
        if ctx.sha_range_context:
            sections.append((
                "commit_range",
                "DETERMINISTIC COMMIT-RANGE CONTEXT (ordering verified via git merge-base):\n",
                ctx.sha_range_context,
            ))

        # Git context
        if ctx.git_log:
            sections.append(("git_log", "RECENT GIT CHANGES (last 7 days):\n", ctx.git_log))
        if ctx.changed_files:
            sections.append(("changed_files", "CHANGED FILES:\n", ctx.changed_files))

        # Known patterns
        if ctx.known_patterns:
            sections.append((
                "known_patterns",
                "KNOWN FAILURE PATTERNS FOR THIS FLOW:\n",
                "\n".join(f"- {p}" for p in ctx.known_patterns),
            ))

        return sections

    def locate_sources(
        self, ctx: DiagnosisContext, user_content: str
    ) -> List[Tuple[str, int, int]]:
        """``(name, char_start, char_end)`` of each cited section's body in
        ``user_content``, which must be the prompt that is actually sent.

        A section is located by its header plus body, which must occur in
        ``user_content`` exactly once; the offsets cover the body alone. A
        section that is empty, not found, or found more than once is left
        out rather than guessed, because an offset into the wrong place is a
        citation to the wrong text.
        """
        located: List[Tuple[str, int, int]] = []
        for name, header, body in self._sections(ctx):
            if name not in CITED_SECTIONS or not body:
                continue
            needle = header + body
            if user_content.count(needle) != 1:
                continue
            start = user_content.index(needle) + len(header)
            located.append((name, start, start + len(body)))
        return located

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

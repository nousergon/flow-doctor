"""Tests for the context assembler."""

from flow_doctor.core.models import KnownPattern, Report
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext


def _make_report(**kwargs):
    defaults = dict(
        flow_name="test-flow",
        error_message="Something went wrong",
        error_type="ValueError",
        traceback="Traceback (most recent call last):\n  File 'test.py', line 10\nValueError: bad value",
    )
    defaults.update(kwargs)
    return Report(**defaults)


def test_assemble_basic():
    assembler = ContextAssembler(repo="owner/repo", dependencies=["upstream"])
    report = _make_report()
    ctx = assembler.assemble(report)

    assert isinstance(ctx, DiagnosisContext)
    assert ctx.error_type == "ValueError"
    assert ctx.error_message == "Something went wrong"
    assert ctx.flow_name == "test-flow"
    assert ctx.repo == "owner/repo"
    assert ctx.dependencies == ["upstream"]
    assert ctx.runtime_info is not None


def test_assemble_with_git_context():
    assembler = ContextAssembler()
    report = _make_report()
    git_ctx = {"git_log": "abc1234 Fix bug", "changed_files": "src/main.py"}
    ctx = assembler.assemble(report, git_context=git_ctx)

    assert ctx.git_log == "abc1234 Fix bug"
    assert ctx.changed_files == "src/main.py"


def test_assemble_with_known_patterns():
    assembler = ContextAssembler()
    report = _make_report()
    patterns = [
        KnownPattern(
            error_signature="sig1", category="EXTERNAL",
            root_cause="API down", resolution="Retry later",
        ),
    ]
    ctx = assembler.assemble(report, known_patterns=patterns)

    assert ctx.known_patterns is not None
    assert len(ctx.known_patterns) == 1
    assert "EXTERNAL" in ctx.known_patterns[0]
    assert "Retry later" in ctx.known_patterns[0]


def test_build_prompt_basic():
    assembler = ContextAssembler(repo="owner/repo")
    report = _make_report()
    ctx = assembler.assemble(report)
    prompt = assembler.build_prompt(ctx)

    assert "ValueError" in prompt
    assert "Something went wrong" in prompt
    assert "Traceback" in prompt
    assert "test-flow" in prompt
    assert "owner/repo" in prompt


def test_build_prompt_with_logs():
    assembler = ContextAssembler()
    report = _make_report(logs="INFO: Starting\nERROR: Failed here")
    ctx = assembler.assemble(report)
    prompt = assembler.build_prompt(ctx)

    assert "CAPTURED LOGS" in prompt
    assert "Failed here" in prompt


def test_build_prompt_with_git():
    assembler = ContextAssembler()
    report = _make_report()
    git_ctx = {"git_log": "abc Fix thing", "changed_files": "foo.py"}
    ctx = assembler.assemble(report, git_context=git_ctx)
    prompt = assembler.build_prompt(ctx)

    assert "RECENT GIT CHANGES" in prompt
    assert "CHANGED FILES" in prompt


def test_build_prompt_no_exception():
    assembler = ContextAssembler()
    report = _make_report(error_type=None, traceback=None)
    ctx = assembler.assemble(report)
    prompt = assembler.build_prompt(ctx)

    assert "ERROR MESSAGE" in prompt
    assert "Something went wrong" in prompt


def test_system_prompt():
    assembler = ContextAssembler()
    assert "pipeline reliability engineer" in assembler.system_prompt
    assert "TRANSIENT" in assembler.system_prompt


def test_system_prompt_weighs_world_event_hypotheses():
    """The KLAC regression (alpha-engine-data#417-419): a stock-split restatement
    was confidently attributed to a recent commit. The prompt must (a) allow for
    working-as-designed anomaly flags, (b) name corporate actions as a cause
    class, and (c) de-anchor recent git changes from presumed culpability."""
    sp = ContextAssembler().system_prompt
    assert "DESIGNED to flag" in sp
    assert "corporate action" in sp
    assert "stock split" in sp
    assert "NOT the presumed culprit" in sp
    # Forced hypothesis diversity: a CODE verdict must carry a non-CODE alternative.
    assert "at least one non-CODE hypothesis" in sp


def test_system_prompt_external_covers_upstream_world_events():
    sp = ContextAssembler().system_prompt
    assert "EXTERNAL: third-party API/service down, or an upstream/world event" in sp


def test_log_truncation_short():
    """Short logs should pass through unchanged."""
    logs = "line1\nline2\nline3"
    result = ContextAssembler._truncate_logs(logs)
    assert result == logs


def test_log_truncation_long():
    """Long logs should be truncated with markers."""
    # Create logs exceeding the budget
    lines = [f"INFO: Log line number {i}" for i in range(50000)]
    lines[1000] = "ERROR: Something failed here"
    logs = "\n".join(lines)

    result = ContextAssembler._truncate_logs(logs)

    # Should be significantly shorter
    assert len(result) < len(logs)
    # Should contain the ERROR line
    assert "ERROR: Something failed here" in result
    # Should contain tail lines
    assert "Log line number 49999" in result
    # Should have omission markers
    assert "omitted" in result

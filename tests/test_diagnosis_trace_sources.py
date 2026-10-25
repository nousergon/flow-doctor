"""Typed source citations on the diagnosis call's llm_trace.v1 row
(alpha-engine-config-I12312).

The diagnosis prompt is unchanged: each evidence section is LOCATED in the
prompt that is sent, never re-rendered around ``<source>`` tags, so these
tests pin both halves — the prompt is byte-for-byte what it was, and every
located offset slices exactly the section body it names.

krepis is not in the default CI install, so the provider-wiring tests inject
a minimal fake ``krepis.llm_trace``; the round trip through the real
``krepis.llm_trace.bind_sources`` runs only where krepis is installed.
"""

import hashlib
import json
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from flow_doctor.core.models import Report
from flow_doctor.diagnosis.context import CITED_SECTIONS, ContextAssembler, DiagnosisContext
from flow_doctor.diagnosis.provider import RouterProvider, trace_sources_or_none
from tests.test_router_provider import _edge_spec, _fake_llm_result, _install_fake_krepis, _route


def _full_context(**overrides):
    values = dict(
        error_type="ValueError",
        error_message="invalid literal for int(): 'x'",
        traceback="Traceback (most recent call last):\n  File \"a.py\", line 3\nValueError: invalid literal",
        logs="INFO start\nERROR parse failed\nINFO done",
        flow_name="daily-collect",
        repo="nousergon/example",
        runtime_info="Python 3.11.9, Linux 6.1",
        dependencies=["s3", "fred"],
        dependency_status="ok",
        sha_range_context="abc1234 is newer than def5678",
        git_log="abc1234 fix parser",
        changed_files="collectors/parse.py",
        known_patterns=["[DATA] provider restatement → re-pull"],
        report_id="01JTESTREPORTID",
    )
    values.update(overrides)
    return DiagnosisContext(**values)


# Frozen: the prompt this context produced before sections were cited. A diff
# here is a change to what the model reads, which this arc must never make.
_FULL_PROMPT = (
    "EXCEPTION:\nValueError: invalid literal for int(): 'x'\n\n"
    "TRACEBACK:\nTraceback (most recent call last):\n  File \"a.py\", line 3\nValueError: invalid literal\n\n"
    "CAPTURED LOGS (last 3 lines):\nINFO start\nERROR parse failed\nINFO done\n\n"
    "FLOW CONTEXT:\n- Name: daily-collect\n- Repo: nousergon/example\n- Runtime: Python 3.11.9, Linux 6.1\n"
    "- Dependencies: s3, fred (status: ok)\n\n"
    "DETERMINISTIC COMMIT-RANGE CONTEXT (ordering verified via git merge-base):\nabc1234 is newer than def5678\n\n"
    "RECENT GIT CHANGES (last 7 days):\nabc1234 fix parser\n\n"
    "CHANGED FILES:\ncollectors/parse.py\n\n"
    "KNOWN FAILURE PATTERNS FOR THIS FLOW:\n- [DATA] provider restatement → re-pull"
)


def test_prompt_is_unchanged_by_citation():
    assert ContextAssembler().build_prompt(_full_context()) == _FULL_PROMPT


def test_every_located_offset_slices_exactly_its_section_body():
    ctx = _full_context()
    prompt = ContextAssembler().build_prompt(ctx)
    located = {name: prompt[s:e] for name, s, e in ContextAssembler().locate_sources(ctx, prompt)}
    assert located == {
        "exception": "ValueError: invalid literal for int(): 'x'",
        "traceback": ctx.traceback,
        "logs": ctx.logs,
        "commit_range": ctx.sha_range_context,
        "git_log": ctx.git_log,
        "changed_files": ctx.changed_files,
        "known_patterns": "- [DATA] provider restatement → re-pull",
    }


def test_flow_context_is_never_cited():
    ctx = _full_context()
    prompt = ContextAssembler().build_prompt(ctx)
    names = [name for name, _, _ in ContextAssembler().locate_sources(ctx, prompt)]
    assert "flow_context" not in names
    assert "flow_context" not in CITED_SECTIONS


def test_empty_error_message_is_not_cited():
    ctx = _full_context(error_type=None, error_message="")
    prompt = ContextAssembler().build_prompt(ctx)
    names = [name for name, _, _ in ContextAssembler().locate_sources(ctx, prompt)]
    assert "error_message" not in names and "exception" not in names


def test_section_found_more_than_once_is_left_out():
    ctx = _full_context()
    prompt = ContextAssembler().build_prompt(ctx)
    doubled = prompt + "\n\nTRACEBACK:\n" + ctx.traceback
    names = [name for name, _, _ in ContextAssembler().locate_sources(ctx, doubled)]
    assert "traceback" not in names
    assert "logs" in names


def test_section_absent_from_the_sent_prompt_is_left_out():
    ctx = _full_context()
    names = [name for name, _, _ in ContextAssembler().locate_sources(ctx, "something else")]
    assert names == []


def test_assemble_carries_the_report_id():
    report = Report(flow_name="f", error_message="boom")
    ctx = ContextAssembler().assemble(report)
    assert ctx.report_id == report.id


# ── provider wiring, against a minimal fake krepis.llm_trace ───────────────


@dataclass(frozen=True)
class _FakeSourceBlock:
    cid: str
    kind: str
    source_id: str
    text: str
    document_id: Optional[str] = None
    uri: Optional[str] = None
    version: Optional[str] = None
    retrieval: Any = None
    meta: Any = None


@dataclass(frozen=True)
class _FakeRenderedSource:
    block: Any
    content_sha256: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class _FakeRenderedSources:
    text: str
    sources: Tuple = field(default_factory=tuple)


def _install_fake_llm_trace(monkeypatch):
    mod = types.ModuleType("krepis.llm_trace")
    mod.SourceBlock = _FakeSourceBlock
    mod.RenderedSource = _FakeRenderedSource
    mod.RenderedSources = _FakeRenderedSources
    mod.sha256_hex = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    monkeypatch.setitem(sys.modules, "krepis.llm_trace", mod)
    return mod


def test_sources_are_typed_artifacts_under_the_report(monkeypatch):
    _install_fake_llm_trace(monkeypatch)
    ctx = _full_context()
    prompt = ContextAssembler().build_prompt(ctx)

    rendered = trace_sources_or_none(ctx, ContextAssembler(), prompt)

    assert rendered.text == prompt
    by_cid = {s.block.cid: s for s in rendered.sources}
    assert set(by_cid) == {
        "exception", "traceback", "logs", "commit_range", "git_log", "changed_files", "known_patterns",
    }
    tb = by_cid["traceback"]
    assert tb.block.kind == "artifact"
    assert tb.block.source_id == "flow-doctor/report/01JTESTREPORTID#traceback"
    assert tb.block.document_id == "flow-doctor/report/01JTESTREPORTID"
    assert tb.block.meta == {"section": "traceback", "flow_name": "daily-collect"}
    assert prompt[tb.char_start:tb.char_end] == tb.block.text == ctx.traceback
    assert tb.content_sha256 == hashlib.sha256(ctx.traceback.encode("utf-8")).hexdigest()


def test_context_without_a_report_id_is_still_namespaced(monkeypatch):
    _install_fake_llm_trace(monkeypatch)
    ctx = _full_context(report_id=None)
    prompt = ContextAssembler().build_prompt(ctx)
    rendered = trace_sources_or_none(ctx, ContextAssembler(), prompt)
    assert {s.block.document_id for s in rendered.sources} == {
        "flow-doctor/report/unidentified/daily-collect"
    }


def test_krepis_without_llm_trace_sends_no_sources(monkeypatch):
    # krepis < 0.59.74 has no llm_trace module; the router extra accepts 0.17.
    monkeypatch.setitem(sys.modules, "krepis.llm_trace", None)
    ctx = _full_context()
    assert trace_sources_or_none(ctx, ContextAssembler(), ContextAssembler().build_prompt(ctx)) is None


def test_rendering_failure_sends_no_sources(monkeypatch, capsys):
    _install_fake_llm_trace(monkeypatch)
    assembler = ContextAssembler()
    monkeypatch.setattr(assembler, "locate_sources", MagicMock(side_effect=RuntimeError("bad")))
    assert trace_sources_or_none(_full_context(), assembler, "x") is None
    assert "trace source rendering failed" in capsys.readouterr().err


def _diagnose_with_fake_router(monkeypatch, ctx):
    resolve = MagicMock(return_value=(_edge_spec(), _route()))
    result_json = json.dumps({"category": "DATA", "root_cause": "restated", "confidence": 0.8})
    client_cls, _ = _install_fake_krepis(
        monkeypatch, resolve_group_spec=resolve, complete_return=_fake_llm_result(result_json)
    )
    RouterProvider(model_group="low").diagnose(ctx, ContextAssembler())
    return client_cls.return_value.complete.call_args.kwargs


def test_router_provider_on_a_krepis_without_llm_trace_sends_no_sources(monkeypatch):
    # The fake krepis package has no llm_trace submodule, like krepis < 0.59.74.
    monkeypatch.setitem(sys.modules, "krepis.llm_trace", None)
    kwargs = _diagnose_with_fake_router(monkeypatch, _full_context())
    assert "sources" not in kwargs
    assert kwargs["user_content"] == _FULL_PROMPT


def test_router_provider_sends_sources_when_llm_trace_is_available(monkeypatch):
    ctx = _full_context()
    resolve = MagicMock(return_value=(_edge_spec(), _route()))
    result_json = json.dumps({"category": "DATA", "root_cause": "restated", "confidence": 0.8})
    client_cls, _ = _install_fake_krepis(
        monkeypatch, resolve_group_spec=resolve, complete_return=_fake_llm_result(result_json)
    )
    _install_fake_llm_trace(monkeypatch)

    RouterProvider(model_group="low").diagnose(ctx, ContextAssembler())

    kwargs = client_cls.return_value.complete.call_args.kwargs
    assert kwargs["user_content"] == _FULL_PROMPT
    sources = kwargs["sources"]
    assert sources.text == kwargs["user_content"]
    assert all(kwargs["user_content"][s.char_start:s.char_end] == s.block.text for s in sources.sources)


# ── round trip through the real krepis, where installed ────────────────────


def test_real_krepis_binds_the_sources_without_changing_the_prompt():
    llm_trace = pytest.importorskip("krepis.llm_trace")
    ctx = _full_context()
    prompt = ContextAssembler().build_prompt(ctx)
    rendered = trace_sources_or_none(ctx, ContextAssembler(), prompt)

    bound_prompt, bound = llm_trace.bind_sources(prompt, rendered)

    assert bound_prompt == prompt
    assert len(bound) == 7
    for s in bound:
        assert prompt[s.char_start:s.char_end] == s.block.text
        assert s.content_sha256 == llm_trace.sha256_hex(s.block.text)

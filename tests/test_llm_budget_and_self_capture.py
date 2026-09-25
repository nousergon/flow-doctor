"""The diagnosis LLM budget, and flow-doctor never re-capturing its own
diagnosis call's log records as new reports.

Observed 2026-09-24 ~20:51Z on data-collector: the diagnosis path called the
krepis client with ``max_tokens=2048`` on router group ``low``, which resolved
to a reasoning model that spent all 2048 tokens reasoning and returned empty
content. krepis logged ``llm: EMPTY message.content ... finish_reason='length'
completion_tokens=2048 reasoning_tokens=2048`` at ERROR, and flow-doctor's
root-logger handler filed THAT as a new report — a noise loop in which the
diagnosis itself was lost. Same class as alpha-engine-config-I6917 / I8700 /
I6901.
"""

import ast
import json
import logging
import pathlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import flow_doctor
from flow_doctor.core._context import in_own_llm_call, own_llm_call_scope
from flow_doctor.core.constants import LLM_MAX_TOKENS
from flow_doctor.core.handler import FlowDoctorHandler
from flow_doctor.diagnosis.context import ContextAssembler
from flow_doctor.diagnosis.provider import (
    DiagnosisProvider,
    EmptyDiagnosisResponse,
    OpenAICompatProvider,
    RouterProvider,
)
from flow_doctor.fix.generator import FixGenerator
from tests.test_diagnosis_provider import _install_fake_openai, _make_context
from tests.test_router_provider import (
    _edge_spec,
    _fake_llm_result,
    _install_fake_krepis,
    _route,
)

_PKG = pathlib.Path(flow_doctor.__file__).parent

# alpha-engine-config-I8700 occurrence 2: the `low` group's measured p99
# reasoning draw (13089) plus the p95 answer length (1105). A ceiling below
# this sum empties calls in the measured tail — and that measurement is
# censored at its old ceiling, so this is a floor, not a target.
_MEASURED_LOW_GROUP_NEED = 13089 + 1105


# ── the budget ─────────────────────────────────────────────────────────────


def test_budget_clears_the_measured_reasoning_draw_of_the_low_group():
    assert LLM_MAX_TOKENS >= _MEASURED_LOW_GROUP_NEED


def _max_tokens_keywords():
    """Every ``max_tokens=<expr>`` keyword and default in the package."""
    found = []
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "max_tokens":
                found.append((path, node.value))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                pos = args.posonlyargs + args.args
                pos_defaults = dict(zip(pos[len(pos) - len(args.defaults):], args.defaults))
                kw_defaults = {
                    a: d for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
                }
                for arg, default in {**pos_defaults, **kw_defaults}.items():
                    if arg.arg == "max_tokens":
                        found.append((path, default))
    return found


def test_every_max_tokens_in_the_package_is_the_named_constant():
    """No call site may carry its own literal: a scattered literal is how
    one site was left at 2048 while the class was fixed elsewhere."""
    sites = _max_tokens_keywords()
    # diagnosis: openai_compat, router resolve, router complete; fix: router
    # resolve, router complete, openai_compat; plus resolve_router_edge's
    # default. A drop means a site stopped passing the budget at all.
    assert len(sites) >= 7, sites
    offenders = [
        f"{path.relative_to(_PKG.parent)}:{value.lineno} max_tokens={ast.unparse(value)}"
        for path, value in sites
        # ``max_tokens=max_tokens`` is resolve_router_edge forwarding its own
        # parameter, whose default is checked as a site in its own right.
        if not (isinstance(value, ast.Name) and value.id in ("LLM_MAX_TOKENS", "max_tokens"))
    ]
    assert not offenders, offenders


def test_openai_compat_diagnosis_sends_the_budget(monkeypatch):
    provider = OpenAICompatProvider(
        api_key="k", model="m", base_url="https://openrouter.ai/api/v1"
    )
    resp = _openai_response(json.dumps({"category": "DATA", "root_cause": "x"}))
    client = _install_fake_openai(monkeypatch, resp)

    provider.diagnose(_make_context(), ContextAssembler())

    assert client.chat.completions.create.call_args.kwargs["max_tokens"] == LLM_MAX_TOKENS


def test_router_diagnosis_resolves_and_completes_with_the_budget(monkeypatch):
    resolve = MagicMock(return_value=(_edge_spec(), _route()))
    client_cls, _ = _install_fake_krepis(
        monkeypatch,
        resolve_group_spec=resolve,
        complete_return=_fake_llm_result(json.dumps({"category": "CODE", "root_cause": "x"})),
    )

    RouterProvider(model_group="low").diagnose(_make_context(), ContextAssembler())

    assert resolve.call_args.kwargs["max_tokens"] == LLM_MAX_TOKENS
    complete = client_cls.return_value.complete
    assert complete.call_args.kwargs["max_tokens"] == LLM_MAX_TOKENS


def test_router_fix_generation_resolves_and_completes_with_the_budget(monkeypatch):
    resolve = MagicMock(return_value=(_edge_spec(), _route()))
    client_cls, _ = _install_fake_krepis(
        monkeypatch,
        resolve_group_spec=resolve,
        complete_return=_fake_llm_result("NO_FIX"),
    )

    FixGenerator(provider="router", model_group="low")._complete_router("prompt")

    assert resolve.call_args.kwargs["max_tokens"] == LLM_MAX_TOKENS
    complete = client_cls.return_value.complete
    assert complete.call_args.kwargs["max_tokens"] == LLM_MAX_TOKENS


def test_openai_compat_fix_generation_sends_the_budget(monkeypatch):
    client = _install_fake_openai(monkeypatch, _openai_response("NO_FIX"))

    FixGenerator(
        provider="openai_compat", api_key="k", base_url="http://vllm.internal/v1"
    )._complete_openai_compat("prompt")

    assert client.chat.completions.create.call_args.kwargs["max_tokens"] == LLM_MAX_TOKENS


# ── empty content degrades, never fabricates ──────────────────────────────


def _openai_response(content, *, finish_reason="stop", completion=500, reasoning=0):
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=completion,
        cost=0.001,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def test_openai_compat_empty_content_raises_instead_of_a_junk_diagnosis(monkeypatch):
    provider = OpenAICompatProvider(
        api_key="k", model="m", base_url="https://openrouter.ai/api/v1"
    )
    _install_fake_openai(
        monkeypatch,
        _openai_response("", finish_reason="length", completion=16000, reasoning=16000),
    )

    with pytest.raises(EmptyDiagnosisResponse, match="finish_reason='length'"):
        provider.diagnose(_make_context(), ContextAssembler())


def test_router_empty_content_raises_after_recording_the_billed_spend(monkeypatch):
    resolve = MagicMock(return_value=(_edge_spec(), _route()))
    result = _fake_llm_result("   ")
    result.raw_response = _openai_response(
        "", finish_reason="length", completion=16000, reasoning=16000
    )
    _, record_cost = _install_fake_krepis(
        monkeypatch, resolve_group_spec=resolve, complete_return=result
    )

    with pytest.raises(EmptyDiagnosisResponse, match="reasoning_tokens=16000"):
        RouterProvider(model_group="low").diagnose(_make_context(), ContextAssembler())

    # An exhausted budget is fully billed; the spend must still be recorded.
    record_cost.assert_called_once()


# ── the loop ───────────────────────────────────────────────────────────────


class _KrepisLikeProvider(DiagnosisProvider):
    """Stands in for the real router path: logs the krepis EMPTY-content
    ERROR on the ``krepis.llm`` logger from inside ``diagnose()`` — exactly
    what krepis does on the calling thread — then fails the way the real
    provider now does."""

    def __init__(self):
        self.calls = 0

    def diagnose(self, context, assembler):
        self.calls += 1
        logging.getLogger("krepis.llm").error(
            "llm: EMPTY message.content on a successful response — "
            "finish_reason='length' completion_tokens=2048 "
            "reasoning_tokens=2048 model='low'"
        )
        raise EmptyDiagnosisResponse("diagnosis model 'low' returned EMPTY content")


def _fd_with_provider(tmp_path, provider):
    fd = flow_doctor.FlowDoctor.from_config(
        flow_name="data-collector",
        store={"type": "sqlite", "path": str(tmp_path / "fd.db")},
        notify=[],
        rate_limits={"max_alerts_per_day": 50, "max_diagnosed_per_day": 50},
        diagnosis={
            "enabled": True,
            "provider": "openai_compat",
            "api_key": "fake-key",
            "base_url": "https://openrouter.ai/api/v1",
        },
    )
    assert fd._knowledge_base is not None and fd._rate_limiter is not None
    fd._diagnosis_provider = provider
    return fd


@pytest.fixture
def root_handler():
    """Attach a FlowDoctorHandler to the ROOT logger, as krepis
    ``setup_logging`` does in every fleet process."""
    attached = []

    def attach(fd):
        handler = FlowDoctorHandler(fd, level=logging.ERROR)
        logging.getLogger().addHandler(handler)
        attached.append(handler)
        return handler

    yield attach
    for handler in attached:
        logging.getLogger().removeHandler(handler)
        handler.shutdown(timeout=5)


def test_krepis_error_logged_during_diagnosis_does_not_file_a_new_report(
    tmp_path, root_handler
):
    provider = _KrepisLikeProvider()
    fd = _fd_with_provider(tmp_path, provider)
    handler = root_handler(fd)
    diagnosed = []
    run_diagnosis = fd._run_diagnosis

    def spy(report, cascade_source):
        result = run_diagnosis(report, cascade_source)
        diagnosed.append((report.error_message, result, report.diagnosis_error))
        return result

    fd._run_diagnosis = spy

    logging.getLogger("data_collector.host").error("upstream feed returned 503")
    # Let the worker finish diagnosing BEFORE the shutdown sentinel is queued:
    # a record the diagnosis emitted is enqueued synchronously inside it, so
    # waiting here puts any looped record AHEAD of the sentinel, where the
    # worker would file it. Shutting down first would hide the loop.
    deadline = time.monotonic() + 5
    while not diagnosed and time.monotonic() < deadline:
        time.sleep(0.01)
    handler.shutdown(timeout=5)

    reports = fd.history(limit=10)
    assert [r.error_message for r in reports] == ["upstream feed returned 503"]
    assert provider.calls == 1
    # Degraded, not lost: the report was filed and dispatched WITHOUT a
    # diagnosis, carrying why (every notifier renders diagnosis_error).
    [(message, diagnosis, diagnosis_error)] = diagnosed
    assert message == "upstream feed returned 503"
    assert diagnosis is None
    assert diagnosis_error.startswith("EmptyDiagnosisResponse:")


def test_krepis_error_on_a_host_thread_is_still_captured(tmp_path, root_handler):
    """The scope is per-thread: an ERROR the HOST logs on another thread
    while a diagnosis is in flight is a real error and must still be filed.
    """
    host_logged = threading.Event()

    class _SlowProvider(DiagnosisProvider):
        def diagnose(self, context, assembler):
            if host_logged.is_set():
                # The captured host error is itself diagnosed; don't spawn again.
                raise EmptyDiagnosisResponse("empty")

            def host_work():
                logging.getLogger("krepis.llm").error("host's own LLM call failed")
                host_logged.set()

            t = threading.Thread(target=host_work)
            t.start()
            t.join(timeout=5)
            raise EmptyDiagnosisResponse("empty")

    fd = _fd_with_provider(tmp_path, _SlowProvider())
    handler = root_handler(fd)

    fd.report(RuntimeError("first failure"))
    assert host_logged.is_set()
    handler.shutdown(timeout=5)

    messages = sorted(r.error_message for r in fd.history(limit=10))
    assert messages == ["first failure", "host's own LLM call failed"]


def test_krepis_error_outside_diagnosis_is_still_captured(tmp_path, root_handler):
    fd = _fd_with_provider(tmp_path, _KrepisLikeProvider())
    handler = root_handler(fd)

    fd._diagnosis_provider = None  # no diagnosis → nothing to scope
    logging.getLogger("krepis.llm").error("llm: a host call failed")
    handler.shutdown(timeout=5)

    assert [r.error_message for r in fd.history(limit=10)] == ["llm: a host call failed"]


def test_own_llm_call_scope_resets_even_when_the_call_raises():
    assert not in_own_llm_call()
    with pytest.raises(ValueError):
        with own_llm_call_scope():
            assert in_own_llm_call()
            raise ValueError("boom")
    assert not in_own_llm_call()

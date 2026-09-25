"""Context-variable plumbing for ``flow_name`` / ``stage`` propagation.

Deep call-stacks shouldn't have to thread ``context={"stage": "..."}``
through every layer to land it on a Flow Doctor report. The contextvars
defined here let consumers wrap a section of work in
``flow_doctor.context(flow_name=..., stage=...)`` and any
``fd.report(...)`` inside auto-picks up those values without explicit
plumbing. ``ContextVar`` is per-task in asyncio and per-thread in sync
code, so the propagation is correct under concurrency.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

_flow_name_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "flow_doctor.flow_name", default=None
)
_stage_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "flow_doctor.stage", default=None
)
# Arbitrary extras for advanced consumers — appears under ``context.extra``
# on the persisted Report alongside ``flow_name``/``stage``.
_extra_var: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "flow_doctor.extra", default=None
)

# True while THIS thread / asyncio task is inside flow-doctor's own LLM call
# (the diagnosis). Read by ``FlowDoctorHandler.emit`` — see
# ``own_llm_call_scope`` for why. Private: not part of the public API.
_own_llm_call_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "flow_doctor.own_llm_call", default=False
)


def current_flow_name() -> Optional[str]:
    return _flow_name_var.get()


def current_stage() -> Optional[str]:
    return _stage_var.get()


def current_extra() -> Optional[Dict[str, Any]]:
    return _extra_var.get()


def current_context() -> Dict[str, Any]:
    """Snapshot the current Flow Doctor contextvars as a dict, suitable
    for merging into a report's ``context`` payload."""
    ctx: Dict[str, Any] = {}
    fn = _flow_name_var.get()
    if fn:
        ctx["flow_name"] = fn
    stg = _stage_var.get()
    if stg:
        ctx["stage"] = stg
    extra = _extra_var.get()
    if extra:
        ctx.update(extra)
    return ctx


@contextmanager
def context(
    *,
    flow_name: Optional[str] = None,
    stage: Optional[str] = None,
    **extra: Any,
) -> Iterator[None]:
    """Push ``flow_name`` / ``stage`` / arbitrary extras onto the
    current execution context for the duration of the ``with`` block.

    Nesting is supported — inner contexts shadow outer ones for the keys
    they specify. Other keys remain visible from the outer scope::

        with flow_doctor.context(flow_name="morning-signal", stage="ingest"):
            run_ingest()
            with flow_doctor.context(stage="rank"):
                run_rank()
                # any fd.report() inside picks up flow_name="morning-signal",
                # stage="rank"
    """
    tokens = []
    if flow_name is not None:
        tokens.append(_flow_name_var.set(flow_name))
    if stage is not None:
        tokens.append(_stage_var.set(stage))
    if extra:
        merged = dict(_extra_var.get() or {})
        merged.update(extra)
        tokens.append(_extra_var.set(merged))
    try:
        yield
    finally:
        # Reset in reverse order so nested ``set`` calls unwind correctly.
        for token in reversed(tokens):
            token.var.reset(token)


def in_own_llm_call() -> bool:
    """Whether the current thread / task is inside :func:`own_llm_call_scope`."""
    return _own_llm_call_var.get()


@contextmanager
def own_llm_call_scope() -> Iterator[None]:
    """Mark the enclosed block as flow-doctor's OWN LLM call.

    ``FlowDoctorHandler`` sits on the host's ROOT logger, so any ERROR the
    LLM client logs while flow-doctor is diagnosing a report is otherwise
    captured as a brand-new report — which is diagnosed, which can log the
    same ERROR again. Observed 2026-09-24 on data-collector: krepis logged
    ``llm: EMPTY message.content ... finish_reason='length'`` from inside the
    diagnosis call and flow-doctor filed it as a new report — a feedback loop
    of noise in which the diagnosis itself was lost.

    The handler's existing self-exclusion is by logger NAME (``flow_doctor.*``)
    and cannot see this: the record comes from ``krepis.llm``, a logger this
    package does not own. Scoping by EXECUTION instead is structural — a
    record emitted on the thread that is running the diagnosis, while it is
    running, was caused by the diagnosis, whatever logger or wording it uses —
    and it never touches a record the host emits on any other thread.

    ``ContextVar`` is per-thread in sync code and per-task under asyncio, so
    a concurrent host thread's ERROR is still captured. Records are only kept
    out of flow-doctor's pipeline; they still reach every other handler (the
    host's stdout / JSON logs). What the diagnosis failure means for the
    report is carried on the report itself (``report.diagnosis_error``).
    """
    token = _own_llm_call_var.set(True)
    try:
        yield
    finally:
        _own_llm_call_var.reset(token)


__all__ = [
    "context",
    "current_context",
    "current_extra",
    "current_flow_name",
    "current_stage",
]

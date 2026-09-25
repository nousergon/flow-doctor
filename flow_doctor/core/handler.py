"""logging.Handler that routes ERROR+ records through Flow Doctor."""

from __future__ import annotations

import logging
import queue
import re
import threading
from typing import TYPE_CHECKING, List, Optional

from flow_doctor.core._context import in_own_llm_call

if TYPE_CHECKING:
    from flow_doctor.core.client import FlowDoctor

_SENTINEL = object()

_SEVERITY_MAP = {
    logging.CRITICAL: "critical",
    logging.ERROR: "error",
    logging.WARNING: "warning",
}


class FlowDoctorHandler(logging.Handler):
    """A logging.Handler that feeds records into Flow Doctor's pipeline.

    Usage::

        fd = FlowDoctor.builder("pipeline").add_notifier(...).build()
        handler = FlowDoctorHandler(fd)
        logging.getLogger().addHandler(handler)
    """

    def __init__(
        self,
        fd: FlowDoctor,
        level: int = logging.ERROR,
        *,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        queue_size: int = 100,
    ):
        super().__init__(level)
        self._fd = fd
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._include_re = [re.compile(p) for p in (include_patterns or [])]
        self._exclude_re = [re.compile(p) for p in (exclude_patterns or [])]

        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Enqueue the record for async processing. Never raises."""
        try:
            # Self-exclusion — structural, not pattern-based. This handler is
            # attached to the ROOT logger (see class docstring), and every
            # flow-doctor module (client.py, notify/*.py) logs to the
            # "flow_doctor" logger, including the CRITICAL a notifier
            # failure raises. Without this check, that CRITICAL is
            # re-captured as a NEW report, which is dispatched, which can
            # fail the same notifier, which logs another CRITICAL — an
            # unbounded loop (alpha-engine-config-I7276: one underlying S3
            # write failure produced "far too many" alerts).
            #
            # This is deliberately record.name, not a default
            # exclude_patterns message regex: message text is caller-
            # controlled prose that drifts (someone rewords a log line and
            # the exclusion silently stops matching); record.name is
            # structural — it names the logger, not what it said, and a
            # child logger like "flow_doctor.notify.s3" is still covered by
            # the startswith check.
            if record.name == "flow_doctor" or record.name.startswith("flow_doctor."):
                return

            # Records emitted BY flow-doctor's own diagnosis LLM call, whatever
            # logger they come from (krepis.llm, openai, httpx). Without this a
            # krepis ``EMPTY message.content`` ERROR raised inside the
            # diagnosis became a NEW report, diagnosed in turn — a loop (see
            # ``flow_doctor.core._context.own_llm_call_scope``).
            if in_own_llm_call():
                return

            msg = record.getMessage()

            # Pattern filtering
            if self._include_re and not any(r.search(msg) for r in self._include_re):
                return
            if any(r.search(msg) for r in self._exclude_re):
                return

            severity = _SEVERITY_MAP.get(record.levelno, "error")

            # Extract exception if present
            exc = None
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]

            item = (exc, msg, severity, record.name, record.pathname, record.lineno)
            self._queue.put_nowait(item)
        except Exception:
            # emit() must never crash
            pass

    def _drain(self) -> None:
        """Background worker that calls fd.report() per queued item."""
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                break
            try:
                exc, msg, severity, logger_name, pathname, lineno = item
                context = {
                    "logger": logger_name,
                    "source": f"{pathname}:{lineno}",
                }
                if exc is not None:
                    self._fd.report(exc, severity=severity, context=context)
                else:
                    self._fd.report(msg, severity=severity, context=context)
            except Exception:
                pass

    def shutdown(self, timeout: float = 5) -> None:
        """Send sentinel and wait for the worker to drain."""
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            return
        self._worker.join(timeout=timeout)

    def close(self) -> None:
        """Shutdown the worker and close the handler."""
        self.shutdown()
        super().close()

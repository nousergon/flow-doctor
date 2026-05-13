"""``RecordingFlowDoctor`` — in-memory test double for downstream tests.

Records every ``report()`` / ``report_async()`` call as a
:class:`ReportedIncident` so consumer test files can make crisp
assertions on the behaviour of their pipelines under failure.

Implements :class:`flow_doctor.FlowDoctorProtocol`, so wherever
production code expects a ``FlowDoctorProtocol`` you can drop in
a ``RecordingFlowDoctor`` and ``mypy --strict`` stays clean.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Dict, Iterator, List, Optional


@dataclass
class ReportedIncident:
    """One captured ``report()`` call. Comparable for ergonomic asserts."""

    error: Any = None
    severity: str = "error"
    context: Optional[Dict[str, Any]] = None
    logs: Optional[str] = None
    message: Optional[str] = None
    exc_type: Optional[str] = None
    exc_message: Optional[str] = None
    # Populated automatically from any ``flow_doctor.context(...)`` scope
    # active when ``report()`` was called.
    ambient_context: Dict[str, Any] = field(default_factory=dict)


class RecordingFlowDoctor:
    """In-memory recorder satisfying :class:`FlowDoctorProtocol`."""

    def __init__(self) -> None:
        self.reports: List[ReportedIncident] = []
        self._id_counter = 0

    # ----- public protocol -------------------------------------------------

    def report(
        self,
        error: Any = None,
        *,
        severity: str = "error",
        context: Optional[Dict[str, Any]] = None,
        logs: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Optional[str]:
        from flow_doctor.core._context import current_context

        exc_type: Optional[str] = None
        exc_message: Optional[str] = None
        if isinstance(error, BaseException):
            exc_type = type(error).__qualname__
            exc_message = str(error)
        incident = ReportedIncident(
            error=error,
            severity=severity,
            context=context,
            logs=logs,
            message=message,
            exc_type=exc_type,
            exc_message=exc_message,
            ambient_context=dict(current_context()),
        )
        self.reports.append(incident)
        self._id_counter += 1
        return f"recorded-{self._id_counter}"

    async def report_async(
        self,
        error: Any = None,
        *,
        severity: str = "error",
        context: Optional[Dict[str, Any]] = None,
        logs: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Optional[str]:
        # No event-loop work to do — recording is in-memory. We keep the
        # method async so the Protocol contract is satisfied and consumer
        # ``await fd.report_async(...)`` calls in tests round-trip cleanly.
        return self.report(
            error,
            severity=severity,
            context=context,
            logs=logs,
            message=message,
        )

    @contextmanager
    def guard(self) -> Iterator[None]:
        try:
            yield
        except Exception as exc:
            self.report(exc)
            raise

    def monitor(self, func: Optional[Callable] = None, **kwargs: Any) -> Any:
        if func is None:

            def decorator(f: Callable) -> Callable:
                return self._wrap_monitor(f)

            return decorator
        return self._wrap_monitor(func)

    def _wrap_monitor(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kw: Any) -> Any:
            try:
                return func(*args, **kw)
            except Exception as exc:
                self.report(exc)
                raise

        return wrapper

    # ----- ergonomic helpers for tests ------------------------------------

    def clear(self) -> None:
        """Reset captured reports — useful for table-driven tests."""
        self.reports.clear()
        self._id_counter = 0

    @property
    def last(self) -> Optional[ReportedIncident]:
        """Most recently captured incident, or None if empty."""
        return self.reports[-1] if self.reports else None

    def of_type(self, exc_type: str) -> List[ReportedIncident]:
        """Filter captured incidents by exception type name."""
        return [r for r in self.reports if r.exc_type == exc_type]


__all__ = ["RecordingFlowDoctor", "ReportedIncident"]

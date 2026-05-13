"""Public ``FlowDoctorProtocol`` contract.

Consumers type-hint against the Protocol rather than the concrete
``FlowDoctor`` class so they can swap in test doubles (e.g.
``RecordingFlowDoctor``) and let ``mypy --strict`` verify the contract.

The Protocol is intentionally tight — it declares only the surface
consumers can rely on across versions. Internal helpers (``status()``,
``digest()``, ``history()``, ``get_handler()``, etc.) are NOT part of
the Protocol so we can evolve them without bumping the major version.
"""

from __future__ import annotations

from typing import (
    Any,
    Awaitable,
    Callable,
    ContextManager,
    Dict,
    Optional,
    Protocol,
    Union,
    runtime_checkable,
)


@runtime_checkable
class FlowDoctorProtocol(Protocol):
    """The cross-version public contract of a flow-doctor instance."""

    def report(
        self,
        error: Union[BaseException, str, None] = None,
        *,
        severity: str = "error",
        context: Optional[Dict[str, Any]] = None,
        logs: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Optional[str]:
        """Report an error or message. Returns a report id, or None
        if suppressed by dedup. Never raises on the caller's behalf."""
        ...

    def guard(self) -> ContextManager[None]:
        """Context manager that reports + re-raises any exception
        raised within its block."""
        ...

    def monitor(self, func: Optional[Callable] = None, **kwargs: Any) -> Any:
        """Decorator form of ``guard()``."""
        ...

    def report_async(
        self,
        error: Union[BaseException, str, None] = None,
        *,
        severity: str = "error",
        context: Optional[Dict[str, Any]] = None,
        logs: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Awaitable[Optional[str]]:
        """Async counterpart of :meth:`report` — fire-and-forget from
        async pipelines without blocking the event loop."""
        ...


__all__ = ["FlowDoctorProtocol"]

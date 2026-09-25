"""Shared krepis-router edge resolution.

Lifted out of ``flow_doctor.diagnosis.provider.RouterProvider`` in 0.15.0 when
``flow_doctor.fix.generator.FixGenerator`` gained its own router path
(alpha-engine-config-I7014): the resolution decision — which routes are
compelled, how a resolution failure fails closed, how exec_context is read —
is made in exactly ONE place rather than reimplemented per call-site. A
second copy of a routing decision is exactly what `model-router-policy.md`
§2 forbids at layer 5.

Both callers speak the router edge's OpenAI-compatible wire
(``krepis.llm.LLMClient`` / ``_call_openai_compat_chat``-shaped transports),
so this module hands back the resolved ``edge_spec`` only — pricing, SFT
capture and the actual completion call stay with each caller, since those
differ (diagnosis JSON contract vs. fix-diff contract; different
``callsite_id``).
"""

from __future__ import annotations

import os
import sys

from flow_doctor.core.constants import LLM_MAX_TOKENS


class RouterUnresolvable(RuntimeError):
    """The configured ``model_group`` could not be resolved to a callable
    endpoint through the krepis router.

    A distinct type (mirrors ``morning_signal.claude.RouterGroupUnresolvable``
    and ``model-router-policy`` R20) so callers never mistake "the router
    could not be reached" for "the router was reached and declined" — this
    always means the LLM call did not happen at all.
    """


#: Routes a call may be served on — the COMPELLED PATHS, per
#: `model-router-policy.md` §5 and mirroring
#: `morning_signal.claude._COMPELLED_ROUTES`. `litellm_proxy` is the
#: authenticated router edge (the normal case). `egress_proxy` is the
#: registry-derived direct route a consumer degrades to when the edge's own
#: health probe fails — still DLP-scanned, still a route the registry
#: declared, so R26 holds. Anything else (a bare `openrouter`/`anthropic`
#: direct route chosen by krepis's own fallback) is refused:
#: alpha-engine-config-I6367 forbids direct-OpenRouter linkage, and the
#: 2026-07-17 ruling sets the direct-Anthropic API budget to $0.
COMPELLED_ROUTES = frozenset({"litellm_proxy", "egress_proxy"})


def resolve_router_edge(
    model_group: str, *, max_tokens: int = LLM_MAX_TOKENS, log_prefix: str = "flow-doctor"
):
    """Resolve ``model_group`` to a callable ``edge_spec`` via krepis.

    Raises :class:`RouterUnresolvable` on any failure — krepis missing, the
    group unresolvable from this execution context, or resolution landing on
    anything outside :data:`COMPELLED_ROUTES` — never returns a partial or
    guessed result, and never falls back to a direct provider or an ambient
    key (R20).
    """
    try:
        from krepis.router import resolve_group_spec
    except ImportError as exc:
        raise RouterUnresolvable(
            f"krepis is not installed, so router group {model_group!r} "
            f"cannot be resolved. Install with: pip install flow-doctor[router]"
        ) from exc

    # Declared, never inferred (model-router-policy R29). None hands the
    # decision to krepis's own documented default rather than guessing here
    # from a hostname or metadata lookup.
    exec_context = os.environ.get("KREPIS_EXEC_CONTEXT") or None
    try:
        edge_spec, route = resolve_group_spec(
            model_group,
            exec_context=exec_context,
            # The router edge speaks OpenAI-compatible chat completions;
            # asking for the anthropic wire would hand back a URL neither
            # caller's transport can speak.
            wire="openai",
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise RouterUnresolvable(
            f"router group {model_group!r} did not resolve "
            f"(exec_context={exec_context!r}): {exc}"
        ) from exc

    resolved_route = route.get("route")
    if resolved_route not in COMPELLED_ROUTES:
        raise RouterUnresolvable(
            f"router group {model_group!r} resolved to route "
            f"{resolved_route!r} (provider={edge_spec.provider!r}), which "
            f"is not a compelled path - refusing to call a direct "
            f"provider chosen by fallback (alpha-engine-config-I6367). "
            f"Compelled routes: {sorted(COMPELLED_ROUTES)}"
        )

    if resolved_route != "litellm_proxy":
        print(
            f"[{log_prefix}] DEGRADED: router group {model_group!r} "
            f"resolved to route {resolved_route!r} (provider="
            f"{edge_spec.provider!r} model={edge_spec.model!r}) rather "
            f"than the authenticated edge — the edge's health probe did "
            f"not pass. This is the registry-derived degraded route "
            f"(model-router-policy §5), still the compelled path.",
            file=sys.stderr,
        )

    return edge_spec

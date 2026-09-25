"""Diagnosis providers: ABC + OpenAI-compatible and krepis-router implementations.

No provider here holds a vendor-specific direct client. ``AnthropicProvider``
was deleted in 0.15.0 (Brian ruling: flow-doctor must not depend on the
``anthropic`` distribution or construct a direct-Anthropic client anywhere —
the fleet's Anthropic account carries a $0 budget, alpha-engine-config-I7460,
and every deployment is now a krepis consumer). The two surviving transports
are provider-neutral: :class:`OpenAICompatProvider` speaks OpenAI-compatible
chat completions to an operator-named ``base_url`` (the path a self-hosted or
external PyPI user takes — no vendor SDK, no default endpoint), and
:class:`RouterProvider` resolves a krepis capability class instead of
addressing any vendor directly.
"""

from __future__ import annotations

import json
import os
import sys
from abc import ABC, abstractmethod
from typing import Optional

from flow_doctor.core.constants import DEFAULT_DIAGNOSIS_MODEL, LLM_MAX_TOKENS
from flow_doctor.core.models import Diagnosis
from flow_doctor.core.router import RouterUnresolvable, resolve_router_edge
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext

# SFT capture (small-model distillation corpus, config#1541). The diagnosis
# task is the third producer surface (after the Vires coach and morning-signal);
# each teacher call is emitted as a canonical krepis SFT v3 record tagged
# producer="flow_doctor_diagnosis" so a mixed-task corpus can be filtered back
# to this single task by producer/model. flow-doctor is MIT and depends only on
# the MIT `krepis` foundation (no AGPL seam) — the same non-AGPL path
# morning-signal uses.
SFT_PRODUCER = "flow_doctor_diagnosis"
# Landing zone when capture is enabled but no explicit sink is configured — the
# `_sft_raw/` convention the corpus tooling expects (config#1541 acceptance).
DEFAULT_SFT_SINK_PATH = "_sft_raw/flow_doctor_diagnosis.sft.jsonl"


class DiagnosisProvider(ABC):
    """Abstract interface for LLM diagnosis providers."""

    @abstractmethod
    def diagnose(self, context: DiagnosisContext, assembler: ContextAssembler) -> Diagnosis:
        """Run diagnosis on the given context. Returns a Diagnosis object."""


def _capture_sft_record(
    *,
    sink_path: Optional[str],
    raw_request: dict,
    raw_response: object,
    text: str,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    meta: dict,
) -> None:
    """Append one diagnosis call to the SFT corpus, if capture is enabled.

    Pure telemetry: a no-op unless the fleet capture switch
    (``LLM_SFT_CAPTURE_ENABLED`` / ``ALPHA_ENGINE_DECISION_CAPTURE_ENABLED``)
    is set truthy, and always best-effort — a capture failure is surfaced on
    stderr but NEVER propagates into the diagnosis result (mirrors the
    Vires-coach / morning-signal capture policy). ``krepis`` is an optional
    dependency (``flow-doctor[sft]``); when the switch is on but the dep is
    missing we say so loudly rather than dropping training data silently.
    """
    try:
        from krepis.llm_capture import capture_enabled

        if not capture_enabled():
            return
    except ImportError:
        # Switch un-checkable without krepis. Only warn if the operator
        # actually asked for capture via the env switch — otherwise stay quiet.
        import os

        if any(
            os.environ.get(v, "").lower() in ("1", "true")
            for v in ("LLM_SFT_CAPTURE_ENABLED", "ALPHA_ENGINE_DECISION_CAPTURE_ENABLED")
        ):
            print(
                "[flow-doctor] WARNING: SFT capture is enabled but the 'krepis' "
                "package is not installed; diagnosis SFT records are being "
                "dropped. Install with: pip install flow-doctor[sft]",
                file=sys.stderr,
            )
        return

    try:
        from krepis.llm import LLMResult, LLMUsage
        from krepis.llm_capture import capture_llm_call

        result = LLMResult(
            text=text,
            model=model,
            provider=provider,
            usage=LLMUsage(input_tokens=int(input_tokens), output_tokens=int(output_tokens)),
            raw_request=raw_request,
            raw_response=raw_response,
        )
        capture_llm_call(
            result,
            producer=SFT_PRODUCER,
            sink_path=sink_path or DEFAULT_SFT_SINK_PATH,
            meta=meta,
            cost_usd=cost_usd,
        )
    except Exception as exc:  # noqa: BLE001 — capture never breaks a diagnosis
        print(
            f"[flow-doctor] WARNING: SFT capture skipped ({exc})",
            file=sys.stderr,
        )


class EmptyDiagnosisResponse(RuntimeError):
    """The diagnosis LLM call succeeded on the wire but returned no content.

    On a reasoning model this is almost always ``finish_reason='length'``:
    the chain of thought consumed the whole ``max_tokens`` budget
    (``flow_doctor.core.constants.LLM_MAX_TOKENS``) and nothing was left to
    answer with. Raised instead of parsing ``''`` into a fabricated
    low-confidence diagnosis; ``FlowDoctor._run_diagnosis`` catches it,
    stamps ``report.diagnosis_error`` and files the report WITHOUT a
    diagnosis — the report is never lost, and never carries a diagnosis that
    was not produced.
    """


def _empty_response_detail(raw_response: object) -> str:
    """``finish_reason`` / token counts off an OpenAI-shaped response, for the
    ``EmptyDiagnosisResponse`` message. Best-effort: any missing field reads
    as ``?`` rather than failing the (already failed) diagnosis a second way.
    """
    try:
        finish = raw_response.choices[0].finish_reason  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - diagnostic detail only
        finish = "?"
    usage = getattr(raw_response, "usage", None)
    completion = getattr(usage, "completion_tokens", "?")
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", "?")
    return (
        f"finish_reason={finish!r} completion_tokens={completion} "
        f"reasoning_tokens={reasoning} max_tokens={LLM_MAX_TOKENS}"
    )


def _raise_if_empty(text: str, raw_response: object, *, model: str) -> None:
    if not text:
        raise EmptyDiagnosisResponse(
            f"diagnosis model {model!r} returned EMPTY content "
            f"({_empty_response_detail(raw_response)}). On a reasoning model "
            f"this means the reasoning trace consumed the whole budget; the "
            f"report is filed without a diagnosis."
        )


# Valid categories for classification
_VALID_CATEGORIES = {"TRANSIENT", "DATA", "CODE", "CONFIG", "EXTERNAL", "INFRA"}


def _parse_llm_json_response(text: str) -> dict:
    """Extract and parse JSON from LLM response text.

    Shared across every diagnosis transport (``OpenAICompatProvider``,
    ``RouterProvider``) — all speak the same JSON diagnosis contract; only
    the transport differs. Lived as ``AnthropicProvider._parse_json`` until
    0.15.0, when the direct-Anthropic transport was removed entirely
    (Brian ruling: flow-doctor must not depend on the ``anthropic``
    distribution or construct a direct-Anthropic client anywhere) and this
    parser — genuinely provider-agnostic — was lifted to module level rather
    than left hanging off the deleted class.
    """
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON block in markdown code fence
    for marker in ("```json", "```"):
        if marker in text:
            start = text.index(marker) + len(marker)
            end = text.index("```", start) if "```" in text[start:] else len(text)
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass

    # Try to find JSON object boundaries
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start:brace_end + 1])
        except json.JSONDecodeError:
            pass

    # Fallback: return minimal dict
    print(f"[flow-doctor] WARNING: Could not parse LLM response as JSON", file=sys.stderr)
    return {"root_cause": text[:500], "category": "CODE", "confidence": 0.3}


def _build_diagnosis(
    parsed: dict,
    text: str,
    context: DiagnosisContext,
    *,
    model: str,
    tokens_used: int,
    cost_usd: float,
    confidence_calibration: float,
) -> Diagnosis:
    """Shared parsed-response → Diagnosis mapping (both providers emit the
    same JSON contract; only the transport differs)."""
    raw_confidence = float(parsed.get("confidence", 0.5))
    calibrated_confidence = raw_confidence * confidence_calibration

    category = parsed.get("category", "CODE").upper()
    if category not in _VALID_CATEGORIES:
        category = "CODE"

    return Diagnosis(
        report_id="",  # Will be set by caller
        flow_name=context.flow_name,
        category=category,
        root_cause=parsed.get("root_cause", text[:500]),
        confidence=calibrated_confidence,
        affected_files=parsed.get("affected_files"),
        remediation=parsed.get("remediation"),
        auto_fixable=parsed.get("auto_fixable", False),
        reasoning=parsed.get("reasoning"),
        alternative_hypotheses=parsed.get("alternative_hypotheses"),
        source="llm",
        llm_model=model,
        tokens_used=tokens_used,
        cost_usd=round(cost_usd, 6),
    )


def _call_openai_compat_chat(
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: int,
    context: DiagnosisContext,
    assembler: ContextAssembler,
    opt_into_usage: bool,
) -> tuple:
    """Shared OpenAI-compatible chat-completions call.

    Both :class:`OpenAICompatProvider` and :class:`RouterProvider` speak the
    same wire format (OpenAI chat-completions) to a different endpoint —
    OpenRouter/OpenAI/vLLM directly for the former, the krepis router edge
    for the latter. Extracted so the request construction, JSON parsing and
    usage extraction can't drift between the two.

    Returns ``(response, text, parsed, input_tokens, output_tokens,
    reported_cost, request_kwargs)``. ``reported_cost`` is ``None`` when the
    endpoint didn't report ``usage.cost`` — callers decide how to price that.
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)

    kwargs = {}
    if opt_into_usage:
        # Opt into usage accounting: usage.cost is the actually-billed USD
        # figure. OpenRouter always supports this; LiteLLM proxies commonly
        # do too (litellm.completion_cost surfaced via the same field).
        kwargs["extra_body"] = {"usage": {"include": True}}

    request_kwargs = dict(
        model=model,
        max_tokens=LLM_MAX_TOKENS,
        messages=[
            {"role": "system", "content": assembler.system_prompt},
            {"role": "user", "content": assembler.build_prompt(context)},
        ],
        **kwargs,
    )
    response = client.chat.completions.create(**request_kwargs)

    text = (response.choices[0].message.content or "").strip()
    _raise_if_empty(text, response, model=model)
    parsed = _parse_llm_json_response(text)

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    reported_cost = getattr(usage, "cost", None)
    reported_cost = float(reported_cost) if reported_cost is not None else None

    return response, text, parsed, input_tokens, output_tokens, reported_cost, request_kwargs


class OpenAICompatProvider(DiagnosisProvider):
    """Diagnosis provider for any OpenAI-compatible chat-completions endpoint
    (OpenRouter for open-weight models, OpenAI itself, self-hosted vLLM).

    Same JSON diagnosis contract as :class:`RouterProvider` — the system
    prompt and response parsing (``_parse_llm_json_response``) are shared;
    only the transport differs.

    Cost accounting (feeds the ``max_daily_cost_usd`` cap, so it must never
    silently under-count): OpenRouter reports the actually-billed USD cost in
    ``usage.cost`` when the request opts in (this provider always opts in on
    an openrouter base_url). For OTHER endpoints, ``price_in_per_1m`` /
    ``price_out_per_1m`` are REQUIRED — construction fails loud rather than
    letting an unpriced provider bill under a blind cap.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        confidence_calibration: float = 0.85,
        timeout_seconds: int = 30,
        price_in_per_1m: Optional[float] = None,
        price_out_per_1m: Optional[float] = None,
        sft_sink_path: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.confidence_calibration = confidence_calibration
        self.timeout_seconds = timeout_seconds
        self.price_in_per_1m = price_in_per_1m
        self.price_out_per_1m = price_out_per_1m
        self.sft_sink_path = sft_sink_path
        if not self._is_openrouter() and (price_in_per_1m is None or price_out_per_1m is None):
            raise ValueError(
                "OpenAICompatProvider: price_in_per_1m + price_out_per_1m are "
                "required for non-OpenRouter endpoints (OpenRouter reports its "
                "own billed cost; other endpoints don't, and an unpriced "
                "provider would bill under a blind max_daily_cost_usd cap)."
            )

    def _is_openrouter(self) -> bool:
        return "openrouter.ai" in (self.base_url or "")

    def diagnose(self, context: DiagnosisContext, assembler: ContextAssembler) -> Diagnosis:
        """Call the OpenAI-compatible endpoint and parse the diagnosis JSON."""
        response, text, parsed, input_tokens, output_tokens, reported_cost, request_kwargs = (
            _call_openai_compat_chat(
                api_key=self.api_key,
                model=self.model,
                base_url=self.base_url,
                timeout_seconds=self.timeout_seconds,
                context=context,
                assembler=assembler,
                opt_into_usage=self._is_openrouter(),
            )
        )

        if reported_cost is not None:
            cost = reported_cost
        elif self.price_in_per_1m is not None and self.price_out_per_1m is not None:
            cost = (
                input_tokens * self.price_in_per_1m / 1_000_000
                + output_tokens * self.price_out_per_1m / 1_000_000
            )
        else:
            # OpenRouter response missing usage.cost (shouldn't happen with the
            # opt-in) — err HIGH rather than free so the daily cap stays honest.
            print(
                "[flow-doctor] WARNING: OpenRouter response carried no usage.cost; "
                "pricing at Opus rates so the daily cost cap errs conservative",
                file=sys.stderr,
            )
            fallback_in, fallback_out = _FALLBACK_PRICES_PER_1M
            cost = (
                input_tokens * fallback_in / 1_000_000
                + output_tokens * fallback_out / 1_000_000
            )

        _capture_sft_record(
            sink_path=self.sft_sink_path,
            raw_request=request_kwargs,
            raw_response=response,
            text=text,
            model=self.model,
            provider="openai_compat",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            meta={
                "provider": "openai_compat",
                "flow_name": context.flow_name,
                "base_url": self.base_url,
            },
        )

        return _build_diagnosis(
            parsed, text, context,
            model=self.model,
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost,
            confidence_calibration=self.confidence_calibration,
        )


# ``RouterUnresolvable`` and the resolution logic below moved to
# ``flow_doctor.core.router`` in 0.15.0 (imported above) so
# ``flow_doctor.fix.generator.FixGenerator``'s own router path
# (alpha-engine-config-I7014) could share them rather than reimplementing the
# compelled-route decision a second time. ``RouterUnresolvable`` is still
# importable from this module — existing imports of
# ``flow_doctor.diagnosis.provider.RouterUnresolvable`` keep working.


class RouterProvider(DiagnosisProvider):
    """Diagnosis provider that resolves a krepis router capability class
    (``low``/``med``/``high``/``ultra``) rather than addressing a model or
    endpoint directly.

    Optional — requires the ``krepis`` package (``pip install
    flow-doctor[router]``), the same optional-dependency shape as the SFT
    capture path above. flow-doctor is a self-hostable OSS tool: a self-hoster
    or external PyPI user with their own OpenAI-compatible endpoint keeps
    using ``OpenAICompatProvider`` exactly as before — no krepis required.
    ``RouterProvider`` exists for deployments that are themselves a krepis
    consumer (Nous Ergon's own flow-doctor installs) and must not hold a
    direct provider key at all — `model-router-policy.md` layer 5: a
    consumer applies the router's resolution contract verbatim and holds no
    routing table of its own.

    Fails closed (R20): any resolution failure — krepis missing, the group
    unresolvable from this execution context, or resolution landing on
    anything outside :data:`flow_doctor.core.router.COMPELLED_ROUTES` — raises
    :class:`RouterUnresolvable` rather than falling back to a direct
    provider or an ambient key. There is no fallback chain here the way
    ``morning_signal`` has one; a diagnosis call skipped this cycle is a
    contained failure, so fail-closed is strictly correct with no cost.
    """

    def __init__(
        self,
        model_group: str,
        confidence_calibration: float = 0.85,
        timeout_seconds: int = 30,
        sft_sink_path: Optional[str] = None,
    ):
        self.model_group = model_group
        self.confidence_calibration = confidence_calibration
        self.timeout_seconds = timeout_seconds
        self.sft_sink_path = sft_sink_path

    def _resolve(self):
        """Resolve ``self.model_group`` to a callable edge spec via krepis.

        Thin wrapper over the shared :func:`flow_doctor.core.router
        .resolve_router_edge` (lifted out in 0.15.0 so this decision is made
        in one place — see the module-level note above). Raises
        :class:`RouterUnresolvable` on any failure — never returns a partial
        or guessed result.
        """
        return resolve_router_edge(self.model_group, max_tokens=LLM_MAX_TOKENS, log_prefix="flow-doctor")

    def diagnose(self, context: DiagnosisContext, assembler: ContextAssembler) -> Diagnosis:
        """Resolve the router group, then call the resolved endpoint.

        Uses ``krepis.llm.LLMClient`` rather than a hand-rolled OpenAI
        client: it owns the router-edge credential chain (env var, then the
        per-consumer SSM secret — ``_resolve_api_key`` in ``krepis.llm``),
        which this provider must not reimplement, and ``krepis.cost
        .record_llm_call`` prices the response off the registry's own price
        card when the edge doesn't report ``usage.cost`` — correct for a
        route that can resolve to any of several models, where no single
        static price is knowable ahead of the call.
        """
        edge_spec = self._resolve()
        from krepis.llm import LLMClient

        client = LLMClient(
            edge_spec,
            callsite_id="flow_doctor_diagnosis",
            timeout=float(self.timeout_seconds),
            max_retries=0,
        )
        result = client.complete(
            system=assembler.system_prompt,
            user_content=assembler.build_prompt(context),
            max_tokens=LLM_MAX_TOKENS,
            cache_system=True,
            on_unsupported="drop",
        )

        text = (result.text or "").strip()

        from krepis.cost import record_llm_call

        # Recorded BEFORE the empty-content check: an exhausted reasoning
        # budget is fully billed, and spend that never reaches the cost
        # record is spend every budget guard reading it under-counts.
        cost_record = record_llm_call(result, extra_fields={"callsite_id": "flow_doctor_diagnosis"})
        _raise_if_empty(text, result.raw_response, model=result.model)
        parsed = _parse_llm_json_response(text)
        cost = float(cost_record.get("cost_usd") or 0.0)
        input_tokens = int(getattr(result.usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(result.usage, "output_tokens", 0) or 0)

        _capture_sft_record(
            sink_path=self.sft_sink_path,
            raw_request=result.raw_request,
            raw_response=result.raw_response,
            text=text,
            model=result.model,
            provider="router",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            meta={
                "provider": "router",
                "model_group": self.model_group,
                "flow_name": context.flow_name,
                "cost_source": cost_record.get("cost_source"),
            },
        )

        return _build_diagnosis(
            parsed, text, context,
            model=result.model,
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost,
            confidence_calibration=self.confidence_calibration,
        )

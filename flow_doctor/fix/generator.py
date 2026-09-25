"""Fix generator: calls LLM to produce a unified diff from a diagnosis."""

from __future__ import annotations

import sys
from typing import Dict, List, Optional

from flow_doctor.core.constants import DEFAULT_DIAGNOSIS_MODEL, LLM_MAX_TOKENS
from flow_doctor.core.router import resolve_router_edge
from flow_doctor.fix.prompts import SYSTEM_PROMPT, build_fix_prompt

#: Accepted ``provider`` values. ``"anthropic"`` was REMOVED in 0.15.0 — the
#: fleet's Anthropic account carries a $0 budget (alpha-engine-config-I7460)
#: and every deployment is now a krepis consumer, so the direct-Anthropic
#: transport was dead weight that could still be silently reached by a config
#: that simply omitted ``provider``. Deleting the branch (rather than
#: deprecating it) means a config still naming ``anthropic`` fails loudly
#: instead of quietly calling an unfunded account — the same precedent
#: crucible-research-PR606 set.
_VALID_PROVIDERS = ("openai_compat", "router")


class FixGenerator:
    """Generates fix diffs using an LLM.

    ``provider`` mirrors ``DiagnosisConfig.provider`` and, like it, has NO
    DEFAULT (0.15.0 — Brian ruling: flow-doctor must not depend on the
    ``anthropic`` distribution or construct a direct-Anthropic client
    anywhere, and more generally must not pick a vendor for the caller). It
    is a required keyword argument: ``"openai_compat"`` (any OpenAI-compatible
    chat-completions endpoint — your router, OpenAI, a self-hosted vLLM, an
    inference vendor — named by ``base_url``) or ``"router"`` (resolves a
    krepis router capability class — ``model_group`` — instead of holding a
    direct provider key; requires the optional ``krepis`` package, ``pip
    install flow-doctor[router]``). Both transports run the same prompts and
    return the same plain-text diff contract. ``"anthropic"`` is no longer
    accepted — see ``_VALID_PROVIDERS``.

    ``base_url`` has no default. It carried a hardcoded OpenRouter API URL
    until 0.13.0, which meant an ``openai_compat`` generator constructed
    without one shipped the contents of the affected source files — the
    largest payload flow-doctor sends anywhere — to a vendor the caller never
    named. An unset endpoint now fails closed at call time.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_DIAGNOSIS_MODEL,
        timeout_seconds: int = 60,
        base_url: Optional[str] = None,
        model_group: Optional[str] = None,
        *,
        provider: str,
    ):
        if provider not in _VALID_PROVIDERS:
            hint = (
                " 'anthropic' was removed in 0.15.0 — set provider='router' "
                "with model_group (a krepis capability class: low/med/high/"
                "ultra), or provider='openai_compat' with base_url."
                if provider == "anthropic"
                else ""
            )
            raise ValueError(
                f"FixGenerator provider must be one of {_VALID_PROVIDERS}, "
                f"got '{provider}'.{hint}"
            )
        if provider == "router" and not model_group:
            raise ValueError(
                "FixGenerator(provider='router') requires model_group (one "
                "of: low, med, high, ultra)."
            )
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.provider = provider
        self.base_url = base_url
        self.model_group = model_group

    def _complete_router(self, user_prompt: str) -> str:
        """Resolve ``model_group`` through krepis and call the resolved edge.

        Mirrors ``flow_doctor.diagnosis.provider.RouterProvider.diagnose``:
        same lazy krepis import (via the shared
        ``flow_doctor.core.router.resolve_router_edge``), same
        ``RouterUnresolvable`` fail-closed posture, same compelled-route
        allowlist, same cost recording via ``krepis.cost.record_llm_call``.
        Uses ``krepis.llm.LLMClient`` rather than a hand-rolled OpenAI client
        so it inherits the router-edge credential chain (env var, then the
        per-consumer SSM secret) instead of reimplementing it.
        """
        edge_spec = resolve_router_edge(
            self.model_group, max_tokens=LLM_MAX_TOKENS, log_prefix="flow-doctor-fix"
        )
        from krepis.llm import LLMClient

        client = LLMClient(
            edge_spec,
            callsite_id="flow_doctor_fix_generation",
            timeout=float(self.timeout_seconds),
            max_retries=0,
        )
        result = client.complete(
            system=SYSTEM_PROMPT,
            user_content=user_prompt,
            max_tokens=LLM_MAX_TOKENS,
            cache_system=True,
            on_unsupported="drop",
        )

        from krepis.cost import record_llm_call

        record_llm_call(result, extra_fields={"callsite_id": "flow_doctor_fix_generation"})

        return result.text

    def _complete_openai_compat(self, user_prompt: str) -> str:
        # Checked BEFORE importing the transport: the config is invalid whether
        # or not the optional `openai` extra is installed, and a guard that
        # needs the dependency in order to refuse cannot run in the
        # environment least likely to have it.
        if not self.base_url:
            # Fail closed. The openai SDK's own default points at OpenAI's API,
            # so an unset base_url would not "do nothing" — it would send the
            # caller's source files somewhere they never named.
            raise ValueError(
                "FixGenerator(provider='openai_compat') has no base_url. Set "
                "diagnosis.base_url to the endpoint you want. flow-doctor "
                "ships no default endpoint."
            )

        from openai import OpenAI

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )
        response = client.chat.completions.create(
            model=self.model,
            max_tokens=LLM_MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""

    def generate(
        self,
        category: str,
        root_cause: str,
        confidence: float,
        remediation: Optional[str],
        affected_files: List[str],
        file_contents: Dict[str, str],
        test_contents: Dict[str, str],
        prior_rejections: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Generate a unified diff for the fix.

        Returns:
            The unified diff string, or None if the LLM cannot produce a fix.
        """
        user_prompt = build_fix_prompt(
            category=category,
            root_cause=root_cause,
            confidence=confidence,
            remediation=remediation,
            affected_files=affected_files,
            file_contents=file_contents,
            test_contents=test_contents,
            prior_rejections=prior_rejections,
        )

        if self.provider == "router":
            text = self._complete_router(user_prompt)
        else:
            text = self._complete_openai_compat(user_prompt)

        text = text.strip()

        if text == "NO_FIX":
            return None

        # Strip markdown fences if LLM wrapped them
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```diff or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines)

        return text

    @staticmethod
    def extract_files_from_diff(diff: str) -> List[str]:
        """Extract file paths from a unified diff."""
        files = []
        for line in diff.split("\n"):
            if line.startswith("+++ b/"):
                path = line[6:]
                if path and path != "/dev/null":
                    files.append(path)
        return files

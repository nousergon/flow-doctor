"""Shared constants for flow-doctor.

Single source of truth for values that were previously duplicated as bare
literals across modules and drifted out of sync.
"""

from __future__ import annotations

# Canonical Anthropic model id for the LLM diagnosis / fix-generation layer.
#
# This is the ONE place the default model is defined. Every provider/config
# default imports it from here so the id can never silently diverge again.
#
# History: the default used to be hardcoded as ``"claude-sonnet-4-6-20250514"``
# in five separate modules. That id is malformed — the ``20250514`` snapshot
# belongs to the Sonnet-4 generation, not the Sonnet-4.6 family it was paired
# with — so the Anthropic API rejected it with a 404 and the LLM-diagnosis
# layer was silently dead fleet-wide (see config#1370). The valid id for the
# current Sonnet 4.6 release is the unsuffixed alias below.
DEFAULT_DIAGNOSIS_MODEL = "claude-sonnet-4-6"

# The ``max_tokens`` ceiling on every LLM call flow-doctor makes — diagnosis
# (both transports) and fix generation (both transports). ONE constant, so no
# call site can be left behind at a stale literal when the number moves.
#
# Why 16000 and not the 2048 / 4096 literals it replaces: on a REASONING model
# ``max_tokens`` bounds the chain of thought AND the answer together, so a
# budget sized to the expected answer returns a fully-billed EMPTY completion
# (``finish_reason='length'``, ``content=''``). The router groups flow-doctor
# resolves (``low`` in the fleet) can land on a reasoning model; on
# 2026-09-24 data-collector's diagnosis drew ``reasoning_tokens=2048`` against
# ``max_tokens=2048`` and answered nothing.
#
# The number is the one the fleet already measured for the same group, not a
# new guess (alpha-engine-config-I8700 / I6917 / I6901): the ``low`` group's
# p99 reasoning draw was 13089 tokens and a p95 answer ~1105, i.e. 14194 needed;
# the Think Tank ``sweep`` tier on ``low`` was raised to 16000 on that
# measurement (the doubling krepis applies on a proven exhaustion, pre-applied).
# That measurement is CENSORED at its old ceiling — a successful call cannot
# report a draw the ceiling refused — so it is a floor, never a guarantee.
# A ceiling is billed only when drawn. Stays under the common 16384 output cap
# of non-reasoning OpenAI-compatible models, so the openai_compat path does not
# start being refused for asking for more than the model can emit.
#
# ``krepis.llm.LLMClient.complete()`` (which both router paths call) does NOT
# escalate on exhaustion the way ``structured()`` does, so this ceiling is the
# only budget those calls get. If a diagnosis still comes back empty, the
# provider raises ``EmptyDiagnosisResponse`` and the report is filed without a
# diagnosis (``report.diagnosis_error`` names why) — never a junk diagnosis
# parsed out of an empty string.
LLM_MAX_TOKENS = 16000

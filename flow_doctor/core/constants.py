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

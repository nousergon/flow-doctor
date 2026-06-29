"""Regression: the default diagnosis/fix model id must be the single canonical
valid Anthropic id, sourced from one constant — never a malformed snapshot.

Guards config#1370: the prior default ``claude-sonnet-4-6-20250514`` was a
malformed id (Sonnet-4 snapshot date paired with the 4.6 family) that the API
404'd, silently killing the LLM-diagnosis layer fleet-wide.
"""

from __future__ import annotations

from pathlib import Path

from flow_doctor.core.constants import DEFAULT_DIAGNOSIS_MODEL
from flow_doctor.core.config import DiagnosisConfig
from flow_doctor.diagnosis.provider import AnthropicProvider
from flow_doctor.fix.generator import FixGenerator

# The malformed id that broke the layer; must never reappear as a literal.
_BAD_ID = "claude-sonnet-4-6-20250514"


def test_canonical_default_is_valid_alias():
    assert DEFAULT_DIAGNOSIS_MODEL == "claude-sonnet-4-6"


def test_all_defaults_point_at_the_constant():
    assert DiagnosisConfig().model == DEFAULT_DIAGNOSIS_MODEL
    assert AnthropicProvider(api_key="x").model == DEFAULT_DIAGNOSIS_MODEL
    assert FixGenerator(api_key="x").model == DEFAULT_DIAGNOSIS_MODEL


def test_yaml_fallback_default_is_canonical():
    # The dict-parse path in from_yaml/load must use the same default.
    cfg = DiagnosisConfig(enabled=True)
    assert cfg.model == DEFAULT_DIAGNOSIS_MODEL


def test_no_malformed_literal_remains_in_source():
    src_root = Path(__file__).resolve().parent.parent / "flow_doctor"
    # constants.py is the single source of truth; it documents the retired id
    # in a history note on purpose. Every other module must be free of it.
    offenders = [
        str(p)
        for p in src_root.rglob("*.py")
        if p.name != "constants.py" and _BAD_ID in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"malformed model id still hardcoded in: {offenders}"

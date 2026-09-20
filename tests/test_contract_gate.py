"""CLI contract gate smoke tests for Stage B (Task T6)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from herbenzo.contract_gate import (
    validate_inbound_formulation_spec,
    validate_outbound_modernized_sku,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ashwagandha.json"


def test_inbound_example_ok():
    raw = json.loads(EXAMPLE.read_text())
    spec = validate_inbound_formulation_spec(raw)
    assert spec.formulation_id == "F-ASHW-001"


def test_inbound_rejects_unknown():
    raw = json.loads(EXAMPLE.read_text())
    raw["not_a_field"] = True
    with pytest.raises(ValidationError):
        validate_inbound_formulation_spec(raw)


def test_inbound_rejects_raised_floor_when_inherited_set():
    raw = json.loads(EXAMPLE.read_text())
    raw["inherited_confidence"] = 0.5
    raw["confidence"] = 0.9
    with pytest.raises(ValidationError):
        validate_inbound_formulation_spec(raw)

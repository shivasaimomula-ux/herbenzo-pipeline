"""CLI contract gate smoke tests for Stage B (Task T6 / T22)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from herbenzo.components.modernizer.modernizer import ModernizerEngine
from herbenzo.contract_gate import (
    attach_provenance_thread,
    to_engine_payload,
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


def test_engine_payload_strips_provenance_thread():
    raw = json.loads(EXAMPLE.read_text())
    raw["source_spec_id"] = "s1"
    raw["provenance_thread"] = {
        "schema_version": "1.0.0",
        "spec_id": "s1",
        "stages": ["F", "A"],
    }
    spec = validate_inbound_formulation_spec(raw)
    engine = to_engine_payload(spec)
    assert "provenance_thread" not in engine
    assert "source_spec_id" not in engine
    assert "schema_version" not in engine


def test_attach_provenance_thread_adds_sku():
    raw = json.loads(EXAMPLE.read_text())
    raw["source_spec_id"] = "s1"
    raw["provenance_thread"] = {
        "schema_version": "1.0.0",
        "spec_id": "s1",
        "formulation_id": raw["formulation_id"],
        "stages": ["A"],
    }
    spec = validate_inbound_formulation_spec(raw)
    sku_local = ModernizerEngine().modernize(to_engine_payload(spec))
    outbound = validate_outbound_modernized_sku(sku_local)
    with_thread = attach_provenance_thread(spec, outbound)
    assert with_thread.provenance_thread is not None
    assert with_thread.provenance_thread.spec_id == "s1"
    assert with_thread.provenance_thread.sku_id == with_thread.sku_id
    assert "B" in with_thread.provenance_thread.stages

"""CLI/HTTP contract gates for Stage B (FormulationSpec → ModernizedSKU)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from herbenzo_contracts import FormulationSpec, ModernizedSKU, validation_error_body

# Shared FormulationSpec fields the local engine schema does not accept (extra=forbid).
_ENGINE_EXCLUDE = frozenset({"schema_version", "inherited_confidence", "source_spec_id"})


def validate_inbound_formulation_spec(payload: dict[str, Any] | FormulationSpec) -> FormulationSpec:
    if isinstance(payload, FormulationSpec):
        return payload
    return FormulationSpec.model_validate(payload)


def to_engine_payload(spec: FormulationSpec) -> dict[str, Any]:
    """Dump shared FormulationSpec into a dict the local ModernizerEngine accepts."""
    data = spec.model_dump(mode="json")
    for key in _ENGINE_EXCLUDE:
        data.pop(key, None)
    return data


def validate_outbound_modernized_sku(payload: dict[str, Any] | Any) -> ModernizedSKU:
    # Local B ModernizedSKU is a distinct class — always re-validate via JSON dump.
    if hasattr(payload, "model_dump"):
        return ModernizedSKU.model_validate(payload.model_dump(mode="json"))
    return ModernizedSKU.model_validate(payload)


def format_cli_error(exc: BaseException) -> str:
    body = validation_error_body(exc)
    return f"contract_validation: {body.get('message')}\n{body.get('details') or body}"


def http_error_detail(exc: BaseException, *, code: str = "contract_validation") -> dict[str, Any]:
    """validation_error_body made JSON-serializable for FastAPI HTTPException detail."""
    body = validation_error_body(exc, code=code)
    return json.loads(json.dumps(body, default=str))

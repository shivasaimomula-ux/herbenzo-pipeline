"""CLI/HTTP contract gates for Stage B (FormulationSpec → ModernizedSKU)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from herbenzo_contracts import FormulationSpec, ModernizedSKU, validation_error_body
from herbenzo_contracts.provenance import extend_from_handoff

# Shared FormulationSpec fields the local engine schema does not accept (extra=forbid).
_ENGINE_EXCLUDE = frozenset(
    {"schema_version", "inherited_confidence", "source_spec_id", "provenance_thread"}
)


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


def attach_provenance_thread(spec: FormulationSpec, sku: ModernizedSKU) -> ModernizedSKU:
    """Echo upstream IDs and append sku_id on the outbound ModernizedSKU (T22)."""
    thread = extend_from_handoff(
        spec.provenance_thread,
        stage="B",
        payload={
            "spec_id": spec.source_spec_id,
            "formulation_id": spec.formulation_id,
            "sku_id": sku.sku_id,
        },
        sku_id=sku.sku_id,
        formulation_id=spec.formulation_id,
        spec_id=spec.source_spec_id,
    )
    return sku.model_copy(update={"provenance_thread": thread})


# Pipeline-local annotations. The shared ModernizedSKU has no field for a
# pending marker, so they are removed before that gate. A pending ingredient
# still fails the gate because its descriptor block is empty on purpose.
_LOCAL_ANNOTATIONS = ("marker_status", "standardization")


def _strip_local_annotations(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(data))
    for ing in cleaned.get("ingredients") or []:
        if not isinstance(ing, dict):
            continue
        ing.pop("marker_status", None)
        for block in ("marker", "bcs", "delivery"):
            node = ing.get(block)
            if isinstance(node, dict):
                for key in _LOCAL_ANNOTATIONS:
                    node.pop(key, None)
    return cleaned


def validate_outbound_modernized_sku(payload: dict[str, Any] | Any) -> ModernizedSKU:
    # Local B ModernizedSKU is a distinct class — always re-validate via JSON dump.
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    return ModernizedSKU.model_validate(_strip_local_annotations(data))


def format_cli_error(exc: BaseException) -> str:
    body = validation_error_body(exc)
    return f"contract_validation: {body.get('message')}\n{body.get('details') or body}"


def http_error_detail(exc: BaseException, *, code: str = "contract_validation") -> dict[str, Any]:
    """validation_error_body made JSON-serializable for FastAPI HTTPException detail."""
    body = validation_error_body(exc, code=code)
    return json.loads(json.dumps(body, default=str))

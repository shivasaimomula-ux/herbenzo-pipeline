"""FastAPI service for Stage B (Modernizer).

Run:
  uvicorn herbenzo.api:app --host 0.0.0.0 --port 8003

Port 8003 is the Stage B contract. Independent B UI is Task T10 — this service
exposes health + modernize only (optional health JSON at ``/``).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from herbenzo.components.modernizer.modernizer import ENGINE_VERSION, ModernizerEngine
from herbenzo.contract_gate import (
    http_error_detail,
    to_engine_payload,
    validate_inbound_formulation_spec,
    validate_outbound_modernized_sku,
)
from herbenzo.services.registries import UnknownIngredient, UnknownMarker
from herbenzo_contracts import CONTRACT_SCHEMA_VERSION

app = FastAPI(
    title="Herbenzo Modernizer (Stage B)",
    version="1.0.0",
    description=(
        "Deterministic FormulationSpec → ModernizedSKU service. "
        "No LLM. Independent UI lands in Task T10."
    ),
)

_ENGINE = ModernizerEngine()


def _health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "stage": "B",
        "service": "herbenzo-pipeline",
        "engine_version": ENGINE_VERSION,
        "contracts": "herbenzo-contracts",
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "port_contract": 8003,
        "llm": False,
        "ui": "deferred-to-T10",
        "endpoints": {"health": "/health", "modernize": "POST /modernize"},
    }


@app.get("/")
def root():
    """Minimal health JSON page (not a full UI — see Task T10)."""
    return _health_payload()


@app.get("/health")
def health():
    return _health_payload()


@app.post("/modernize")
async def modernize(request: Request):
    """Validate FormulationSpec → ModernizerEngine.modernize → ModernizedSKU."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=http_error_detail(ValueError("Request body must be JSON")),
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail=http_error_detail(ValueError("Request body must be a JSON object")),
        )

    try:
        spec = validate_inbound_formulation_spec(payload)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(exc)) from exc

    try:
        # Engine uses local schemas; strip shared-only fields at the boundary.
        sku = _ENGINE.modernize(to_engine_payload(spec))
        outbound = validate_outbound_modernized_sku(sku)
    except (UnknownIngredient, UnknownMarker) as exc:
        raise HTTPException(
            status_code=422,
            detail=http_error_detail(exc, code="unknown_ingredient"),
        ) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(exc)) from exc

    return JSONResponse(content=outbound.model_dump(mode="json"))

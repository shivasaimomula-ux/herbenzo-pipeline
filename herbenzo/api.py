"""FastAPI service for Stage B (Modernizer).

Run:
  uvicorn herbenzo.api:app --host 0.0.0.0 --port 8003

Port 8003 is the Stage B contract. Independent B UI is served at ``/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from herbenzo.components.modernizer.modernizer import ENGINE_VERSION, ModernizerEngine
from herbenzo.contract_gate import (
    attach_provenance_thread,
    http_error_detail,
    to_engine_payload,
    validate_inbound_formulation_spec,
    validate_outbound_modernized_sku,
)
from herbenzo.services.registries import UnknownIngredient, UnknownMarker
from herbenzo_contracts import CONTRACT_SCHEMA_VERSION

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Herbenzo Modernizer (Stage B)",
    version="1.0.0",
    description=(
        "Deterministic FormulationSpec → ModernizedSKU service. "
        "No LLM. Independent UI at GET /."
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
        "ui": "available",
        "endpoints": {
            "ui": "/",
            "health": "/health",
            "modernize": "POST /modernize",
            "static": "/static/",
        },
    }


@app.get("/")
def root():
    """Independent Stage B modernize UI (Task T10)."""
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=500, detail="B UI index.html missing")
    return FileResponse(index, media_type="text/html; charset=utf-8")


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
        outbound = attach_provenance_thread(spec, outbound)
    except (UnknownIngredient, UnknownMarker) as exc:
        raise HTTPException(
            status_code=422,
            detail=http_error_detail(exc, code="unknown_ingredient"),
        ) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(exc)) from exc

    return JSONResponse(content=outbound.model_dump(mode="json"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

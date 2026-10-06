"""FastAPI service for Stage B (Modernizer).

Run:
  uvicorn herbenzo.api:app --host 0.0.0.0 --port 8003

Port 8003 is the Stage B contract. Independent B UI is served at ``/``.
Compose drafts are JSON files under ``herbenzo/data/compose_drafts``
(override with ``HERBENZO_COMPOSE_DRAFTS_DIR``). They are UI scaffolding only.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

from herbenzo.config import get_settings, load_project_env

load_project_env()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from herbenzo.components.modernizer.modernizer import ENGINE_VERSION, ModernizerEngine
from herbenzo.enrich_api import router as enrich_router
from herbenzo.contract_gate import (
    attach_provenance_thread,
    http_error_detail,
    to_engine_payload,
    validate_inbound_formulation_spec,
    validate_outbound_modernized_sku,
)
from herbenzo.format_suggestions import suggest_from_payload
from herbenzo.services.registries import (
    UnknownIngredient,
    UnknownMarker,
    iter_registry_records,
    merged_ingredient_ids,
)
from herbenzo_contracts import CONTRACT_SCHEMA_VERSION

STATIC_DIR = Path(__file__).resolve().parent / "static"
_DEFAULT_DRAFTS_DIR = Path(__file__).resolve().parent / "data" / "compose_drafts"
_DRAFT_ID = re.compile(r"^d[a-f0-9]{16}$")
_DRAFT_LOCK = threading.Lock()
_MAX_DRAFT_NAME = 120

app = FastAPI(
    title="Herbenzo Modernizer (Stage B)",
    version="1.0.0",
    description=(
        "Deterministic FormulationSpec → ModernizedSKU service. "
        "The modernize path does not call an LLM. Independent UI at GET /."
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
            "ingredients": "GET /ingredients",
            "suggest_formats": "POST /suggest-formats",
            "drafts": "GET/POST /drafts",
            "draft": "GET/DELETE /drafts/{id}",
            "static": "/static/",
            "enrich_propose": "POST /enrich/propose",
            "enrich_candidates": "GET /enrich/candidates",
            "enrich_candidate": "GET /enrich/candidates/{id}",
            "enrich_approve": "POST /enrich/candidates/{id}/approve",
            "enrich_reject": "POST /enrich/candidates/{id}/reject",
        },
        "enrichment_llm": get_settings().llm_available,
    }


def _drafts_dir() -> Path:
    raw = os.environ.get("HERBENZO_COMPOSE_DRAFTS_DIR")
    if raw:
        return Path(raw).expanduser()
    return _DEFAULT_DRAFTS_DIR


def _check_draft_id(draft_id: str) -> str:
    if not _DRAFT_ID.fullmatch(draft_id or ""):
        raise HTTPException(status_code=422, detail="Draft id is invalid")
    return draft_id


def _draft_path(draft_id: str) -> Path:
    directory = _drafts_dir().resolve()
    path = (directory / f"{draft_id}.json").resolve()
    if path.parent != directory:
        raise HTTPException(status_code=422, detail="Draft id is invalid")
    return path


def _spec_is_complete(spec: dict[str, Any]) -> bool:
    """True when the stored object can be posted to /modernize.

    Schema-valid and every ingredient id is in the stock registry. Unknown ids
    still 422 from /modernize; those drafts stay incomplete.
    """
    try:
        parsed = validate_inbound_formulation_spec(spec)
    except (ValidationError, ValueError, TypeError):
        return False
    known = merged_ingredient_ids()
    return all(ing.ingredient_id in known for ing in parsed.ingredients)


def _ingredient_count(spec: Any) -> int:
    if isinstance(spec, dict) and isinstance(spec.get("ingredients"), list):
        return len(spec["ingredients"])
    return 0


def _read_draft_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _public_draft(doc: dict[str, Any]) -> dict[str, Any]:
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    return {
        "id": doc.get("id"),
        "name": doc.get("name"),
        "saved_at": doc.get("saved_at"),
        "updated_at": doc.get("updated_at"),
        "complete": _spec_is_complete(spec),
        "ingredient_count": _ingredient_count(spec),
        "spec": spec,
    }


def _summary_draft(doc: dict[str, Any]) -> dict[str, Any]:
    full = _public_draft(doc)
    full.pop("spec", None)
    return full


def _write_draft(doc: dict[str, Any]) -> None:
    directory = _drafts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _draft_path(str(doc["id"]))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _new_draft_id() -> str:
    directory = _drafts_dir()
    for _ in range(5):
        draft_id = "d" + token_hex(8)
        if not (directory / f"{draft_id}.json").exists():
            return draft_id
    raise HTTPException(status_code=500, detail="Could not allocate a draft id")


def _parse_draft_body(payload: Any) -> tuple[str, dict[str, Any], str | None]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Draft body must be a JSON object")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="Draft name is required")
    name = name.strip()
    if len(name) > _MAX_DRAFT_NAME:
        raise HTTPException(
            status_code=422,
            detail=f"Draft name must be {_MAX_DRAFT_NAME} characters or fewer",
        )
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        raise HTTPException(status_code=422, detail="Draft spec must be a JSON object")
    draft_id = payload.get("id", None)
    if draft_id is not None:
        if not isinstance(draft_id, str):
            raise HTTPException(status_code=422, detail="Draft id is invalid")
        _check_draft_id(draft_id)
    return name, spec, draft_id


def _ingredient_row(rec: Any) -> dict[str, Any]:
    return {
        "ingredient_id": rec.ingredient_id,
        "botanical_name": rec.botanical_name,
        "common_name": rec.common_name,
        "sanskrit_name": rec.sanskrit_name,
        "synonyms": list(rec.synonyms),
        "part_used": rec.part_used,
        "markers": [
            {"marker_name": marker.marker_name, "rationale": marker.rationale}
            for marker in rec.markers
        ],
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


@app.get("/ingredients")
def ingredients():
    """Stock rows plus approved overlay rows. Pending enrichment candidates are omitted."""
    return {"ingredients": [_ingredient_row(rec) for rec in iter_registry_records()]}


@app.get("/drafts")
def list_drafts():
    """Named in-progress FormulationSpecs. Incomplete drafts are included."""
    directory = _drafts_dir()
    docs: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in directory.glob("d*.json"):
            if not _DRAFT_ID.fullmatch(path.stem):
                continue
            doc = _read_draft_file(path)
            if doc is None or doc.get("id") != path.stem:
                continue
            docs.append(doc)
    docs.sort(key=lambda doc: str(doc.get("updated_at") or ""), reverse=True)
    return {"drafts": [_summary_draft(doc) for doc in docs]}


@app.get("/drafts/{draft_id}")
def get_draft(draft_id: str):
    _check_draft_id(draft_id)
    doc = _read_draft_file(_draft_path(draft_id))
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No draft {draft_id}")
    return _public_draft(doc)


@app.post("/drafts")
async def save_draft(request: Request):
    """Create a named draft, or update one when ``id`` is an existing draft.

    ``spec`` is stored as sent. It does not have to be a complete FormulationSpec.
    ``complete`` is true only when that object would pass ``POST /modernize``.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Draft body must be JSON") from exc

    name, spec, draft_id = _parse_draft_body(payload)
    now = datetime.now(UTC).isoformat()
    complete = _spec_is_complete(spec)

    with _DRAFT_LOCK:
        if draft_id is None:
            draft_id = _new_draft_id()
            doc = {
                "id": draft_id,
                "name": name,
                "saved_at": now,
                "updated_at": now,
                "complete": complete,
                "spec": spec,
            }
            _write_draft(doc)
            status = 201
        else:
            existing = _read_draft_file(_draft_path(draft_id))
            if existing is None:
                raise HTTPException(status_code=404, detail=f"No draft {draft_id}")
            doc = {
                "id": draft_id,
                "name": name,
                "saved_at": existing.get("saved_at") or now,
                "updated_at": now,
                "complete": complete,
                "spec": spec,
            }
            _write_draft(doc)
            status = 200
    return JSONResponse(status_code=status, content=_public_draft(doc))


@app.delete("/drafts/{draft_id}")
def delete_draft(draft_id: str):
    _check_draft_id(draft_id)
    path = _draft_path(draft_id)
    with _DRAFT_LOCK:
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"No draft {draft_id}")
        path.unlink()
    return {"deleted": True, "id": draft_id}


@app.post("/suggest-formats")
async def suggest_formats(request: Request):
    """Advisory finished-format ranking for one or more ingredient ids.

    Does not run the modernizer and does not change a ModernizedSKU. Unknown
    registry ids return 422 ``unknown_ingredient``, same as ``/modernize``.
    """
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
        return suggest_from_payload(payload)
    except (UnknownIngredient, UnknownMarker) as exc:
        raise HTTPException(
            status_code=422,
            detail=http_error_detail(exc, code="unknown_ingredient"),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(exc)) from exc


@app.post("/modernize")
async def modernize(request: Request):
    """Validate FormulationSpec → ModernizerEngine.modernize → ModernizedSKU.

    A classical preparation whose registry row has no active marker does not
    422. When some ingredients still have markers, the body is that
    ModernizedSKU plus ``classical_active_marker_gap``. When none do, the body
    is ``{"sku": null, "classical_active_marker_gap": ...}``.
    """
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

    # Ask the engine's registry so a caller-supplied client (and approved
    # overlay rows) are visible. Collect every miss before raising.
    unknown: list[str] = []
    for ing in spec.ingredients:
        try:
            _ENGINE.registries.lookup_ingredient(ing.ingredient_id)
        except UnknownIngredient:
            unknown.append(ing.ingredient_id)
    if unknown:
        listed = ", ".join(repr(item) for item in unknown)
        exc = UnknownIngredient(
            f"{listed} is not in the ingredient registry; "
            "resolve botanical identity before modernization"
        )
        detail = dict(http_error_detail(exc, code="unknown_ingredient"))
        detail["error"] = "unknown_ingredient"
        detail["unknown_ids"] = unknown
        if not all(item in str(detail.get("message") or "") for item in unknown):
            detail["message"] = str(exc)
        raise HTTPException(status_code=422, detail=detail)

    try:
        # Engine uses local schemas; strip shared-only fields at the boundary.
        sku = _ENGINE.modernize(to_engine_payload(spec))
        # Read after modernize. Not a contract field — herbenzo-contracts does
        # not declare it. Attached below only once outbound validation has
        # passed, and only when the indicator actually fired.
        marker_gap = _ENGINE.classical_active_marker_gap
        if sku is None:
            # No marker-backed ingredient. Do not 422 and do not invent a SKU.
            return JSONResponse(
                status_code=200,
                content={
                    "sku": None,
                    "classical_active_marker_gap": marker_gap,
                },
            )
        outbound = validate_outbound_modernized_sku(sku)
        outbound = attach_provenance_thread(spec, outbound)
    except (UnknownIngredient, UnknownMarker) as exc:
        raise HTTPException(
            status_code=422,
            detail=http_error_detail(exc, code="unknown_ingredient"),
        ) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(exc)) from exc

    body = outbound.model_dump(mode="json")
    if marker_gap is not None:
        body["classical_active_marker_gap"] = marker_gap
    return JSONResponse(content=body)


app.include_router(enrich_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

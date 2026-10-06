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
from herbenzo.contract_gate import (
    attach_provenance_thread,
    http_error_detail,
    to_engine_payload,
    validate_inbound_formulation_spec,
    validate_outbound_modernized_sku,
)
from herbenzo.format_suggestions import suggest_from_payload
from herbenzo.research_api import router as research_router
from herbenzo.services.llm_config import validate_llm_settings
from herbenzo.services.classical_marker_gap import marker_warnings
from herbenzo.services.records import (
    ResearchError,
    UnknownMarker,
    apply_marker_overrides,
    provenance_from_approvals,
    snapshot_from_approvals,
)
from herbenzo.services.research import stored_pubmed_count
from herbenzo_contracts import CONTRACT_SCHEMA_VERSION
from herbenzo_contracts.provenance import extend_from_handoff

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

def _llm_config_payload() -> dict[str, Any]:
    settings = get_settings()
    return validate_llm_settings(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )


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
        "llm_config": _llm_config_payload(),
        "ui": "available",
        "endpoints": {
            "ui": "/",
            "health": "/health",
            "modernize": "POST /modernize",
            "research": "POST /research",
            "research_suggest": "GET/POST /research/suggest",
            "research_approve": "POST /research/approve",
            "research_marker": "POST /research/marker",
            "ayush_search": "GET /research/ayush/search?q=",
            "ayush_record": "GET /research/ayush/records/{arp_id}",
            "ayush_accept": "POST /enrich/ayush/{arp_id}/accept",
            "suggest_formats": "POST /suggest-formats",
            "drafts": "GET/POST /drafts",
            "draft": "GET/DELETE /drafts/{id}",
            "static": "/static/",
        },
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


def _approval_matches(spec_ids: set[str], approvals: list[Any]) -> bool:
    """True when every ingredient id has a species-rank approval with enough refs."""
    settings = get_settings()
    matched: set[str] = set()
    for doc in approvals:
        if not isinstance(doc, dict) or doc.get("status") != "approved":
            continue
        ingredient = doc.get("ingredient") if isinstance(doc.get("ingredient"), dict) else {}
        ingredient_id = str(ingredient.get("ingredient_id") or doc.get("ingredient_id") or "")
        taxonomy = doc.get("taxonomy") if isinstance(doc.get("taxonomy"), dict) else {}
        literature = doc.get("literature") if isinstance(doc.get("literature"), dict) else {}
        if str(taxonomy.get("rank") or "").casefold() != "species":
            continue
        if literature.get("status") != "ok":
            continue
        if stored_pubmed_count(literature) < settings.min_pubmed_refs:
            continue
        if ingredient_id:
            matched.add(ingredient_id)
    return spec_ids <= matched and bool(spec_ids)


def _spec_is_complete(spec: dict[str, Any], approvals: list[Any] | None = None) -> bool:
    """True when the draft can be posted to /modernize with its saved approvals."""
    try:
        parsed = validate_inbound_formulation_spec(spec)
    except (ValidationError, ValueError, TypeError):
        return False
    return _approval_matches({ing.ingredient_id for ing in parsed.ingredients}, list(approvals or []))


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
    approvals = doc.get("approvals") if isinstance(doc.get("approvals"), list) else []
    return {
        "id": doc.get("id"),
        "name": doc.get("name"),
        "saved_at": doc.get("saved_at"),
        "updated_at": doc.get("updated_at"),
        "complete": _spec_is_complete(spec, approvals),
        "ingredient_count": _ingredient_count(spec),
        "spec": spec,
        "approvals": approvals,
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
    approvals = payload.get("approvals", [])
    if approvals is None:
        approvals = []
    if not isinstance(approvals, list):
        raise HTTPException(status_code=422, detail="Draft approvals must be a list")
    return name, spec, draft_id, approvals


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

    name, spec, draft_id, approvals = _parse_draft_body(payload)
    now = datetime.now(UTC).isoformat()
    complete = _spec_is_complete(spec, approvals)

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
                "approvals": approvals,
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
                "approvals": approvals,
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
    except ResearchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=_research_detail(exc)) from exc
    except UnknownMarker as exc:
        raise HTTPException(
            status_code=422,
            detail=_research_detail(ResearchError(str(exc), code="marker_unverified")),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(exc)) from exc


def _research_detail(exc: ResearchError) -> dict[str, Any]:
    detail = dict(http_error_detail(exc, code=exc.code))
    detail["error"] = exc.code
    return detail


def _split_modernize_body(payload: dict[str, Any]) -> tuple[dict[str, Any], list[Any], list[Any] | None]:
    if "spec" in payload:
        extra = set(payload) - {"spec", "approvals", "marker_overrides"}
        if extra:
            raise ValueError("unexpected modernize fields: " + ", ".join(sorted(extra)))
        spec = payload.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("spec must be an object")
        approvals = payload.get("approvals") or []
        if not isinstance(approvals, list):
            raise ValueError("approvals must be a list")
        overrides = payload.get("marker_overrides")
        if overrides is not None and not isinstance(overrides, list):
            raise ValueError("marker_overrides must be a list")
        return spec, approvals, overrides
    return payload, [], None


def _ingredient_pending(ingredient: dict[str, Any]) -> bool:
    if ingredient.get("marker_status") == "pending":
        return True
    marker = ingredient.get("marker")
    return isinstance(marker, dict) and marker.get("marker_status") == "pending"


def _provenance_thread_payload(spec, sku_id: str) -> dict[str, Any]:
    thread = extend_from_handoff(
        spec.provenance_thread,
        stage="B",
        payload={
            "spec_id": spec.source_spec_id,
            "formulation_id": spec.formulation_id,
            "sku_id": sku_id,
        },
        sku_id=sku_id,
        formulation_id=spec.formulation_id,
        spec_id=spec.source_spec_id,
    )
    return thread.model_dump(mode="json")


@app.post("/modernize")
async def modernize(request: Request):
    """Modernize an approved research snapshot.

    Body is either a bare FormulationSpec (rejected as not_approved) or
    ``{"spec", "approvals", "marker_overrides"}``. Computation does not call
    PubChem or an LLM. Provenance is attached beside the SKU.
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
        spec_payload, approvals, overrides = _split_modernize_body(payload)
        spec = validate_inbound_formulation_spec(spec_payload)
        approvals = apply_marker_overrides(approvals, overrides)
    except ResearchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=_research_detail(exc)) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(exc)) from exc

    lookup = snapshot_from_approvals([doc for doc in approvals if isinstance(doc, dict)])
    missing = []
    for ing in spec.ingredients:
        if ing.ingredient_id not in lookup._records:
            missing.append(ing.ingredient_id)
    if missing:
        listed = ", ".join(missing)
        exc = ResearchError(
            f"{listed} is not approved for this request; research it and approve it before modernization",
            code="not_approved",
        )
        detail = _research_detail(exc)
        detail["ingredient_ids"] = missing
        raise HTTPException(status_code=422, detail=detail)

    engine = ModernizerEngine(lookup)
    provenance = provenance_from_approvals([doc for doc in approvals if isinstance(doc, dict)])
    try:
        sku = engine.modernize(to_engine_payload(spec))
        marker_gap = engine.classical_active_marker_gap
        local = json.loads(sku.model_dump_json())
        pending = any(_ingredient_pending(ing) for ing in local.get("ingredients") or [])
        # Shared ModernizedSKU requires a PubChem marker block. A pending
        # ingredient has none on purpose, so that gate runs only when every
        # marker is resolved. Shared models stay unchanged.
        if pending:
            body = local
            body["provenance_thread"] = _provenance_thread_payload(spec, sku.sku_id)
        else:
            outbound = validate_outbound_modernized_sku(sku)
            outbound = attach_provenance_thread(spec, outbound)
            body = outbound.model_dump(mode="json")
            for ingredient in body.get("ingredients") or []:
                ingredient["marker_status"] = "resolved"
    except ResearchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=_research_detail(exc)) from exc
    except UnknownMarker as exc:
        raise HTTPException(
            status_code=422,
            detail=_research_detail(ResearchError(str(exc), code="marker_unverified")),
        ) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(exc)) from exc

    if marker_gap is not None:
        body["classical_active_marker_gap"] = marker_gap
    body["warnings"] = marker_warnings(marker_gap)
    body["research_provenance"] = provenance
    return JSONResponse(content=body)


app.include_router(research_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

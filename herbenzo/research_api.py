"""Live research routes. Nothing on these routes is stored as an ingredient list."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from herbenzo.contract_gate import http_error_detail
from herbenzo.services.gemini_research import ResearchFrontDoor
from herbenzo.services.records import ResearchError
from herbenzo.services.research import ResearchService, build_research_service

router = APIRouter()

_FRONT: ResearchFrontDoor | None = None


def build_front_door() -> ResearchFrontDoor:
    return ResearchFrontDoor(build_research_service())


def get_front_door() -> ResearchFrontDoor:
    global _FRONT
    if _FRONT is None:
        _FRONT = build_front_door()
    return _FRONT


def _error(exc: ResearchError) -> HTTPException:
    detail = dict(http_error_detail(exc, code=exc.code))
    detail["error"] = exc.code
    return HTTPException(status_code=exc.status_code, detail=detail)


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=http_error_detail(ValueError("Request body must be JSON"))) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail=http_error_detail(ValueError("Request body must be a JSON object")),
        )
    return payload


@router.post("/research")
async def research(request: Request):
    """Research one ingredient name. The response is not saved."""
    payload = await _json_object(request)
    query = payload.get("query") or payload.get("q") or payload.get("name")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=422, detail=http_error_detail(ValueError("query is required")))
    part = payload.get("part_used")
    sources = payload.get("name_sources")
    name_query = payload.get("name_query")
    try:
        return get_front_door().research(
            query.strip(),
            part_used=part if isinstance(part, str) else None,
            name_sources=sources if isinstance(sources, list) else None,
            name_query=name_query if isinstance(name_query, str) else None,
        )
    except ResearchError as exc:
        raise _error(exc) from exc


@router.get("/research/suggest")
def research_suggest(q: str = ""):
    try:
        return get_front_door().service.suggest(q)
    except ResearchError as exc:
        return {
            "query": q,
            "suggestions": [],
            "auto_selected": None,
            "source": "NCBI Taxonomy, GBIF vernacular, Wikidata",
            "source_notes": [str(exc)],
            "error": str(exc),
        }


@router.post("/research/suggest")
async def research_suggest_post(request: Request):
    payload = await _json_object(request)
    query = payload.get("q") or payload.get("query") or ""
    if not isinstance(query, str):
        query = ""
    try:
        return get_front_door().service.suggest(query)
    except ResearchError as exc:
        return {
            "query": query,
            "suggestions": [],
            "auto_selected": None,
            "source": "NCBI Taxonomy, GBIF vernacular, Wikidata",
            "source_notes": [str(exc)],
            "error": str(exc),
        }


@router.post("/research/approve")
async def research_approve(request: Request):
    """Stateless approval. The caller keeps the returned document."""
    payload = await _json_object(request)
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else payload
    if "taxonomy" not in candidate and isinstance(payload.get("candidate"), dict):
        candidate = payload["candidate"]
    service: ResearchService = get_front_door().service
    try:
        return service.approve(
            candidate,
            marker_name=payload.get("marker_name") if isinstance(payload.get("marker_name"), str) else None,
            part_used=payload.get("part_used") if isinstance(payload.get("part_used"), str) else None,
            common_name=payload.get("common_name") if isinstance(payload.get("common_name"), str) else None,
            note=payload.get("note") if isinstance(payload.get("note"), str) else None,
        )
    except ResearchError as exc:
        raise _error(exc) from exc


@router.post("/research/marker")
async def research_marker(request: Request):
    """Replace the marker on an approval after a live PubChem check."""
    payload = await _json_object(request)
    approval = payload.get("approval")
    if not isinstance(approval, dict):
        raise HTTPException(status_code=422, detail=http_error_detail(ValueError("approval is required")))
    marker_name = payload.get("marker_name")
    if not isinstance(marker_name, str) or not marker_name.strip():
        raise HTTPException(status_code=422, detail=http_error_detail(ValueError("marker_name is required")))
    try:
        return get_front_door().service.set_marker(
            approval,
            marker_name,
            note=payload.get("note") if isinstance(payload.get("note"), str) else None,
        )
    except ResearchError as exc:
        raise _error(exc) from exc

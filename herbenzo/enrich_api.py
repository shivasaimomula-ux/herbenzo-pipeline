"""Enrichment routes on the Stage B app.

Pending candidates are review records. They are not registry rows and they are
not accepted by POST /modernize.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from herbenzo.services.enrichment import (
    EnrichmentError,
    EnrichmentService,
    build_enrichment_service,
)

router = APIRouter()


class ProposeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200)
    part_used: str | None = Field(default=None, max_length=120)
    max_markers: int = Field(default=3, ge=1, le=8)
    max_pmids: int = Field(default=5, ge=0, le=10)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marker_name: str | None = Field(default=None, max_length=200)
    part_used: str | None = Field(default=None, max_length=120)
    common_name: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=500)


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


class AyushAcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=500)


def service() -> EnrichmentService:
    return build_enrichment_service()


def _http(exc: EnrichmentError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": str(exc)},
    )


async def _optional_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return payload


@router.post("/enrich/propose", status_code=201)
def propose(body: ProposeBody):
    """Resolve a species and store a pending evidence bundle. Does not touch the registry."""
    try:
        return service().propose(
            body.query,
            part_used=body.part_used,
            max_markers=body.max_markers,
            max_pmids=body.max_pmids,
        )
    except EnrichmentError as exc:
        raise _http(exc) from exc


@router.get("/enrich/candidates")
def list_candidates(status: str | None = None):
    if status is not None and status not in {"pending", "approved", "rejected"}:
        raise HTTPException(status_code=422, detail="status must be pending, approved, or rejected")
    try:
        return {"candidates": service().list(status=status)}
    except EnrichmentError as exc:
        raise _http(exc) from exc


@router.get("/enrich/candidates/{candidate_id}")
def get_candidate(candidate_id: str):
    try:
        return service().get(candidate_id)
    except EnrichmentError as exc:
        raise _http(exc) from exc


@router.post("/enrich/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: str, request: Request):
    """Promote one PubChem-backed marker into the curated registry overlay."""
    try:
        body = ApproveBody.model_validate(await _optional_body(request))
        doc = service().approve(
            candidate_id,
            marker_name=body.marker_name,
            part_used=body.part_used,
            common_name=body.common_name,
            note=body.note,
        )
    except EnrichmentError as exc:
        raise _http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return doc


@router.get("/research/ayush/search")
def research_ayush_search(
    q: str = Query(min_length=1, max_length=200),
    system: str = "any",
    category: str = "any",
    limit: int | None = Query(default=None, ge=1, le=25),
    offset: int = Query(default=0, ge=0, le=5000),
):
    """Bibliographic ARP search. ``status: disabled`` when the portal switch is off."""
    from herbenzo.services.ayush_portal import ayush_public_search

    return ayush_public_search(q, system=system, category=category, limit=limit, offset=offset)


@router.get("/research/ayush/records/{arp_id}")
def research_ayush_record(arp_id: str):
    """One ARP record page, resolved by ARP id. Does not store an abstract."""
    from herbenzo.services.ayush_portal import ayush_public_record

    try:
        return ayush_public_record(arp_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/enrich/ayush/{arp_id}/accept")
def accept_ayush_record(arp_id: str, body: AyushAcceptBody | None = None):
    """Record a reviewer acceptance for one Ayush Research Portal id.

    Does not fetch the portal. Later searches apply the decision to records
    that have neither a PMID nor a DOI.
    """
    from herbenzo.config import get_settings
    from herbenzo.services.ayush_portal import AyushPortalService

    note = body.note if body is not None else ""
    try:
        return AyushPortalService.from_settings(get_settings()).accept(arp_id, note=note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/enrich/candidates/{candidate_id}/reject")
async def reject_candidate(candidate_id: str, request: Request):
    try:
        body = RejectBody.model_validate(await _optional_body(request))
        return service().reject(candidate_id, reason=body.reason)
    except EnrichmentError as exc:
        raise _http(exc) from exc

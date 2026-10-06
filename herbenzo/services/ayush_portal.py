"""Ayush Research Portal citations, license provenance, and the tool adapter.

Search hits are bibliographic. A PMID is cited from PubMed (verified through
the enrichment E-utilities client when one is attached). A DOI without a PMID
is cited as that DOI. A record with neither stays ``confidence: low`` and
``review_status: needs_review`` until ``accept`` records a reviewer decision.

``ayush_portal_search`` is the function a Gemini or OpenAI tool dispatcher
should register under the name ``ayush_portal_search``. It returns compact
hits and does not raise.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from herbenzo.clients.ayush_portal import (
    AYUSH_PORTAL_FUNCTION,
    AYUSH_PORTAL_TOOL,
    RECORD_ENDPOINT,
    SEARCH_ENDPOINT,
    AyushPortalClient,
    compact_hit,
)
from herbenzo.config import Settings, get_settings

__all__ = [
    "SOURCE",
    "AyushPortalService",
    "AyushReviewStore",
    "attribution_line",
    "ayush_portal_search",
    "ayush_portal_tool_schema",
    "ayush_public_record",
    "ayush_public_search",
    "public_hit",
]

_PUBLIC_KEYS = (
    "arp_id",
    "title",
    "journal",
    "year",
    "authors",
    "pmid",
    "doi",
    "publisher_url",
    "category",
    "system",
    "evidence_grade",
    "record_url",
    "citation",
    "confidence",
    "review_status",
    "cross_check_source",
    "attribution",
    "source",
    "endpoint",
    "retrieved_at",
    "license_basis",
    "permission_ref",
)
_GAP_KEYS = ("pmid", "doi", "system", "category", "evidence_grade", "journal")

SOURCE = "Ayush Research Portal"
DEFAULT_LICENSE_BASIS = "verbal_authorization"
DEFAULT_PERMISSION_REF = (
    "CCRAS Deputy Director Srikanth, Delhi, 2026-10-06; "
    "research use permitted for Herbenzo Ayurvedic and Herbal Pvt Ltd"
)
_ARP_ID = re.compile(r"^ARP_[A-Z0-9]+$")
_REVIEW_LOCK = threading.Lock()
_DROPPED_KEYS = ("abstract", "email", "emails", "corresponding_author", "affiliation", "keywords")


def attribution_line(record_url: str, arp_id: str, retrieved_at: str) -> str:
    date = retrieved_at[:10] if len(retrieved_at) >= 10 else retrieved_at
    return (
        "Source: Ayush Research Portal, Ministry of Ayush, Government of India"
        f" — {record_url} (ARP ID {arp_id}), retrieved {date}"
    )


def ayush_portal_tool_schema() -> dict[str, Any]:
    """Fresh Gemini function declaration for ``ayush_portal_search``.

    OpenAI-style tools wrap the same object: ``{"type": "function", "function": schema}``.
    ``AYUSH_PORTAL_TOOL`` is that wrapper.
    """
    return json.loads(json.dumps(AYUSH_PORTAL_FUNCTION))


def ayush_portal_search(
    query: str,
    system: str = "any",
    category: str = "any",
    limit: int | None = None,
    *,
    service: AyushPortalService | None = None,
) -> dict[str, Any]:
    """Tool adapter. Compact hits, or ``{status, reason}`` when the portal cannot be used.

    Register this function in the research front door under the tool name
    ``ayush_portal_search``. A disabled switch and a portal failure both return
    a structured result so the caller can continue with PubMed and web search.
    """
    try:
        active = service if service is not None else AyushPortalService.from_settings(get_settings())
        result = active.search(query, system=system, category=category, limit=limit)
    except Exception as exc:
        return {"status": "unavailable", "reason": exc.__class__.__name__}
    status = result.get("status")
    if status != "ok":
        return {"status": status or "unavailable", "reason": result.get("reason") or "unavailable"}
    return {"status": "ok", "hits": list(result.get("hits") or [])}


def public_hit(record: dict[str, Any]) -> dict[str, Any]:
    """Bibliographic hit plus citation, review state, and provenance. No abstract."""
    hit = {key: record.get(key) for key in _PUBLIC_KEYS}
    for key in _DROPPED_KEYS:
        hit.pop(key, None)
    return hit


def ayush_public_search(
    query: str,
    system: str = "any",
    category: str = "any",
    limit: int | None = None,
    offset: int = 0,
    *,
    service: AyushPortalService | None = None,
) -> dict[str, Any]:
    """CLI and HTTP search. Richer than the Gemini tool payload, still bibliographic."""
    try:
        active = service if service is not None else AyushPortalService.from_settings(get_settings())
        result = active.search(query, system=system, category=category, limit=limit, offset=offset)
    except Exception as exc:
        return {"status": "unavailable", "reason": exc.__class__.__name__, "hits": []}
    return _present_search(result)


def ayush_public_record(arp_id: str, *, service: AyushPortalService | None = None) -> dict[str, Any]:
    """CLI and HTTP record lookup by ARP id. Raises ``ValueError`` when the id is malformed."""
    active = service if service is not None else AyushPortalService.from_settings(get_settings())
    result = active.record_by_arp_id(arp_id)
    if result.get("status") != "ok" or not isinstance(result.get("record"), dict):
        return {
            "status": result.get("status") or "unavailable",
            "reason": result.get("reason") or "unavailable",
            "source": SOURCE,
            "arp_id": (arp_id or "").strip(),
            "endpoint": result.get("endpoint"),
            "retrieved_at": result.get("retrieved_at"),
            "license_basis": result.get("license_basis"),
            "permission_ref": result.get("permission_ref"),
            "hit": None,
        }
    record = result["record"]
    return {
        "status": "ok",
        "reason": None,
        "source": SOURCE,
        "arp_id": record.get("arp_id"),
        "endpoint": record.get("endpoint") or RECORD_ENDPOINT,
        "retrieved_at": record.get("retrieved_at"),
        "license_basis": record.get("license_basis"),
        "permission_ref": record.get("permission_ref"),
        "hit": public_hit(record),
    }


def _present_search(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status") or "unavailable"
    body = {
        "status": status if status in {"ok", "disabled", "unavailable"} else "unavailable",
        "reason": result.get("reason"),
        "source": result.get("source") or SOURCE,
        "query": result.get("query"),
        "endpoint": result.get("endpoint"),
        "retrieved_at": result.get("retrieved_at"),
        "license_basis": result.get("license_basis"),
        "permission_ref": result.get("permission_ref"),
        "hits": [],
    }
    if body["status"] != "ok":
        return body
    records = result.get("records") if isinstance(result.get("records"), list) else []
    body["hits"] = [public_hit(row) for row in records if isinstance(row, dict)]
    return body


def ayush_portal_search_from_arguments(
    arguments: dict[str, Any] | None,
    *,
    service: AyushPortalService | None = None,
) -> dict[str, Any]:
    """Dispatcher entry point. ``arguments`` is the model tool-call object."""
    if not isinstance(arguments, dict):
        return {"status": "unavailable", "reason": "invalid_arguments"}
    query = arguments.get("query")
    if not isinstance(query, str):
        return {"status": "unavailable", "reason": "empty_query"}
    limit = arguments.get("limit")
    return ayush_portal_search(
        query,
        system=arguments.get("system") or "any",
        category=arguments.get("category") or "any",
        limit=limit if isinstance(limit, int) else None,
        service=service,
    )


class AyushReviewStore:
    """Reviewer accept notes keyed by ARP ID.

    The file is ``ayush_reviews.json`` under the configured directory
    (``HERBENZO_REGISTRY_DIR``). It records a provenance note for one portal
    record. It is not an ingredient registry and it does not store a candidate.
    """

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory).expanduser() / "ayush_reviews.json"

    def get(self, arp_id: str) -> dict[str, Any] | None:
        row = self._load().get(arp_id)
        return row if isinstance(row, dict) else None

    def accept(self, arp_id: str, *, note: str, accepted_at: str, license_basis: str, permission_ref: str) -> dict[str, Any]:
        decision = {
            "arp_id": arp_id,
            "review_status": "accepted",
            "accepted_at": accepted_at,
            "note": note,
            "license_basis": license_basis,
            "permission_ref": permission_ref,
            "source": SOURCE,
        }
        with _REVIEW_LOCK:
            data = self._load()
            data[arp_id] = decision
            self._save(data)
        return decision

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


class AyushPortalService:
    def __init__(
        self,
        client: AyushPortalClient,
        *,
        pubmed: Any | None = None,
        reviews: AyushReviewStore | None = None,
        license_basis: str | None = None,
        permission_ref: str | None = None,
        doi_resolver: Callable[[str], bool] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.pubmed = pubmed
        self.reviews = reviews
        self.license_basis = (license_basis or "").strip() or DEFAULT_LICENSE_BASIS
        self.permission_ref = (permission_ref or "").strip() or DEFAULT_PERMISSION_REF
        self.doi_resolver = doi_resolver
        self._now = now or (lambda: datetime.now(UTC))

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        pubmed: Any | None = None,
        reviews: AyushReviewStore | None = None,
        transport: Callable | None = None,
    ) -> AyushPortalService:
        current = settings if settings is not None else get_settings()
        client = AyushPortalClient(
            base_url=current.ayush_portal_base_url,
            enabled=current.ayush_portal_enabled,
            transport=transport,
            min_interval_s=current.ayush_portal_min_interval_s,
            timeout_s=current.ayush_portal_timeout_s,
            max_results=current.ayush_portal_max_results,
            user_agent=current.ayush_portal_user_agent,
        )
        store = reviews if reviews is not None else AyushReviewStore(current.registry_dir)
        return cls(
            client,
            pubmed=pubmed,
            reviews=store,
            license_basis=current.ayush_portal_license_basis,
            permission_ref=current.ayush_portal_permission_ref,
        )

    def search(
        self,
        query: str,
        *,
        system: str | None = None,
        category: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Stored literature block. Never raises."""
        try:
            raw = self.client.search(query, system=system, category=category, limit=limit, offset=offset)
        except Exception as exc:
            return self._failure(query, exc.__class__.__name__)
        status = raw.get("status")
        if status != "ok":
            return self._failure(query, str(raw.get("reason") or status or "unavailable"), status=status or "unavailable")
        retrieved_at = str(raw.get("retrieved_at") or self._now().isoformat())
        endpoint = str(raw.get("endpoint") or SEARCH_ENDPOINT)
        records = [
            self._finalize(hit, query=query, endpoint=endpoint, retrieved_at=retrieved_at)
            for hit in raw.get("hits") or []
            if isinstance(hit, dict)
        ]
        return {
            "status": "ok",
            "reason": None,
            "source": SOURCE,
            "query": query,
            "endpoint": endpoint,
            "retrieved_at": retrieved_at,
            "records": records,
            "hits": [compact_hit(record) for record in records],
            "license_basis": self.license_basis,
            "permission_ref": self.permission_ref,
        }

    def record_by_arp_id(self, arp_id: str) -> dict[str, Any]:
        """Resolve an ARP id through search, then fetch that one record page.

        PMID lives on the search JSON. Year, authors, and the publisher URL
        live on the HTML page. The page view increments the portal counter.
        """
        ident = (arp_id or "").strip()
        if not _ARP_ID.fullmatch(ident):
            raise ValueError("arp_id is invalid")
        try:
            found = self.client.search(ident, limit=5)
        except Exception as exc:
            return self._failure(ident, exc.__class__.__name__)
        if found.get("status") != "ok":
            return self._failure(
                ident,
                str(found.get("reason") or found.get("status") or "unavailable"),
                status=found.get("status") or "unavailable",
            )
        match = next(
            (hit for hit in found.get("hits") or [] if isinstance(hit, dict) and hit.get("arp_id") == ident),
            None,
        )
        if match is None:
            return self._failure(ident, "not_found")
        try:
            page = self.client.record(match.get("arp_internal_id"))
        except Exception as exc:
            return self._failure(ident, exc.__class__.__name__, endpoint=RECORD_ENDPOINT)
        if page.get("status") != "ok" or not isinstance(page.get("record"), dict):
            return self._failure(
                ident,
                str(page.get("reason") or page.get("status") or "unavailable"),
                status=page.get("status") or "unavailable",
                endpoint=RECORD_ENDPOINT,
            )
        merged = _fill_record_gaps(page["record"], match)
        retrieved_at = str(page.get("retrieved_at") or self._now().isoformat())
        stored = self._finalize(
            merged,
            query=ident,
            endpoint=RECORD_ENDPOINT,
            retrieved_at=retrieved_at,
        )
        return {
            "status": "ok",
            "reason": None,
            "source": SOURCE,
            "query": ident,
            "endpoint": RECORD_ENDPOINT,
            "retrieved_at": retrieved_at,
            "record": stored,
            "license_basis": self.license_basis,
            "permission_ref": self.permission_ref,
        }

    def record(self, internal_id: int | str, *, query: str = "") -> dict[str, Any]:
        """One record page, with the same citation and license fields as search hits."""
        try:
            raw = self.client.record(internal_id)
        except Exception as exc:
            return self._failure(query, exc.__class__.__name__, endpoint="View_Res_Landing_Url")
        if raw.get("status") != "ok" or not isinstance(raw.get("record"), dict):
            return self._failure(
                query,
                str(raw.get("reason") or raw.get("status") or "unavailable"),
                status=raw.get("status") or "unavailable",
                endpoint="View_Res_Landing_Url",
            )
        retrieved_at = str(raw.get("retrieved_at") or self._now().isoformat())
        stored = self._finalize(
            raw["record"],
            query=query,
            endpoint="View_Res_Landing_Url",
            retrieved_at=retrieved_at,
        )
        return {"status": "ok", "reason": None, "record": stored}

    def accept(self, arp_id: str, *, note: str = "", record: dict[str, Any] | None = None) -> dict[str, Any]:
        """Mark an ARP record reviewer-accepted. Persists the decision and updates ``record`` when given."""
        ident = (arp_id or "").strip()
        if not _ARP_ID.fullmatch(ident):
            raise ValueError("arp_id is invalid")
        accepted_at = self._now().isoformat()
        if self.reviews is not None:
            self.reviews.accept(
                ident,
                note=(note or "").strip(),
                accepted_at=accepted_at,
                license_basis=self.license_basis,
                permission_ref=self.permission_ref,
            )
        if record is None:
            return {
                "arp_id": ident,
                "review_status": "accepted",
                "accepted_at": accepted_at,
                "note": (note or "").strip(),
                "source": SOURCE,
                "license_basis": self.license_basis,
                "permission_ref": self.permission_ref,
            }
        updated = self._finalize(
            record,
            query=str(record.get("query") or ""),
            endpoint=str(record.get("endpoint") or SEARCH_ENDPOINT),
            retrieved_at=str(record.get("retrieved_at") or accepted_at),
        )
        updated["review_status"] = "accepted"
        updated["confidence"] = "accepted"
        updated["accepted_at"] = accepted_at
        updated["review_note"] = (note or "").strip() or None
        return updated

    def _finalize(self, hit: dict[str, Any], *, query: str, endpoint: str, retrieved_at: str) -> dict[str, Any]:
        record = {key: value for key, value in hit.items() if key not in _DROPPED_KEYS}
        for key in _DROPPED_KEYS:
            record.pop(key, None)
        record["source"] = SOURCE
        record["query"] = query
        record["endpoint"] = endpoint
        record["retrieved_at"] = retrieved_at
        record["license_basis"] = self.license_basis
        record["permission_ref"] = self.permission_ref
        record_url = str(record.get("record_url") or "")
        arp_id = str(record.get("arp_id") or "")
        record["attribution"] = attribution_line(record_url, arp_id, retrieved_at)
        self._apply_citation(record)
        self._apply_review(record)
        self._scrub_strings(record)
        return record

    def _apply_citation(self, record: dict[str, Any]) -> None:
        pmid = record.get("pmid") if isinstance(record.get("pmid"), str) and record.get("pmid") else None
        doi = record.get("doi") if isinstance(record.get("doi"), str) and record.get("doi") else None
        if pmid:
            verified = self._verify_pmid(pmid)
            record["cross_check_source"] = "pubmed" if verified else "none"
            record["citation"] = {
                "source": "PubMed",
                "pmid": pmid,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            }
            record["confidence"] = "verified" if verified else "unverified"
            record["review_status"] = "not_required"
            return
        if doi:
            resolved = self._resolve_doi(doi)
            record["cross_check_source"] = "crossref" if resolved else "doi"
            record["citation"] = {"source": "DOI", "doi": doi, "url": f"https://doi.org/{doi}"}
            record["confidence"] = "verified" if resolved else "identifier"
            record["review_status"] = "not_required"
            return
        record["cross_check_source"] = "none"
        record["citation"] = {"source": SOURCE, "url": record.get("record_url")}
        record["confidence"] = "low"
        record["review_status"] = "needs_review"

    def _apply_review(self, record: dict[str, Any]) -> None:
        if record.get("review_status") != "needs_review" or self.reviews is None:
            return
        arp_id = str(record.get("arp_id") or "")
        decision = self.reviews.get(arp_id)
        if not decision or decision.get("review_status") != "accepted":
            return
        record["review_status"] = "accepted"
        record["confidence"] = "accepted"
        record["accepted_at"] = decision.get("accepted_at")
        note = decision.get("note")
        record["review_note"] = note if isinstance(note, str) and note else None

    def _verify_pmid(self, pmid: str) -> bool:
        pubmed = self.pubmed
        if pubmed is None:
            return False
        try:
            summary = pubmed.summary("pubmed", [pmid])
        except Exception:
            return False
        records = summary.get("records") if isinstance(summary, dict) else None
        if not isinstance(records, list):
            return False
        for row in records:
            if isinstance(row, dict) and str(row.get("uid") or "").strip() == pmid:
                return True
        return False

    def _resolve_doi(self, doi: str) -> bool:
        if self.doi_resolver is None:
            return False
        try:
            return bool(self.doi_resolver(doi))
        except Exception:
            return False

    def _failure(
        self,
        query: str,
        reason: str,
        *,
        status: str | None = None,
        endpoint: str = SEARCH_ENDPOINT,
    ) -> dict[str, Any]:
        reported = status or "unavailable"
        if reported not in {"unavailable", "disabled"}:
            reported = "unavailable"
        return {
            "status": reported,
            "reason": reason,
            "source": SOURCE,
            "query": query,
            "endpoint": endpoint,
            "retrieved_at": None,
            "records": [],
            "hits": [],
            "license_basis": self.license_basis,
            "permission_ref": self.permission_ref,
        }

    def _scrub_strings(self, record: dict[str, Any]) -> None:
        from herbenzo.clients.ayush_portal import strip_emails

        for key, value in list(record.items()):
            if isinstance(value, str):
                record[key] = strip_emails(value)
            elif isinstance(value, dict):
                for inner, inner_value in list(value.items()):
                    if isinstance(inner_value, str):
                        value[inner] = strip_emails(inner_value)


def _fill_record_gaps(page: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    """Keep the record page, and copy index fields the HTML does not carry."""
    merged = {key: value for key, value in page.items() if key not in _DROPPED_KEYS}
    for key in _GAP_KEYS:
        if not merged.get(key) and index.get(key):
            merged[key] = index[key]
    return merged


# Re-exported for callers that want the OpenAI wrapper without importing the client.
OPENAI_TOOL = AYUSH_PORTAL_TOOL

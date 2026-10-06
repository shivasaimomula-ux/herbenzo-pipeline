"""PubChem reads used by ingredient enrichment.

Physicochemical numbers come only from these PUG-REST property records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from herbenzo.clients.httpjson import CachedJsonClient, HttpError
from herbenzo.services.enrich_parse import (
    aids_from_payload,
    properties_from_pubchem_row,
    synonyms_from_payload,
)

__all__ = ["PubChemLookup", "PubChemLookupError", "PROPERTY_FIELDS"]

_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

PROPERTY_FIELDS = [
    "Title",
    "MolecularFormula",
    "MolecularWeight",
    "CanonicalSMILES",
    "IsomericSMILES",
    "ConnectivitySMILES",
    "InChIKey",
    "IUPACName",
    "XLogP",
    "TPSA",
    "HBondDonorCount",
    "HBondAcceptorCount",
    "RotatableBondCount",
]


class PubChemLookupError(RuntimeError):
    """PubChem returned no usable record."""


class PubChemLookup:
    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        transport: Callable | None = None,
        min_interval_s: float = 0.25,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "cache_dir": cache_dir or "cache/enrichment/pubchem",
            "transport": transport,
            "min_interval_s": min_interval_s,
        }
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.http = CachedJsonClient(**kwargs)

    def properties(self, cid: int) -> tuple[dict[str, Any], str]:
        fields = ",".join(PROPERTY_FIELDS)
        url = f"{_BASE}/compound/cid/{cid}/property/{fields}/JSON"
        try:
            payload, retrieved_at = self.http.get_json(url, cache_key=f"props_{cid}")
        except HttpError as exc:
            raise PubChemLookupError(f"no property record for CID {cid}") from exc
        rows = (payload.get("PropertyTable") or {}).get("Properties") or []
        if not rows or not isinstance(rows[0], dict):
            raise PubChemLookupError(f"no property record for CID {cid}")
        parsed = properties_from_pubchem_row(rows[0])
        if parsed.get("cid") is None:
            parsed["cid"] = int(cid)
        return parsed, retrieved_at

    def synonyms(self, cid: int) -> tuple[list[str], str]:
        url = f"{_BASE}/compound/cid/{cid}/synonyms/JSON"
        try:
            payload, retrieved_at = self.http.get_json(url, cache_key=f"syn_{cid}")
        except HttpError as exc:
            if exc.status == 404:
                return [], _now_fallback()
            raise PubChemLookupError(f"synonym lookup failed for CID {cid}") from exc
        return synonyms_from_payload(payload), retrieved_at

    def bioassays(self, cid: int) -> tuple[dict[str, Any], str]:
        total_url = f"{_BASE}/compound/cid/{cid}/aids/JSON"
        active_url = f"{_BASE}/compound/cid/{cid}/aids/JSON?aids_type=active"
        try:
            total_payload, retrieved_at = self.http.get_json(total_url, cache_key=f"aids_{cid}_all")
            total = aids_from_payload(total_payload)
        except HttpError as exc:
            if exc.status == 404:
                total = []
                retrieved_at = _now_fallback()
            else:
                return _assay_unavailable(cid, str(exc)), _now_fallback()
        try:
            active_payload, active_at = self.http.get_json(active_url, cache_key=f"aids_{cid}_active")
            active = aids_from_payload(active_payload)
            retrieved_at = active_at or retrieved_at
        except HttpError as exc:
            if exc.status == 404:
                active = []
            else:
                return _assay_unavailable(cid, str(exc)), retrieved_at
        return {
            "status": "ok",
            "total": len(total),
            "active": len(active),
            "source": "PubChem PUG-REST",
            "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=BioAssay-Results",
            "retrieved_at": retrieved_at,
            "error": None,
        }, retrieved_at


def _assay_unavailable(cid: int, error: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "total": None,
        "active": None,
        "source": "PubChem PUG-REST",
        "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=BioAssay-Results",
        "retrieved_at": None,
        "error": error,
    }


def _now_fallback() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()

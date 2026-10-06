"""Chemical taxonomy for a marker compound.

ClassyFire/ChemOnt (kingdom through direct parent) comes from PubChem's
classification record when that record is complete, and otherwise from the
keyless ClassyFire API addressed by InChIKey. NP Classifier pathway, superclass,
and class come from the keyless GNPS NPClassifier service when a SMILES string
is available. Missing classifications are recorded; they do not fail enrichment.
"""

from __future__ import annotations

import hashlib
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from herbenzo.clients.httpjson import CachedJsonClient, HttpError
from herbenzo.services.enrich_parse import (
    LEVELS,
    classyfire_from_entity,
    classyfire_from_pubchem,
    npclassifier_from_payload,
)

__all__ = ["ChemicalTaxonomyClient"]

_PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
_CLASSYFIRE = "http://classyfire.wishartlab.com/entities"
_NPCLASSIFIER = "https://npclassifier.gnps2.org/classify"


class ChemicalTaxonomyClient:
    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        transport: Callable | None = None,
        min_interval_s: float = 0.25,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "cache_dir": cache_dir or "cache/enrichment/chemclass",
            "transport": transport,
            "min_interval_s": min_interval_s,
        }
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.http = CachedJsonClient(**kwargs)

    def classify(
        self,
        *,
        cid: int | None,
        inchikey: str | None,
        smiles: str | None,
    ) -> dict[str, Any]:
        return {
            "classyfire": self._classyfire(cid=cid, inchikey=inchikey),
            "npclassifier": self._npclassifier(smiles),
        }

    def _classyfire(self, *, cid: int | None, inchikey: str | None) -> dict[str, Any]:
        pubchem_levels: dict[str, Any] = {}
        pubchem_at: str | None = None
        pubchem_error: str | None = None
        if cid is not None:
            url = f"{_PUBCHEM}/compound/cid/{int(cid)}/classification/JSON"
            try:
                payload, pubchem_at = self.http.get_json(url, cache_key=f"pugclass_{int(cid)}")
                pubchem_levels = classyfire_from_pubchem(payload)
            except HttpError as exc:
                pubchem_error = str(exc)
        if _filled_count(pubchem_levels) >= 4:
            return _public_classyfire(
                pubchem_levels,
                source="PubChem classification (ClassyFire/ChemOnt)",
                url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=Classification",
                retrieved_at=pubchem_at,
            )
        key = _clean_inchikey(inchikey)
        if key:
            url = f"{_CLASSYFIRE}/{urllib.parse.quote(key, safe='')}.json"
            try:
                payload, retrieved_at = self.http.get_json(url, cache_key=f"classyfire_{key}")
                levels = classyfire_from_entity(payload)
            except HttpError as exc:
                levels = {}
                retrieved_at = None
                api_error = str(exc)
            else:
                api_error = None
            if _filled_count(levels) >= _filled_count(pubchem_levels) and levels:
                return _public_classyfire(
                    levels,
                    source="ClassyFire API",
                    url=f"http://classyfire.wishartlab.com/entities/{key}",
                    retrieved_at=retrieved_at,
                    error=api_error,
                )
            if pubchem_levels:
                return _public_classyfire(
                    pubchem_levels,
                    source="PubChem classification (ClassyFire/ChemOnt)",
                    url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=Classification",
                    retrieved_at=pubchem_at,
                    error=api_error,
                )
            return _empty_classyfire(api_error or pubchem_error or "no ClassyFire record")
        if pubchem_levels:
            return _public_classyfire(
                pubchem_levels,
                source="PubChem classification (ClassyFire/ChemOnt)",
                url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=Classification",
                retrieved_at=pubchem_at,
            )
        return _empty_classyfire(pubchem_error or "no InChIKey for ClassyFire lookup")

    def _npclassifier(self, smiles: str | None) -> dict[str, Any]:
        text = (smiles or "").strip()
        if not text:
            return _empty_npclassifier("no SMILES for NPClassifier")
        quoted = urllib.parse.quote(text, safe="")
        url = f"{_NPCLASSIFIER}?smiles={quoted}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        try:
            payload, retrieved_at = self.http.get_json(url, cache_key=f"npclassifier_{digest}")
        except HttpError as exc:
            return _empty_npclassifier(str(exc))
        parsed = npclassifier_from_payload(payload)
        if not (parsed["pathway"] or parsed["superclass"] or parsed["class"]):
            block = _empty_npclassifier("NPClassifier returned no class labels")
            block["retrieved_at"] = retrieved_at
            return block
        return {
            "status": "ok",
            "pathway": parsed["pathway"],
            "superclass": parsed["superclass"],
            "class": parsed["class"],
            "isglycoside": parsed["isglycoside"],
            "source": "NPClassifier",
            "url": "https://npclassifier.gnps2.org/classify",
            "retrieved_at": retrieved_at,
            "error": None,
        }


def _filled_count(levels: dict[str, Any]) -> int:
    count = 0
    for key in LEVELS:
        node = levels.get(key) or {}
        if isinstance(node, dict) and node.get("name"):
            count += 1
    return count


def _public_classyfire(
    levels: dict[str, Any],
    *,
    source: str,
    url: str,
    retrieved_at: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    chemont: dict[str, str] = {}
    block: dict[str, Any] = {
        "status": "ok" if _filled_count(levels) else "unavailable",
        "source": source,
        "url": url,
        "retrieved_at": retrieved_at,
        "error": error,
        "chemont_ids": chemont,
    }
    for key in LEVELS:
        node = levels.get(key) or {}
        name = node.get("name") if isinstance(node, dict) else None
        block[key] = name
        chemont_id = node.get("chemont_id") if isinstance(node, dict) else None
        if chemont_id:
            chemont[key] = str(chemont_id)
    if block["status"] != "ok" and error is None:
        block["error"] = "classification levels were missing"
    return block


def _empty_classyfire(error: str) -> dict[str, Any]:
    block: dict[str, Any] = {
        "status": "unavailable",
        "source": None,
        "url": None,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "error": error,
        "chemont_ids": {},
    }
    for key in LEVELS:
        block[key] = None
    return block


def _empty_npclassifier(error: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "pathway": [],
        "superclass": [],
        "class": [],
        "isglycoside": None,
        "source": "NPClassifier",
        "url": "https://npclassifier.gnps2.org/classify",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "error": error,
    }


def _clean_inchikey(value: str | None) -> str | None:
    text = (value or "").strip()
    if text.lower().startswith("inchikey="):
        text = text.split("=", 1)[1].strip()
    return text or None

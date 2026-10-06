"""Request-scoped ingredient records.

These shapes describe one approved research snapshot. They are not a stored
registry and they are not loaded from a stock table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from herbenzo.schemas.contracts import PhysicochemicalProfile

__all__ = [
    "IngredientRecord",
    "MarkerRecord",
    "ResearchError",
    "SnapshotLookup",
    "UnknownMarker",
    "marker_dose_mg",
    "normalize_extract_ratio",
    "reconcile_percentages",
    "apply_marker_overrides",
    "provenance_from_approvals",
    "record_from_approval",
    "snapshot_from_approvals",
]


class UnknownMarker(Exception):
    """A named marker has no PubChem descriptor block on this request."""


class ResearchError(Exception):
    """Structured research or approval failure. The message is not re-quoted."""

    def __init__(self, message: str, *, status_code: int = 422, code: str = "research_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    def __str__(self) -> str:
        return self.args[0] if self.args else ""


@dataclass(frozen=True)
class MarkerRecord:
    marker_name: str
    rationale: str
    efflux_substrate: bool = False
    efflux_transporter: str | None = None
    acid_labile: bool = False
    override_requires_citation: bool = False
    evidence_note: str | None = None


@dataclass(frozen=True)
class IngredientRecord:
    ingredient_id: str
    botanical_name: str
    common_name: str
    sanskrit_name: str | None
    synonyms: tuple[str, ...]
    part_used: str
    markers: tuple[MarkerRecord, ...] = field(default_factory=tuple)
    marker_status: str = "pending"


class SnapshotLookup:
    """In-memory view of the approvals attached to one modernize request."""

    def __init__(
        self,
        records: dict[str, IngredientRecord] | None = None,
        properties: dict[str, dict] | None = None,
    ) -> None:
        self._records = dict(records or {})
        self._props = dict(properties or {})

    def lookup_ingredient(self, ingredient_id: str) -> IngredientRecord:
        rec = self._records.get(ingredient_id)
        if rec is None:
            raise ResearchError(
                f"{ingredient_id} is not approved for this request; "
                "research it and approve it before modernization",
                code="not_approved",
            )
        return rec

    def lookup_marker(self, ingredient_id: str) -> MarkerRecord:
        rec = self.lookup_ingredient(ingredient_id)
        if not rec.markers:
            raise UnknownMarker(
                f"no standardization marker on the approved ingredient {ingredient_id}"
            )
        return rec.markers[0]

    def get_physicochemical_properties(self, marker_name: str) -> PhysicochemicalProfile:
        row = self._props.get(marker_name)
        if row is None:
            raise UnknownMarker(f"no PubChem descriptor record for marker {marker_name}")
        return PhysicochemicalProfile(
            pubchem_cid=row["pubchem_cid"],
            molecular_weight=row["molecular_weight"],
            xlogp=row.get("xlogp"),
            tpsa=row["tpsa"],
            hbd=row["hbd"],
            hba=row["hba"],
            rotatable_bonds=row["rotatable_bonds"],
            source=row["source"],
        )


def record_from_approval(doc: dict[str, Any]) -> tuple[IngredientRecord, dict[str, dict]]:
    """Build a modernizer record from one approved research document."""
    ingredient = doc.get("ingredient") if isinstance(doc.get("ingredient"), dict) else {}
    ingredient_id = str(ingredient.get("ingredient_id") or doc.get("ingredient_id") or "").strip()
    if not ingredient_id:
        raise ResearchError("approved research is missing ingredient_id", code="not_approved")
    botanical = str(ingredient.get("botanical_name") or (doc.get("taxonomy") or {}).get("scientific_name") or "").strip()
    common = str(ingredient.get("common_name") or doc.get("query") or botanical).strip()
    part = str(ingredient.get("part_used") or doc.get("part_used") or "unspecified").strip() or "unspecified"
    if not botanical or not common:
        raise ResearchError(
            f"{ingredient_id} is missing a resolved botanical name",
            code="identity_unresolved",
        )
    status = str(ingredient.get("marker_status") or doc.get("marker_status") or "pending")
    markers: list[MarkerRecord] = []
    props: dict[str, dict] = {}
    if status == "resolved":
        for raw in ingredient.get("markers") or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("marker_name") or "").strip()
            rationale = str(raw.get("rationale") or "").strip() or "PubChem-backed marker on this approval."
            if not name:
                continue
            markers.append(
                MarkerRecord(
                    name,
                    rationale,
                    efflux_substrate=bool(raw.get("efflux_substrate")),
                    efflux_transporter=raw.get("efflux_transporter"),
                    acid_labile=bool(raw.get("acid_labile")),
                    override_requires_citation=bool(raw.get("override_requires_citation")),
                    evidence_note=raw.get("evidence_note"),
                )
            )
        stored = doc.get("properties") if isinstance(doc.get("properties"), dict) else {}
        for name, row in stored.items():
            if isinstance(name, str) and isinstance(row, dict):
                props[name] = row
    synonyms = tuple(
        item.strip()
        for item in (ingredient.get("synonyms") or [])
        if isinstance(item, str) and item.strip()
    )
    sanskrit = ingredient.get("sanskrit_name")
    record = IngredientRecord(
        ingredient_id=ingredient_id,
        botanical_name=botanical,
        common_name=common,
        sanskrit_name=sanskrit.strip() if isinstance(sanskrit, str) and sanskrit.strip() else None,
        synonyms=synonyms,
        part_used=part,
        markers=tuple(markers),
        marker_status="resolved" if markers else "pending",
    )
    return record, props


def provenance_from_approvals(approvals: list[dict[str, Any]]) -> dict[str, Any]:
    """Request snapshot carried beside a SKU. This is not a stored registry."""
    rows: list[dict[str, Any]] = []
    for doc in approvals:
        if not isinstance(doc, dict):
            continue
        ingredient = doc.get("ingredient") if isinstance(doc.get("ingredient"), dict) else {}
        taxonomy = doc.get("taxonomy") if isinstance(doc.get("taxonomy"), dict) else {}
        literature = doc.get("literature") if isinstance(doc.get("literature"), dict) else {}
        properties = doc.get("properties") if isinstance(doc.get("properties"), dict) else {}
        pmids = [
            str(article.get("pmid"))
            for article in (literature.get("articles") or [])
            if isinstance(article, dict) and article.get("pmid")
        ]
        urls = [taxonomy.get("url")] if taxonomy.get("url") else []
        for article in literature.get("articles") or []:
            if isinstance(article, dict) and article.get("url"):
                urls.append(article["url"])
        cids = []
        pubchem_rows = []
        for name, row in properties.items():
            if not isinstance(row, dict):
                continue
            if row.get("pubchem_cid") is not None:
                cids.append(row["pubchem_cid"])
            pubchem_rows.append({"marker_name": name, **{key: row.get(key) for key in (
                "pubchem_cid", "molecular_weight", "xlogp", "tpsa", "hbd", "hba", "rotatable_bonds", "source"
            )}})
            if row.get("source") and "pubchem" in str(row.get("source")).casefold():
                cid = row.get("pubchem_cid")
                if cid is not None:
                    urls.append(f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}")
        rows.append(
            {
                "ingredient_id": ingredient.get("ingredient_id") or doc.get("ingredient_id"),
                "taxonomy_id": taxonomy.get("tax_id"),
                "pmids": pmids,
                "cids": cids,
                "urls": urls,
                "retrieved_at": doc.get("retrieved_at") or taxonomy.get("retrieved_at"),
                "approved_at": doc.get("approved_at"),
                "marker_status": ingredient.get("marker_status") or doc.get("marker_status"),
                "pubchem": pubchem_rows,
                "decision": doc.get("status"),
                "name_match": doc.get("name_match") if isinstance(doc.get("name_match"), dict) else None,
            }
        )
    return {
        "stored": False,
        "note": "Provenance from this request's approvals. It is not written to an ingredient registry.",
        "ingredients": rows,
    }


_OVERRIDE_READY = ("pubchem_cid", "molecular_weight", "tpsa", "hbd", "hba", "rotatable_bonds")


def apply_marker_overrides(approvals: list[dict[str, Any]], overrides: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Attach a caller-supplied PubChem block. A name alone is rejected.

    Modernize does not call PubChem. The block has to already be verified,
    usually by POST /research/marker.
    """
    if not overrides:
        return approvals
    if not isinstance(overrides, list):
        raise ResearchError("marker_overrides must be a list", code="marker_unverified")
    copied = [dict(doc) if isinstance(doc, dict) else doc for doc in approvals]
    by_id = {
        str((doc.get("ingredient") or {}).get("ingredient_id") or doc.get("ingredient_id")): doc
        for doc in copied
        if isinstance(doc, dict)
    }
    for override in overrides:
        if not isinstance(override, dict):
            raise ResearchError("marker override must be an object", code="marker_unverified")
        ingredient_id = str(override.get("ingredient_id") or "").strip()
        marker_name = str(override.get("marker_name") or "").strip()
        pubchem = override.get("pubchem") if isinstance(override.get("pubchem"), dict) else None
        if not ingredient_id or not marker_name or pubchem is None:
            raise ResearchError(
                "marker override needs ingredient_id, marker_name, and a verified PubChem block; "
                "call POST /research/marker before modernize",
                code="marker_unverified",
            )
        missing = [key for key in _OVERRIDE_READY if pubchem.get(key) is None]
        if missing:
            raise ResearchError(
                f"marker override for {marker_name} is missing {', '.join(missing)}",
                code="marker_unverified",
            )
        doc = by_id.get(ingredient_id)
        if doc is None:
            raise ResearchError(
                f"{ingredient_id} has no approval to attach a marker to",
                code="not_approved",
            )
        ingredient = dict(doc.get("ingredient") or {})
        rationale = str(override.get("rationale") or "").strip() or (
            f"Marker {marker_name} from a verified PubChem block on this request."
        )
        ingredient["marker_status"] = "resolved"
        ingredient["markers"] = [
            {
                "marker_name": marker_name,
                "rationale": rationale,
                "efflux_substrate": bool(override.get("efflux_substrate")),
                "efflux_transporter": override.get("efflux_transporter"),
                "acid_labile": bool(override.get("acid_labile")),
                "override_requires_citation": bool(override.get("override_requires_citation")),
                "evidence_note": override.get("evidence_note"),
            }
        ]
        doc["ingredient"] = ingredient
        doc["marker_status"] = "resolved"
        properties = dict(doc.get("properties") or {})
        properties[marker_name] = {
            "marker_name": marker_name,
            "pubchem_cid": int(pubchem["pubchem_cid"]),
            "molecular_weight": float(pubchem["molecular_weight"]),
            "xlogp": None if pubchem.get("xlogp") is None else float(pubchem["xlogp"]),
            "tpsa": float(pubchem["tpsa"]),
            "hbd": int(pubchem["hbd"]),
            "hba": int(pubchem["hba"]),
            "rotatable_bonds": int(pubchem["rotatable_bonds"]),
            "source": pubchem.get("source") or f"PubChem PUG-REST CID {pubchem['pubchem_cid']} (computed descriptors)",
        }
        doc["properties"] = properties
        audit = list(doc.get("marker_audit") or [])
        audit.append(
            {
                "action": "modernize_override",
                "marker_name": marker_name,
                "pubchem_cid": int(pubchem["pubchem_cid"]),
            }
        )
        doc["marker_audit"] = audit
    return copied


def snapshot_from_approvals(approvals: list[dict[str, Any]]) -> SnapshotLookup:
    records: dict[str, IngredientRecord] = {}
    props: dict[str, dict] = {}
    for doc in approvals:
        if not isinstance(doc, dict):
            continue
        record, row_props = record_from_approval(doc)
        records[record.ingredient_id] = record
        props.update(row_props)
    return SnapshotLookup(records, props)


def normalize_extract_ratio(quantity_mg: float, extract_ratio: str | None) -> float | None:
    if extract_ratio is None:
        return None
    native, extract = (float(x) for x in extract_ratio.split(":"))
    return quantity_mg * (native / extract)


def marker_dose_mg(quantity_mg: float, standardized_percent: float | None) -> float | None:
    if standardized_percent is None:
        return None
    return quantity_mg * standardized_percent / 100.0


def reconcile_percentages(parts: dict[str, float], tolerance: float = 1e-6) -> dict[str, float]:
    total = sum(parts.values())
    if total <= 0:
        raise ValueError("composition parts must sum to a positive total")
    pct = {k: v / total * 100.0 for k, v in parts.items()}
    if abs(sum(pct.values()) - 100.0) > tolerance:
        raise ValueError(f"percentages sum to {sum(pct.values())!r}, not 100%")
    return pct

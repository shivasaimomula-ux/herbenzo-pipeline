"""Chemistry fixtures for tests.

The stock rows used to live in the runtime registry. They are loaded here on
purpose so a test can exercise BCS and delivery behavior. Production code does
not import this module and does not read these files.
"""

from __future__ import annotations

import json
from pathlib import Path

from herbenzo.components.modernizer.modernizer import ModernizerEngine
from herbenzo.services.records import IngredientRecord, MarkerRecord, SnapshotLookup

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def legacy_lookup() -> SnapshotLookup:
    records: dict[str, IngredientRecord] = {}
    properties: dict[str, dict] = {}
    stored = _load("marker_properties.json")
    for row in _load("stock_ingredients.json"):
        markers = tuple(
            MarkerRecord(
                marker["marker_name"],
                marker["rationale"],
                efflux_substrate=bool(marker.get("efflux_substrate")),
                efflux_transporter=marker.get("efflux_transporter"),
                acid_labile=bool(marker.get("acid_labile")),
                override_requires_citation=bool(marker.get("override_requires_citation")),
                evidence_note=marker.get("evidence_note"),
            )
            for marker in row.get("markers") or []
        )
        records[row["ingredient_id"]] = IngredientRecord(
            ingredient_id=row["ingredient_id"],
            botanical_name=row["botanical_name"],
            common_name=row["common_name"],
            sanskrit_name=row.get("sanskrit_name"),
            synonyms=tuple(row.get("synonyms") or []),
            part_used=row["part_used"],
            markers=markers,
            marker_status="resolved" if markers else "pending",
        )
        for marker in markers:
            if marker.marker_name in stored:
                properties[marker.marker_name] = stored[marker.marker_name]
    return SnapshotLookup(records, properties)


def legacy_engine() -> ModernizerEngine:
    return ModernizerEngine(legacy_lookup())


def approval_for(ingredient_id: str, lookup: SnapshotLookup | None = None) -> dict:
    """Minimal approved document so an API test can modernize a fixture herb."""
    client = lookup or legacy_lookup()
    record = client.lookup_ingredient(ingredient_id)
    markers = []
    properties = {}
    status = "pending"
    if record.markers:
        status = "resolved"
        marker = record.markers[0]
        markers.append(
            {
                "marker_name": marker.marker_name,
                "rationale": marker.rationale,
                "efflux_substrate": marker.efflux_substrate,
                "efflux_transporter": marker.efflux_transporter,
                "acid_labile": marker.acid_labile,
                "override_requires_citation": marker.override_requires_citation,
                "evidence_note": marker.evidence_note,
            }
        )
        properties[marker.marker_name] = dict(client._props[marker.marker_name])
    return {
        "status": "approved",
        "approved_at": "2026-10-06T00:00:00+00:00",
        "retrieved_at": "2026-10-06T00:00:00+00:00",
        "ingredient_id": record.ingredient_id,
        "ingredient": {
            "ingredient_id": record.ingredient_id,
            "botanical_name": record.botanical_name,
            "common_name": record.common_name,
            "sanskrit_name": record.sanskrit_name,
            "synonyms": list(record.synonyms),
            "part_used": record.part_used,
            "marker_status": status,
            "markers": markers,
        },
        "taxonomy": {
            "tax_id": 1,
            "scientific_name": record.botanical_name,
            "rank": "species",
            "url": "https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=1",
            "retrieved_at": "2026-10-06T00:00:00+00:00",
            "source": "NCBI Taxonomy",
        },
        "literature": {
            "status": "ok",
            "sort": "relevance",
            "articles": [
                {
                    "pmid": "10000001",
                    "title": "Fixture pharmacology reference",
                    "year": "2020",
                    "url": "https://pubmed.ncbi.nlm.nih.gov/10000001/",
                }
            ],
            "retrieved_at": "2026-10-06T00:00:00+00:00",
        },
        "marker_status": status,
        "properties": properties,
    }


def envelope(spec: dict, lookup: SnapshotLookup | None = None) -> dict:
    client = lookup or legacy_lookup()
    approvals = [approval_for(item["ingredient_id"], client) for item in spec["ingredients"]]
    return {"spec": spec, "approvals": approvals}

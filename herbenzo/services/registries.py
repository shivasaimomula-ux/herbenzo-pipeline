"""Registries & Normalization shared service.

Component B cannot run without two curated tables that are *modelling decisions*,
not lookups, and therefore require named domain review:

* **Ingredient identity** — canonical botanical name, synonyms, plant part, stable ID.
* **Standardization markers** — which compound represents a multi-constituent extract.

Physicochemical descriptors are not curated: they are retrieved from PubChem and
stored with their provenance in ``herbenzo/data/marker_properties.json``.

The client is an interface so the same engine code runs against the MCP chemistry
connector in a notebook session and against direct PubChem REST inside a FastAPI
service. ``StaticRegistriesClient`` is the offline implementation backed by the
cached descriptor file; swap in a live client without touching the engine.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Protocol

from herbenzo.schemas.contracts import PhysicochemicalProfile
from herbenzo.services.overlay import load_approved_documents

_DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "marker_properties.json"

__all__ = [
    "MarkerRecord", "IngredientRecord", "RegistriesClient",
    "StaticRegistriesClient", "UnknownIngredient", "UnknownMarker",
    "normalize_extract_ratio", "marker_dose_mg", "reconcile_percentages",
    "iter_registry_records", "merged_ingredient_ids", "merged_ingredients",
]

_OVERLAY_ID = re.compile(r"^HB-[A-Z0-9]{3,16}$")


class UnknownIngredient(KeyError):
    """Ingredient ID is not in the registry — resolve identity before proceeding."""


class UnknownMarker(KeyError):
    """Marker has no retrieved descriptor record."""


@dataclass(frozen=True)
class MarkerRecord:
    """A standardization marker and the curated pharmacological flags attached to it."""

    marker_name: str
    rationale: str
    #: Known efflux-transporter substrate (e.g. P-glycoprotein). Descriptor-based
    #: permeability prediction cannot see efflux, so this must be curated.
    efflux_substrate: bool = False
    efflux_transporter: str | None = None
    #: Degrades at gastric pH — triggers an enteric/delayed-release recommendation.
    acid_labile: bool = False
    #: Every curated override must be reviewed and given a citation before release.
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


# ---------------------------------------------------------------------------
# Curated ingredient identity + marker assignment table.
# EVERY ROW HERE REQUIRES DOMAIN REVIEW BEFORE PRODUCTION USE.
# ---------------------------------------------------------------------------

_INGREDIENTS: dict[str, IngredientRecord] = {
    "HB-ASHW": IngredientRecord(
        "HB-ASHW", "Withania somnifera", "Ashwagandha", "Ashwagandha",
        ("Indian ginseng", "winter cherry"), "root",
        (MarkerRecord(
            "Withaferin A",
            "Principal withanolide; the class routinely used for standardization. "
            "A single withanolide is a proxy for the whole withanolide fraction.",
        ),),
    ),
    "HB-TURM": IngredientRecord(
        "HB-TURM", "Curcuma longa", "Turmeric", "Haridra",
        ("curcuma", "haldi"), "rhizome",
        (MarkerRecord(
            "Curcumin",
            "Principal curcuminoid; standard marker for curcuminoid-standardized extracts.",
        ),),
    ),
    "HB-BERB": IngredientRecord(
        "HB-BERB", "Berberis aristata", "Indian barberry", "Daruharidra",
        ("tree turmeric", "berberine source"), "root bark",
        (MarkerRecord(
            "Berberine",
            "Isoquinoline alkaloid; the standardization marker for berberine extracts.",
            efflux_substrate=True,
            efflux_transporter="P-glycoprotein (ABCB1)",
            override_requires_citation=True,
            evidence_note=(
                "Berberine's low oral bioavailability is attributed substantially to "
                "intestinal P-gp efflux rather than poor passive diffusion. Computed "
                "descriptors predict high passive permeability and therefore misclassify "
                "it; this curated override corrects the permeability call. "
                "REQUIRES a retrieved citation before release."
            ),
        ),),
    ),
    "HB-BOSW": IngredientRecord(
        "HB-BOSW", "Boswellia serrata", "Indian frankincense", "Shallaki",
        ("salai guggul",), "gum resin",
        (MarkerRecord(
            "3-O-acetyl-11-keto-beta-boswellic acid",
            "AKBA; the boswellic acid used for standardization of Boswellia extracts.",
        ),),
    ),
    "HB-PIPL": IngredientRecord(
        "HB-PIPL", "Piper longum", "Long pepper", "Pippali",
        ("pipli", "Indian long pepper"), "fruit",
        (MarkerRecord("Piperine", "Principal alkaloid and pharmacopoeial marker."),),
    ),
    "HB-HARI": IngredientRecord(
        "HB-HARI", "Terminalia chebula", "Chebulic myrobalan", "Haritaki",
        ("harad",), "pericarp of fruit",
        (MarkerRecord(
            "Chebulinic acid",
            "Characteristic hydrolysable tannin of T. chebula; proxy for the tannin fraction.",
        ),),
    ),
    "HB-BIBH": IngredientRecord(
        "HB-BIBH", "Terminalia bellirica", "Belleric myrobalan", "Bibhitaki",
        ("baheda",), "pericarp of fruit",
        (MarkerRecord("Gallic acid", "Commonly assayed phenolic marker for T. bellirica."),),
    ),
    "HB-AMLA": IngredientRecord(
        "HB-AMLA", "Phyllanthus emblica", "Indian gooseberry", "Amalaki",
        ("amla", "Emblica officinalis"), "fruit",
        (MarkerRecord("Ellagic acid", "Ellagitannin-derived marker used for amla extracts."),),
    ),
    "HB-CINN": IngredientRecord(
        "HB-CINN", "Cinnamomum verum", "True cinnamon", "Twak",
        ("Ceylon cinnamon", "Cinnamomum zeylanicum"), "stem bark",
        (MarkerRecord("Cinnamaldehyde", "Principal volatile constituent of cinnamon bark."),),
    ),
    "HB-ELAA": IngredientRecord(
        "HB-ELAA", "Elettaria cardamomum", "Green cardamom", "Ela",
        ("chhoti elaichi",), "seed",
        (MarkerRecord("Eucalyptol", "1,8-cineole; principal volatile marker of cardamom oil."),),
    ),
}

_SYNONYM_INDEX: dict[str, str] = {}
for _rec in _INGREDIENTS.values():
    for _name in (_rec.botanical_name, _rec.common_name, _rec.sanskrit_name, *_rec.synonyms):
        if _name:
            _SYNONYM_INDEX[_name.strip().lower()] = _rec.ingredient_id


def _record_from_overlay(doc: dict) -> IngredientRecord | None:
    ing = doc.get("ingredient")
    if not isinstance(ing, dict):
        return None
    ingredient_id = ing.get("ingredient_id")
    if not isinstance(ingredient_id, str) or not _OVERLAY_ID.fullmatch(ingredient_id):
        return None
    if ingredient_id in _INGREDIENTS:
        return None
    botanical = ing.get("botanical_name")
    common = ing.get("common_name")
    part = ing.get("part_used")
    if not isinstance(botanical, str) or not botanical.strip():
        return None
    if not isinstance(common, str) or not common.strip():
        return None
    if not isinstance(part, str) or not part.strip():
        return None
    markers: list[MarkerRecord] = []
    for raw in ing.get("markers") or []:
        if not isinstance(raw, dict):
            continue
        marker_name = raw.get("marker_name")
        rationale = raw.get("rationale")
        if isinstance(marker_name, str) and marker_name.strip() and isinstance(rationale, str) and rationale.strip():
            markers.append(MarkerRecord(marker_name.strip(), rationale.strip()))
    if not markers:
        return None
    synonyms = tuple(
        item.strip()
        for item in (ing.get("synonyms") or [])
        if isinstance(item, str) and item.strip()
    )
    sanskrit = ing.get("sanskrit_name")
    return IngredientRecord(
        ingredient_id=ingredient_id,
        botanical_name=botanical.strip(),
        common_name=common.strip(),
        sanskrit_name=sanskrit.strip() if isinstance(sanskrit, str) and sanskrit.strip() else None,
        synonyms=synonyms,
        part_used=part.strip(),
        markers=tuple(markers),
    )


def iter_registry_records() -> list[IngredientRecord]:
    """Stock rows, then approved overlay rows. Pending candidates are excluded."""
    records = list(_INGREDIENTS.values())
    seen = {rec.ingredient_id for rec in records}
    extra: list[IngredientRecord] = []
    for doc in load_approved_documents():
        rec = _record_from_overlay(doc)
        if rec is None or rec.ingredient_id in seen:
            continue
        extra.append(rec)
        seen.add(rec.ingredient_id)
    extra.sort(key=lambda rec: rec.ingredient_id)
    return records + extra


def merged_ingredients() -> dict[str, IngredientRecord]:
    return {rec.ingredient_id: rec for rec in iter_registry_records()}


def merged_ingredient_ids() -> set[str]:
    return set(merged_ingredients())


def _overlay_properties() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for doc in load_approved_documents():
        props = doc.get("properties")
        if not isinstance(props, dict):
            continue
        for name, row in props.items():
            if isinstance(name, str) and isinstance(row, dict) and name not in found:
                found[name] = row
    return found


class RegistriesClient(Protocol):
    """Interface consumed by the modernizer engine."""

    def lookup_ingredient(self, ingredient_id: str) -> IngredientRecord: ...
    def resolve_identity(self, name: str) -> str: ...
    def lookup_marker(self, ingredient_id: str) -> MarkerRecord: ...
    def get_physicochemical_properties(self, marker_name: str) -> PhysicochemicalProfile: ...


class StaticRegistriesClient:
    """Offline implementation backed by the cached PubChem descriptor file."""

    def __init__(self, data_path: pathlib.Path | None = None) -> None:
        self._props: dict[str, dict] = json.loads((data_path or _DATA).read_text())

    def lookup_ingredient(self, ingredient_id: str) -> IngredientRecord:
        rec = merged_ingredients().get(ingredient_id)
        if rec is None:
            raise UnknownIngredient(
                f"{ingredient_id!r} is not in the ingredient registry; "
                "resolve botanical identity before modernization"
            )
        return rec

    def resolve_identity(self, name: str) -> str:
        """Map a botanical/common/Sanskrit synonym to the canonical ingredient ID."""
        key = name.strip().lower()
        found = _SYNONYM_INDEX.get(key)
        if found:
            return found
        for rec in iter_registry_records():
            names = (rec.botanical_name, rec.common_name, rec.sanskrit_name, *rec.synonyms)
            if any(item and item.strip().lower() == key for item in names):
                return rec.ingredient_id
        raise UnknownIngredient(f"no registry entry matching {name!r}")

    def lookup_marker(self, ingredient_id: str) -> MarkerRecord:
        rec = self.lookup_ingredient(ingredient_id)
        if not rec.markers:
            raise UnknownMarker(f"no standardization marker assigned for {ingredient_id!r}")
        return rec.markers[0]

    def get_physicochemical_properties(self, marker_name: str) -> PhysicochemicalProfile:
        row = self._props.get(marker_name)
        if row is None:
            row = _overlay_properties().get(marker_name)
        if row is None:
            raise UnknownMarker(
                f"no retrieved descriptor record for marker {marker_name!r}"
            )
        return PhysicochemicalProfile(
            pubchem_cid=row["pubchem_cid"],
            molecular_weight=row["molecular_weight"],
            xlogp=row["xlogp"],
            tpsa=row["tpsa"],
            hbd=row["hbd"],
            hba=row["hba"],
            rotatable_bonds=row["rotatable_bonds"],
            source=row["source"],
        )


# ---------------------------------------------------------------------------
# Dose / unit normalization
# ---------------------------------------------------------------------------

def normalize_extract_ratio(quantity_mg: float, extract_ratio: str | None) -> float | None:
    """Crude-herb equivalent of an extract dose.

    ``normalize_extract_ratio(500, "10:1")`` → 5000.0 mg of native herb.
    Returns ``None`` when the ingredient is a crude powder (no ratio declared),
    because the crude equivalent is then the quantity itself and asserting a
    separate value would be misleading.
    """
    if extract_ratio is None:
        return None
    native, extract = (float(x) for x in extract_ratio.split(":"))
    return quantity_mg * (native / extract)


def marker_dose_mg(quantity_mg: float, standardized_percent: float | None) -> float | None:
    """Marker delivered per serving, from a declared standardization percentage."""
    if standardized_percent is None:
        return None
    return quantity_mg * standardized_percent / 100.0


def reconcile_percentages(parts: dict[str, float], tolerance: float = 1e-6) -> dict[str, float]:
    """Convert parts to percentages and assert they sum to exactly 100%.

    Raises ``ValueError`` rather than silently normalizing, so a composition error
    surfaces at the boundary instead of inside a generated document.
    """
    total = sum(parts.values())
    if total <= 0:
        raise ValueError("composition parts must sum to a positive total")
    pct = {k: v / total * 100.0 for k, v in parts.items()}
    if abs(sum(pct.values()) - 100.0) > tolerance:
        raise ValueError(f"percentages sum to {sum(pct.values())!r}, not 100%")
    return pct

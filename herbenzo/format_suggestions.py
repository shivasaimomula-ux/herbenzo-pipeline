"""Advisory finished-format suggestions.

Ranks candidate dosage forms for a list of registry ingredient ids. The ranker
is a curated catalog plus fixed rules. It does not call the delivery
recommender and it does not change ``ModernizedSKU``.

Chyawanprash-like and other classical preparation names do not drop
nanoemulsion. They keep it in the list, subtract a large penalty, and attach
an explicit owner caution while gummy and soft-chew formats receive a
preference bonus.

Market references live in ``herbenzo/data/modern_formats.json``. A reference
is included only when a public brand page was checked. Empty ``references``
means none was verified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from herbenzo.components.modernizer.bcs_classifier import classify
from herbenzo.services.registries import StaticRegistriesClient

__all__ = [
    "AUDIENCES",
    "CLASSICAL_TOKENS",
    "GUMMY_FORMAT_ID",
    "NANOEMULSION_FORMAT_ID",
    "OWNER_NANOEMULSION_CAUTION",
    "OWNER_NANOEMULSION_PENALTY",
    "SOFT_CHEW_FORMAT_ID",
    "CatalogError",
    "IngredientProfile",
    "is_classical_or_chyawanprash",
    "load_catalog",
    "rank_formats",
    "suggest_formats",
    "suggest_from_payload",
]

_DATA = Path(__file__).resolve().parent / "data"
_CATALOG_PATH = _DATA / "modern_formats.json"
_TRAITS_PATH = _DATA / "format_ingredient_traits.json"

AUDIENCES = ("kids", "teens", "adults", "elderly")
NANOEMULSION_FORMAT_ID = "nanoemulsion"
GUMMY_FORMAT_ID = "gummy"
SOFT_CHEW_FORMAT_ID = "soft_chew"

#: Owner rule: suggest, do not delete. Gummy and soft chew are preferred.
OWNER_NANOEMULSION_CAUTION = (
    "owner guidance: nanoemulsion generally not appropriate for Chyawanprash "
    "modernization; prefer gummy/chewy"
)
OWNER_NANOEMULSION_PENALTY = 55.0
_CLASSICAL_PREFER = {
    GUMMY_FORMAT_ID: 20.0,
    SOFT_CHEW_FORMAT_ID: 20.0,
}

CLASSICAL_TOKENS = frozenset(
    {
        "chyawanprash",
        "chyavanprash",
        "chyawanprasham",
        "chyavanaprasha",
        "chyawanaprash",
        "avaleha",
        "lehya",
        "lehyam",
        "leha",
        "churna",
        "kwath",
        "kwatha",
        "kashaya",
        "kashayam",
        "decoction",
        "bhasma",
        "arishta",
        "asava",
        "asavam",
        "ghrita",
        "taila",
        "tailam",
        "gutika",
        "vati",
    }
)

_MASK_TASTES = frozenset({"bitter", "astringent", "pungent", "sour"})
_TOKEN = re.compile(r"[a-z0-9]+")
_WORD = re.compile(r"[a-z0-9]+")

_BASE = 50.0
_AUDIENCE_HIT = 10.0
_AUDIENCE_MISS = -12.0
_FORM_MATCH = 14.0
_POLYHERBAL = 6.0
_LOW_SOL_HELP = 10.0
_LOW_SOL_UNNECESSARY = -6.0
_BCS_FIT = 6.0
_TASTE_STRONG = 16.0
_TASTE_MODERATE = 8.0
_TASTE_WEAK = -14.0
_HEAT_NONE = 16.0
_HEAT_LOW = 8.0
_HEAT_HIGH = -22.0
_WATER_WHEN_SENSITIVE = -6.0
_VOLATILE_OIL = 4.0
_ACID = -14.0


class CatalogError(ValueError):
    """The on-disk format catalog failed validation."""


class _Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: str = Field(min_length=1)
    category: str = Field(min_length=1)
    url: str = Field(min_length=1)
    note: str | None = None

    def as_public(self) -> dict[str, str]:
        row = {"brand": self.brand, "category": self.category, "url": self.url}
        if self.note:
            row["note"] = self.note
        return row


class _Format(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: str
    dosage_form_label: str = Field(min_length=1)
    aliases: list[str] = Field(min_length=1)
    audiences: list[str] = Field(min_length=1)
    taste_masking: str
    process_heat: str
    water_activity: str
    helps_low_solubility: bool
    protects_volatile: bool
    acidic: bool
    polyherbal: str
    preferred_bcs: list[str]
    max_active_mg: float = Field(gt=0)
    excipients: list[str] = Field(min_length=1)
    textbook_rationale: str = Field(min_length=1)
    constraints: list[str] = Field(min_length=1)
    references: list[_Reference]


class _CatalogFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_version: str = Field(min_length=1)
    about: str = Field(min_length=1)
    formats: list[_Format] = Field(min_length=1)


def _check_choices(fmt: _Format) -> None:
    if fmt.category not in {"textbook", "market"}:
        raise CatalogError(f"{fmt.format_id}: category must be textbook or market")
    if fmt.taste_masking not in {"strong", "moderate", "weak"}:
        raise CatalogError(f"{fmt.format_id}: taste_masking is invalid")
    if fmt.process_heat not in {"none", "low", "high"}:
        raise CatalogError(f"{fmt.format_id}: process_heat is invalid")
    if fmt.water_activity not in {"low", "high"}:
        raise CatalogError(f"{fmt.format_id}: water_activity is invalid")
    if fmt.polyherbal not in {"good", "limited"}:
        raise CatalogError(f"{fmt.format_id}: polyherbal is invalid")
    unknown = [aud for aud in fmt.audiences if aud not in AUDIENCES]
    if unknown:
        raise CatalogError(f"{fmt.format_id}: unknown audiences {unknown}")
    bad_bcs = [bcs for bcs in fmt.preferred_bcs if bcs not in {"I", "II", "III", "IV"}]
    if bad_bcs:
        raise CatalogError(f"{fmt.format_id}: unknown BCS classes {bad_bcs}")
    for ref in fmt.references:
        if not ref.url.startswith("https://"):
            raise CatalogError(f"{fmt.format_id}: reference URL must be https")


@lru_cache(maxsize=1)
def load_catalog() -> tuple[_Format, ...]:
    """Load and validate the finished-format catalog."""
    try:
        raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
        parsed = _CatalogFile.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise CatalogError(f"modern_formats.json failed validation: {exc}") from exc
    ids: list[str] = []
    for fmt in parsed.formats:
        _check_choices(fmt)
        ids.append(fmt.format_id)
    if len(ids) != len(set(ids)):
        raise CatalogError("duplicate format_id in modern_formats.json")
    if NANOEMULSION_FORMAT_ID not in ids:
        raise CatalogError("catalog is missing nanoemulsion")
    if GUMMY_FORMAT_ID not in ids or SOFT_CHEW_FORMAT_ID not in ids:
        raise CatalogError("catalog is missing gummy or soft_chew")
    return tuple(parsed.formats)


@lru_cache(maxsize=1)
def _traits() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(_TRAITS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"format_ingredient_traits.json could not be read: {exc}") from exc
    rows = raw.get("ingredients")
    if not isinstance(rows, dict):
        raise CatalogError("format_ingredient_traits.json needs an ingredients object")
    return rows


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def is_classical_or_chyawanprash(dosage_form: str | None, product_name: str | None) -> bool:
    """True when the requested form or product name is a classical preparation.

    Chyawanprash spellings and avaleha/lehya names are included, along with
    other classical form tokens (churna, kwath, and the rest of
    ``CLASSICAL_TOKENS``).
    """
    blob = f"{dosage_form or ''} {product_name or ''}"
    return any(token in CLASSICAL_TOKENS for token in _words(blob))


def _norm(text: str) -> str:
    return " ".join(_TOKEN.findall(text.lower()))


def _form_matches(fmt: _Format, dosage_form: str) -> bool:
    wanted = _norm(dosage_form)
    if not wanted:
        return False
    aliases = {_norm(alias) for alias in fmt.aliases}
    aliases.add(_norm(fmt.dosage_form_label))
    aliases.add(_norm(fmt.format_id.replace("_", " ")))
    aliases.discard("")
    if wanted in aliases:
        return True
    for alias in aliases:
        if re.search(rf"(^| ){re.escape(alias)}( |$)", wanted):
            return True
    return False


@dataclass(frozen=True)
class IngredientProfile:
    """Chemistry and organoleptic facts the ranker is allowed to see."""

    ingredient_id: str
    quantity_mg: float | None = None
    bcs_class: str = "I"
    solubility: str = "high"
    permeability: str = "high"
    xlogp: float | None = None
    molecular_weight: float | None = None
    tastes: tuple[str, ...] = ()
    heat_sensitive: bool = False
    volatile: bool = False
    acid_labile: bool = False


def profile_from_mapping(data: dict[str, Any]) -> IngredientProfile:
    """Build a profile from a fixture or test dict."""
    tastes = data.get("tastes")
    if tastes is None and data.get("taste"):
        tastes = [data["taste"]]
    taste_tuple = tuple(str(item) for item in (tastes or []) if item)
    quantity = data.get("quantity_mg")
    return IngredientProfile(
        ingredient_id=str(data["ingredient_id"]),
        quantity_mg=None if quantity is None else float(quantity),
        bcs_class=str(data.get("bcs_class") or "I"),
        solubility=str(data.get("solubility") or "high"),
        permeability=str(data.get("permeability") or "high"),
        xlogp=data.get("xlogp"),
        molecular_weight=data.get("molecular_weight"),
        tastes=taste_tuple,
        heat_sensitive=bool(data.get("heat_sensitive")),
        volatile=bool(data.get("volatile")),
        acid_labile=bool(data.get("acid_labile")),
    )


def profile_from_registry(
    ingredient_id: str,
    quantity_mg: float | None,
    registries: Any,
) -> IngredientProfile:
    """Resolve one registry id. Unknown ids raise ``UnknownIngredient``."""
    registries.lookup_ingredient(ingredient_id)
    marker = registries.lookup_marker(ingredient_id)
    props = registries.get_physicochemical_properties(marker.marker_name)
    assessment = classify(props, marker)
    bcs = assessment.bcs_class
    bcs_value = bcs.value if hasattr(bcs, "value") else str(bcs)
    traits = _traits().get(ingredient_id) or {}
    tastes = tuple(str(item) for item in traits.get("tastes") or () if item)
    return IngredientProfile(
        ingredient_id=ingredient_id,
        quantity_mg=quantity_mg,
        bcs_class=bcs_value,
        solubility=str(assessment.solubility_call),
        permeability=str(assessment.permeability_call),
        xlogp=props.xlogp,
        molecular_weight=props.molecular_weight,
        tastes=tastes,
        heat_sensitive=bool(traits.get("heat_sensitive")),
        volatile=bool(traits.get("volatile")),
        acid_labile=bool(marker.acid_labile),
    )


def _needs_mask(profiles: list[IngredientProfile]) -> bool:
    return any(taste in _MASK_TASTES for profile in profiles for taste in profile.tastes)


def _heat_sensitive(profiles: list[IngredientProfile]) -> bool:
    return any(profile.heat_sensitive or profile.volatile for profile in profiles)


def _clamp(score: float) -> float:
    return max(0.0, min(100.0, round(score, 2)))


def _score_one(
    fmt: _Format,
    profiles: list[IngredientProfile],
    *,
    audience: str | None,
    dosage_form: str | None,
    classical: bool,
) -> dict[str, Any]:
    score = _BASE
    reasons: list[str] = [fmt.textbook_rationale]
    cautions: list[str] = []

    if audience:
        if audience in fmt.audiences:
            score += _AUDIENCE_HIT
            reasons.append(f"Listed for {audience}.")
        else:
            score += _AUDIENCE_MISS
            cautions.append(f"Not listed as a primary format for {audience}.")

    if dosage_form and _form_matches(fmt, dosage_form):
        score += _FORM_MATCH
        reasons.append("Matches the requested finished form.")

    if len(profiles) >= 2 and fmt.polyherbal == "good":
        score += _POLYHERBAL
        reasons.append(f"Fits a {len(profiles)}-ingredient blend.")

    low_sol = [p.ingredient_id for p in profiles if p.solubility == "low"]
    if low_sol and fmt.helps_low_solubility:
        score += _LOW_SOL_HELP
        reasons.append(
            "At least one marker looks low-solubility; this format is tagged as a "
            "dispersion or dissolution aid."
        )
    elif profiles and not low_sol and fmt.helps_low_solubility:
        score += _LOW_SOL_UNNECESSARY
        reasons.append(
            "Markers look high-solubility, so a solubilizing carrier is not the first need."
        )

    if any(p.bcs_class in fmt.preferred_bcs for p in profiles):
        score += _BCS_FIT
        reasons.append("Provisional BCS class is one this format is usually chosen for.")

    if _needs_mask(profiles):
        if fmt.taste_masking == "strong":
            score += _TASTE_STRONG
            reasons.append(
                "Strong taste masking for bitter, astringent, pungent, or sour herbs."
            )
        elif fmt.taste_masking == "moderate":
            score += _TASTE_MODERATE
            reasons.append("Some taste masking; confirm flavor at the intended load.")
        else:
            score += _TASTE_WEAK
            cautions.append(
                "Weak taste masking; bitter, astringent, pungent, or sour herbs will be obvious."
            )

    if _heat_sensitive(profiles):
        if fmt.process_heat == "none":
            score += _HEAT_NONE
            reasons.append(
                "No cook step; a better default when a constituent is heat-sensitive or volatile."
            )
        elif fmt.process_heat == "low":
            score += _HEAT_LOW
            reasons.append(
                "Low process heat; still confirm a volatile or heat-sensitive fraction survives."
            )
        else:
            score += _HEAT_HIGH
            cautions.append(
                "High process heat can damage heat-sensitive or volatile constituents."
            )
        if fmt.water_activity == "high":
            score += _WATER_WHEN_SENSITIVE
            cautions.append(
                "High water activity is a weak default for heat-sensitive or volatile constituents."
            )

    if any(p.volatile for p in profiles) and fmt.protects_volatile:
        score += _VOLATILE_OIL
        reasons.append("An oil phase can hold a volatile constituent better than a dry powder.")

    if any(p.acid_labile for p in profiles) and fmt.acidic:
        score += _ACID
        cautions.append(
            "Acid-labile marker; an acidic vehicle is a poor fit."
        )

    declared = [p for p in profiles if p.quantity_mg is not None]
    total_mg = sum(p.quantity_mg or 0.0 for p in declared)
    if declared and total_mg > fmt.max_active_mg:
        ratio = total_mg / fmt.max_active_mg
        score -= min(24.0, 10.0 + 8.0 * (ratio - 1.0))
        cautions.append(
            f"Declared serving load {total_mg:.0f} mg is above the {fmt.max_active_mg:.0f} mg "
            "heuristic for this format. Split the dose or pick a higher-load form."
        )

    if classical:
        bonus = _CLASSICAL_PREFER.get(fmt.format_id)
        if bonus:
            score += bonus
            reasons.append(
                "Preferred finished style for a Chyawanprash-like or classical preparation."
            )
        if fmt.format_id == NANOEMULSION_FORMAT_ID:
            score -= OWNER_NANOEMULSION_PENALTY
            cautions.append(OWNER_NANOEMULSION_CAUTION)

    cautions.extend(fmt.constraints)
    return {
        "format_id": fmt.format_id,
        "name": fmt.name,
        "category": fmt.category,
        "score": _clamp(score),
        "fit_reasons": reasons,
        "cautions": cautions,
        "references": [ref.as_public() for ref in fmt.references],
        "dosage_form_label": fmt.dosage_form_label,
        "excipients": list(fmt.excipients),
    }


def rank_formats(
    profiles: list[IngredientProfile],
    *,
    audience: str | None = None,
    dosage_form: str | None = None,
    product_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return every catalog format, highest score first.

    Ties break on ``format_id`` so the same inputs always produce the same list.
    Nothing is removed for a classical or Chyawanprash-like request.
    """
    classical = is_classical_or_chyawanprash(dosage_form, product_name)
    ranked = [
        _score_one(
            fmt,
            profiles,
            audience=audience,
            dosage_form=dosage_form,
            classical=classical,
        )
        for fmt in load_catalog()
    ]
    ranked.sort(key=lambda row: (-row["score"], row["format_id"]))
    return ranked


def suggest_formats(
    ingredient_ids: list[str],
    *,
    audience: str | None = None,
    dosage_form: str | None = None,
    product_name: str | None = None,
    quantities_mg: dict[str, float] | None = None,
    registries: Any | None = None,
) -> dict[str, Any]:
    """Rank formats for registry ingredient ids.

    ``registries.lookup_ingredient`` raises ``UnknownIngredient`` for an id
    that is not in the stock registry. That is the caller's signal to return
    422.
    """
    if not ingredient_ids:
        raise ValueError("ingredient_ids must be a non-empty list")
    if len(ingredient_ids) != len(set(ingredient_ids)):
        raise ValueError("duplicate ingredient_id")
    client = registries if registries is not None else StaticRegistriesClient()
    quantities = quantities_mg or {}
    profiles = [
        profile_from_registry(ingredient_id, quantities.get(ingredient_id), client)
        for ingredient_id in ingredient_ids
    ]
    classical = is_classical_or_chyawanprash(dosage_form, product_name)
    return {
        "advisory_only": True,
        "changes_modernized_sku": False,
        "ingredient_ids": list(ingredient_ids),
        "audience": audience,
        "dosage_form": dosage_form,
        "product_name": product_name,
        "quantities_mg": {key: quantities[key] for key in ingredient_ids if key in quantities},
        "classical_like": classical,
        "suggestions": rank_formats(
            profiles,
            audience=audience,
            dosage_form=dosage_form,
            product_name=product_name,
        ),
    }


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    return text or None


def _positive_mg(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("quantity_mg must be a number greater than 0")
    number = float(value)
    if number <= 0:
        raise ValueError("quantity_mg must be a number greater than 0")
    return number


def _ids_and_quantities(payload: dict[str, Any]) -> tuple[list[str], dict[str, float]]:
    quantities: dict[str, float] = {}
    ids: list[str] | None = None

    if payload.get("ingredient_ids") is not None:
        raw_ids = payload["ingredient_ids"]
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("ingredient_ids must be a non-empty list")
        ids = []
        for item in raw_ids:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("ingredient_ids must be non-empty strings")
            ids.append(item.strip())

    raw_ingredients = payload.get("ingredients")
    if ids is None:
        if not isinstance(raw_ingredients, list) or not raw_ingredients:
            raise ValueError("ingredient_ids must be a non-empty list")
        ids = []
        for item in raw_ingredients:
            if isinstance(item, str):
                if not item.strip():
                    raise ValueError("ingredient_ids must be non-empty strings")
                ids.append(item.strip())
                continue
            if not isinstance(item, dict) or not isinstance(item.get("ingredient_id"), str):
                raise ValueError("ingredients entries need an ingredient_id")
            ingredient_id = item["ingredient_id"].strip()
            if not ingredient_id:
                raise ValueError("ingredients entries need an ingredient_id")
            ids.append(ingredient_id)
            if item.get("quantity_mg") is not None:
                quantities[ingredient_id] = _positive_mg(item["quantity_mg"])

    if len(ids) != len(set(ids)):
        raise ValueError("duplicate ingredient_id")

    if isinstance(raw_ingredients, list):
        for item in raw_ingredients:
            if not isinstance(item, dict):
                continue
            ingredient_id = item.get("ingredient_id")
            if not isinstance(ingredient_id, str):
                continue
            ingredient_id = ingredient_id.strip()
            if ingredient_id in ids and item.get("quantity_mg") is not None:
                quantities.setdefault(ingredient_id, _positive_mg(item["quantity_mg"]))

    raw_quantities = payload.get("quantities_mg")
    if raw_quantities is not None:
        if not isinstance(raw_quantities, dict):
            raise ValueError("quantities_mg must be an object")
        for key, value in raw_quantities.items():
            if not isinstance(key, str) or key not in ids:
                raise ValueError("quantities_mg keys must be selected ingredient ids")
            quantities[key] = _positive_mg(value)
    return ids, quantities


def suggest_from_payload(payload: dict[str, Any], registries: Any | None = None) -> dict[str, Any]:
    """Validate a ``POST /suggest-formats`` body and rank formats."""
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    ids, quantities = _ids_and_quantities(payload)
    audience = _optional_text(payload.get("audience"), "audience")
    if audience is not None:
        audience = audience.lower()
        if audience not in AUDIENCES:
            joined = ", ".join(AUDIENCES)
            raise ValueError(f"audience must be one of: {joined}")
    return suggest_formats(
        ids,
        audience=audience,
        dosage_form=_optional_text(payload.get("dosage_form"), "dosage_form"),
        product_name=_optional_text(payload.get("product_name"), "product_name"),
        quantities_mg=quantities,
        registries=registries,
    )

"""Typed handoff contracts for the Herbenzo pipeline (green arrows, v2 architecture).

Every stage boundary is a validated Pydantic v2 model. Two invariants are enforced
here rather than left to convention:

1. **Strict schema.** Unknown fields are rejected (``extra="forbid"``); a malformed
   handoff raises instead of propagating silently downstream.
2. **Confidence floor.** A downstream contract may only maintain or *lower* the
   confidence it inherited. Raising it is a validation error.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "EvidenceTier", "BCSClass", "DeliveryTechnology",
    "IngredientSpec", "FormulationSpec",
    "MarkerResolution", "PhysicochemicalProfile", "BCSAssessment",
    "BioavailabilityEvidence", "DeliveryRecommendation",
    "ModernizedIngredient", "ModernizedSKU",
    "ConfidenceFloorViolation",
]


class ConfidenceFloorViolation(ValueError):
    """Raised when a stage attempts to raise the confidence it inherited."""


class EvidenceTier(str, Enum):
    IN_VITRO = "in_vitro"
    ANIMAL = "animal"
    HUMAN_OBSERVATIONAL = "human_observational"
    HUMAN_RCT = "human_rct"
    META_ANALYSIS = "meta_analysis"


class BCSClass(str, Enum):
    I = "I"      # high solubility, high permeability
    II = "II"    # low solubility, high permeability
    III = "III"  # high solubility, low permeability
    IV = "IV"    # low solubility, low permeability


class DeliveryTechnology(str, Enum):
    CONVENTIONAL = "conventional_powder_or_capsule"
    PHYTOSOME = "phytosome_phospholipid_complex"
    SNEDDS = "self_nanoemulsifying_drug_delivery_system"
    NANOEMULSION = "nanoemulsion"
    LIPOSOME = "liposome"
    CYCLODEXTRIN = "cyclodextrin_inclusion_complex"
    SOLID_DISPERSION = "amorphous_solid_dispersion"
    NANOPARTICLE = "polymeric_nanoparticle_encapsulation"
    PERMEATION_ENHANCER = "permeation_enhancer_system"
    MUCOADHESIVE = "mucoadhesive_polymer_matrix"
    ENTERIC = "enteric_coated_delayed_release"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True,
                              use_enum_values=False, str_strip_whitespace=True)


# --------------------------------------------------------------------------
# Input contract — from Component A (Recommender)
# --------------------------------------------------------------------------

class IngredientSpec(_Strict):
    """One ingredient as recommended by Component A."""

    ingredient_id: str = Field(min_length=1, description="Stable internal registry ID")
    botanical_name: str = Field(min_length=1, description="Genus species")
    common_name: str | None = None
    part_used: str | None = None

    quantity_mg: float = Field(gt=0, description="Amount per serving, milligrams")

    extract_ratio: str | None = Field(
        default=None,
        description="Native drug-to-extract ratio, e.g. '10:1'. None for crude powder.",
    )
    standardized_marker: str | None = Field(
        default=None, description="Marker the extract is standardized to, if any"
    )
    standardized_percent: float | None = Field(
        default=None, ge=0, le=100,
        description="Declared marker content as percent w/w of the extract",
    )

    @field_validator("extract_ratio")
    @classmethod
    def _ratio_form(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("extract_ratio must be of the form '<native>:<extract>', e.g. '10:1'")
        try:
            native, extract = float(parts[0]), float(parts[1])
        except ValueError as exc:  # noqa: TRY200
            raise ValueError("extract_ratio components must be numeric") from exc
        if native <= 0 or extract <= 0:
            raise ValueError("extract_ratio components must be positive")
        return v

    @model_validator(mode="after")
    def _marker_consistency(self) -> "IngredientSpec":
        if self.standardized_percent is not None and self.standardized_marker is None:
            raise ValueError(
                "standardized_percent supplied without standardized_marker — "
                "a percentage is meaningless without naming the marker it refers to"
            )
        return self


class FormulationSpec(_Strict):
    """Component A → Component B handoff."""

    formulation_id: str = Field(min_length=1)
    product_name: str = Field(min_length=1)
    dosage_form: str = Field(min_length=1)
    target_market: Literal["US", "EU", "NZ", "AU", "IN", "UK", "CA"]

    servings_per_day: int = Field(gt=0, default=1)
    ingredients: list[IngredientSpec] = Field(min_length=1)

    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence in the recommendation, carried forward as a floor",
    )
    source_stage: str = Field(default="A:recommender")

    @model_validator(mode="after")
    def _unique_ingredient_ids(self) -> "FormulationSpec":
        ids = [i.ingredient_id for i in self.ingredients]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate ingredient_id in formulation")
        return self


# --------------------------------------------------------------------------
# Output contract — to Component C (Monograph Generator)
# --------------------------------------------------------------------------

class PhysicochemicalProfile(_Strict):
    """Descriptors for a marker compound, as retrieved — never invented."""

    pubchem_cid: int
    molecular_weight: float = Field(gt=0)
    xlogp: float | None = None
    tpsa: float = Field(ge=0)
    hbd: int = Field(ge=0)
    hba: int = Field(ge=0)
    rotatable_bonds: int = Field(ge=0)
    measured_solubility_mg_per_ml: float | None = Field(
        default=None, ge=0,
        description="Experimental aqueous solubility, when a cited value exists",
    )
    solubility_source_pmid: str | None = None
    source: str = Field(description="Provenance of the descriptor values")

    @model_validator(mode="after")
    def _measured_needs_source(self) -> "PhysicochemicalProfile":
        if self.measured_solubility_mg_per_ml is not None and not self.solubility_source_pmid:
            raise ValueError(
                "measured_solubility_mg_per_ml requires solubility_source_pmid — "
                "an experimental value must carry its citation"
            )
        return self


class MarkerResolution(_Strict):
    """Which compound was used to represent an ingredient, and how confident that is.

    ``marker_status="pending"`` means no compound was chosen. Properties stay
    empty so a missing marker is not filled in with invented descriptors.
    """

    ingredient_id: str
    marker_name: str | None = None
    marker_status: Literal["resolved", "pending"] = "resolved"
    standardization: Literal["standardized", "unstandardized"] = "standardized"
    rationale: str
    is_proxy: bool = Field(
        default=True,
        description="True when a single marker stands in for a multi-constituent extract",
    )
    properties: PhysicochemicalProfile | None = None

    @model_validator(mode="after")
    def _pending_has_no_invented_compound(self) -> "MarkerResolution":
        if self.marker_status == "pending":
            if self.properties is not None:
                raise ValueError(
                    "a pending marker must not carry physicochemical properties"
                )
            # validate_assignment re-enters this validator, so write directly.
            object.__setattr__(self, "marker_name", None)
            object.__setattr__(self, "standardization", "unstandardized")
            object.__setattr__(self, "is_proxy", False)
            return self
        if not self.marker_name or self.properties is None:
            raise ValueError("a resolved marker requires a name and PubChem properties")
        object.__setattr__(self, "standardization", "standardized")
        return self


class BCSAssessment(_Strict):
    """Provisional BCS classification.

    This is a *predicted* class derived from computed descriptors unless a measured
    solubility or a curated transporter override was applied. It is not a regulatory
    BCS determination, which requires measured equilibrium solubility across the
    physiological pH range and human permeability data.
    """

    bcs_class: BCSClass | None = None
    solubility_call: Literal["high", "low", "unknown"]
    permeability_call: Literal["high", "low", "unknown"]
    evidence_basis: Literal[
        "computed_descriptors", "measured_solubility", "curated_override", "unstandardized"
    ]
    rationale: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    is_regulatory_determination: Literal[False] = False
    marker_status: Literal["resolved", "pending"] = "resolved"

    @model_validator(mode="after")
    def _class_follows_marker(self) -> "BCSAssessment":
        if self.marker_status == "pending":
            object.__setattr__(self, "bcs_class", None)
            object.__setattr__(self, "solubility_call", "unknown")
            object.__setattr__(self, "permeability_call", "unknown")
            object.__setattr__(self, "evidence_basis", "unstandardized")
            return self
        if self.bcs_class is None:
            raise ValueError("BCS class is required when a marker is resolved")
        return self


class BioavailabilityEvidence(_Strict):
    """A quantitative bioavailability claim — admissible only with a citation.

    The platform's standing rule is that no numeric enhancement factor may be asserted
    for a formulation that has not been made and measured. This model makes that rule
    structural: ``fold_change`` cannot exist without ``pmid``, ``evidence_tier`` and the
    model system it was measured in.
    """

    fold_change: float | None = Field(default=None, gt=0)
    fold_change_range: tuple[float, float] | None = None
    pmid: str | None = None
    evidence_tier: EvidenceTier | None = None
    model_system: str | None = Field(
        default=None, description="e.g. 'healthy human volunteers', 'Wistar rat'"
    )
    applies_to_same_delivery_system: bool | None = None
    qualitative_expectation: str | None = Field(
        default=None,
        description="Used when no citation supports a number; e.g. 'increase expected, unquantified'",
    )

    @model_validator(mode="after")
    def _number_requires_citation(self) -> "BioavailabilityEvidence":
        has_number = self.fold_change is not None or self.fold_change_range is not None
        if has_number:
            missing = [f for f, v in (
                ("pmid", self.pmid),
                ("evidence_tier", self.evidence_tier),
                ("model_system", self.model_system),
            ) if not v]
            if missing:
                raise ValueError(
                    "a quantitative bioavailability claim requires "
                    f"{', '.join(missing)} — unsupported numbers are not admissible"
                )
        if not has_number and not self.qualitative_expectation:
            raise ValueError(
                "supply either a cited fold_change or a qualitative_expectation"
            )
        return self


class DeliveryRecommendation(_Strict):
    primary: DeliveryTechnology | None = None
    alternatives: list[DeliveryTechnology] = Field(default_factory=list)
    rationale: list[str] = Field(min_length=1)
    excipients: list[str] = Field(default_factory=list)
    active_unchanged: Literal[True] = Field(
        default=True,
        description="Invariant: the herbal active itself is never modified, only its carrier",
    )
    bioavailability: BioavailabilityEvidence
    advisory_only: Literal[True] = True
    marker_status: Literal["resolved", "pending"] = "resolved"
    standardization: Literal["standardized", "unstandardized"] = "standardized"

    @model_validator(mode="after")
    def _carrier_follows_marker(self) -> "DeliveryRecommendation":
        if self.marker_status == "pending":
            object.__setattr__(self, "primary", None)
            object.__setattr__(self, "alternatives", [])
            object.__setattr__(self, "excipients", [])
            object.__setattr__(self, "standardization", "unstandardized")
            return self
        if self.primary is None:
            raise ValueError("delivery primary is required when a marker is resolved")
        object.__setattr__(self, "standardization", "standardized")
        return self


class ModernizedIngredient(_Strict):
    ingredient_id: str
    botanical_name: str
    quantity_mg: float = Field(gt=0)
    crude_equivalent_mg: float | None = Field(default=None, gt=0)
    marker_dose_mg: float | None = Field(default=None, ge=0)
    marker_status: Literal["resolved", "pending"] = "resolved"
    marker: MarkerResolution
    bcs: BCSAssessment
    delivery: DeliveryRecommendation


class ModernizedSKU(_Strict):
    """Component B → Component C handoff."""

    sku_id: str = Field(min_length=1)
    source_formulation_id: str = Field(min_length=1)
    product_name: str = Field(min_length=1)
    dosage_form: str = Field(min_length=1)
    target_market: Literal["US", "EU", "NZ", "AU", "IN", "UK", "CA"]
    servings_per_day: int = Field(gt=0)

    ingredients: list[ModernizedIngredient] = Field(min_length=1)

    confidence: float = Field(ge=0.0, le=1.0)
    inherited_confidence: float = Field(ge=0.0, le=1.0)
    source_stage: str = Field(default="B:modernizer")
    generated_at: _dt.datetime = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    engine_version: str

    @model_validator(mode="after")
    def _confidence_floor(self) -> "ModernizedSKU":
        if self.confidence > self.inherited_confidence + 1e-9:
            raise ConfidenceFloorViolation(
                f"confidence {self.confidence} exceeds inherited {self.inherited_confidence}; "
                "a downstream stage may only maintain or lower the confidence floor"
            )
        return self

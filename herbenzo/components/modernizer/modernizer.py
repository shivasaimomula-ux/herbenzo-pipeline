"""Component B — Modernizer orchestrator.

Consumes a validated ``FormulationSpec`` from Component A and emits a validated
``ModernizedSKU`` for Component C.

Pipeline: marker resolution → BCS classification → delivery-system selection,
with dose normalization applied per ingredient.

Failure posture: an ingredient that is not on this request's approval snapshot
raises rather than being guessed at. An approved ingredient with no marker is
not a failure and is not left off the SKU. It is included as unstandardized,
with ``marker_status`` pending, and named on ``classical_active_marker_gap``.
No marker, BCS class, or delivery system is invented for it. A formulation
that fails schema validation is rejected at the boundary. Confidence can only
fall, and the indicator itself does not lower it.
"""

from __future__ import annotations

from statistics import fmean
from typing import Any

from herbenzo.components.modernizer.bcs_classifier import classify
from herbenzo.components.modernizer.delivery_recommender import recommend
from herbenzo.schemas.contracts import (
    BCSAssessment,
    BioavailabilityEvidence,
    DeliveryRecommendation,
    FormulationSpec,
    MarkerResolution,
    ModernizedIngredient,
    ModernizedSKU,
)
from herbenzo.services.classical_marker_gap import classical_active_marker_gap
from herbenzo.services.records import (
    SnapshotLookup,
    marker_dose_mg,
    normalize_extract_ratio,
)

__all__ = ["ModernizerEngine", "ENGINE_VERSION"]

ENGINE_VERSION = "B-modernizer/1.0.0"

#: Penalty applied when a marker is a single-compound proxy for a whole extract.
_PROXY_PENALTY = 0.05


class ModernizerEngine:
    """Component B engine."""

    def __init__(
        self,
        lookup: SnapshotLookup | None = None,
        engine_version: str = ENGINE_VERSION,
        registries: SnapshotLookup | None = None,
    ) -> None:
        # ``registries`` is the previous constructor name. It is a request snapshot,
        # not a stored ingredient list.
        self.lookup: SnapshotLookup = lookup or registries or SnapshotLookup()
        self.registries = self.lookup
        self.engine_version = engine_version
        self.classical_active_marker_gap: dict | None = None

    # -- public API ---------------------------------------------------------

    def modernize(
        self,
        spec: FormulationSpec | dict[str, Any],
        bioavailability_evidence: dict[str, BioavailabilityEvidence] | None = None,
    ) -> ModernizedSKU:
        """Run the modernization pipeline.

        ``bioavailability_evidence`` maps ingredient_id → a cited fold-change
        retrieved and adjudicated upstream. Anything absent from it yields a
        qualitative expectation rather than an asserted number.

        Every approved ingredient is on the SKU. One without a marker is
        unstandardized and pending. The indicator on
        ``classical_active_marker_gap`` names that gap. No chemistry is
        fabricated to fill it.
        """
        self.classical_active_marker_gap = None
        spec = self._validate_input(spec)
        evidence = bioavailability_evidence or {}
        indicator = classical_active_marker_gap(spec, self.lookup)
        self.classical_active_marker_gap = indicator
        gapped = {
            item["ingredient_id"]
            for item in (indicator or {}).get("ingredients", ())
        }

        entries: list[ModernizedIngredient] = []
        for ing in spec.ingredients:
            if ing.ingredient_id in gapped:
                entries.append(_pending_ingredient(ing))
                continue
            marker_rec = self.lookup.lookup_marker(ing.ingredient_id)
            props = self.lookup.get_physicochemical_properties(marker_rec.marker_name)

            assessment = classify(props, marker_rec)
            delivery = recommend(assessment, marker_rec, evidence.get(ing.ingredient_id))

            resolution = MarkerResolution(
                ingredient_id=ing.ingredient_id,
                marker_name=marker_rec.marker_name,
                marker_status="resolved",
                rationale=marker_rec.rationale,
                is_proxy=True,
                properties=props,
            )

            entries.append(
                ModernizedIngredient(
                    ingredient_id=ing.ingredient_id,
                    botanical_name=ing.botanical_name,
                    quantity_mg=ing.quantity_mg,
                    crude_equivalent_mg=normalize_extract_ratio(
                        ing.quantity_mg, ing.extract_ratio
                    ),
                    marker_dose_mg=marker_dose_mg(ing.quantity_mg, ing.standardized_percent),
                    marker_status="resolved",
                    marker=resolution,
                    bcs=assessment,
                    delivery=delivery,
                )
            )

        return ModernizedSKU(
            sku_id=f"SKU-{spec.formulation_id}",
            source_formulation_id=spec.formulation_id,
            product_name=spec.product_name,
            dosage_form=spec.dosage_form,
            target_market=spec.target_market,
            servings_per_day=spec.servings_per_day,
            ingredients=entries,
            confidence=self._derive_confidence(spec, entries),
            inherited_confidence=spec.confidence,
            engine_version=self.engine_version,
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _validate_input(spec: FormulationSpec | dict[str, Any]) -> FormulationSpec:
        if isinstance(spec, FormulationSpec):
            return spec
        # Raises pydantic.ValidationError on schema mismatch — the handoff is rejected,
        # not coerced.
        return FormulationSpec.model_validate(spec)

    @staticmethod
    def _derive_confidence(
        spec: FormulationSpec, entries: list[ModernizedIngredient]
    ) -> float:
        """Confidence after modernization — never above what was inherited.

        Component B adds its own uncertainty (descriptor-based classification, proxy
        markers) on top of Component A's. It cannot recover confidence A did not have.
        """
        resolved = [entry for entry in entries if entry.marker_status == "resolved"]
        if not resolved:
            return round(float(spec.confidence), 4)
        own = fmean(entry.bcs.confidence for entry in resolved)
        if any(entry.marker.is_proxy for entry in resolved):
            own = max(0.0, own - _PROXY_PENALTY)
        return round(min(spec.confidence, own), 4)


_PENDING_RATIONALE = (
    "No PubChem standardization marker on this approved ingredient. "
    "QC, specification, and label claims that need a marker are unstandardized. "
    "Requires a marker before release."
)


def _pending_ingredient(ing) -> ModernizedIngredient:
    """Dose and identity only. No compound, class, or carrier is invented."""
    return ModernizedIngredient(
        ingredient_id=ing.ingredient_id,
        botanical_name=ing.botanical_name,
        quantity_mg=ing.quantity_mg,
        crude_equivalent_mg=normalize_extract_ratio(ing.quantity_mg, ing.extract_ratio),
        marker_dose_mg=None,
        marker_status="pending",
        marker=MarkerResolution(
            ingredient_id=ing.ingredient_id,
            marker_status="pending",
            rationale=_PENDING_RATIONALE,
            properties=None,
        ),
        bcs=BCSAssessment(
            marker_status="pending",
            solubility_call="unknown",
            permeability_call="unknown",
            evidence_basis="unstandardized",
            rationale=["Unstandardized: no marker descriptors, so no BCS class is assigned."],
            confidence=0.0,
        ),
        delivery=DeliveryRecommendation(
            marker_status="pending",
            rationale=[
                "No delivery technology is selected until a standardization marker is set. "
                "Requires a marker before release."
            ],
            bioavailability=BioavailabilityEvidence(
                qualitative_expectation="unstandardized; requires a marker before release",
            ),
        ),
    )

"""Component B — Modernizer orchestrator.

Consumes a validated ``FormulationSpec`` from Component A and emits a validated
``ModernizedSKU`` for Component C.

Pipeline: marker resolution → BCS classification → delivery-system selection,
with dose normalization applied per ingredient.

Failure posture: an ingredient that cannot be resolved in the registry raises
rather than being guessed at. A formulation that fails schema validation is
rejected at the boundary. Confidence can only fall.
"""

from __future__ import annotations

from statistics import fmean
from typing import Any

from herbenzo.components.modernizer.bcs_classifier import classify
from herbenzo.components.modernizer.delivery_recommender import recommend
from herbenzo.schemas.contracts import (
    BioavailabilityEvidence,
    FormulationSpec,
    MarkerResolution,
    ModernizedIngredient,
    ModernizedSKU,
)
from herbenzo.services.registries import (
    RegistriesClient,
    StaticRegistriesClient,
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
        registries: RegistriesClient | None = None,
        engine_version: str = ENGINE_VERSION,
    ) -> None:
        self.registries: RegistriesClient = registries or StaticRegistriesClient()
        self.engine_version = engine_version

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
        """
        spec = self._validate_input(spec)
        evidence = bioavailability_evidence or {}

        entries: list[ModernizedIngredient] = []
        for ing in spec.ingredients:
            marker_rec = self.registries.lookup_marker(ing.ingredient_id)
            props = self.registries.get_physicochemical_properties(marker_rec.marker_name)

            assessment = classify(props, marker_rec)
            delivery = recommend(assessment, marker_rec, evidence.get(ing.ingredient_id))

            resolution = MarkerResolution(
                ingredient_id=ing.ingredient_id,
                marker_name=marker_rec.marker_name,
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
        own = fmean(e.bcs.confidence for e in entries)
        if any(e.marker.is_proxy for e in entries):
            own = max(0.0, own - _PROXY_PENALTY)
        return round(min(spec.confidence, own), 4)

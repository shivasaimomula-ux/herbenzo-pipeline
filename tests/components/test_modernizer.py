"""Acceptance tests for Component B (Modernizer)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from herbenzo.components.modernizer.bcs_classifier import classify
from herbenzo.components.modernizer.modernizer import ModernizerEngine
from herbenzo.schemas.contracts import (
    BCSClass,
    BioavailabilityEvidence,
    ConfidenceFloorViolation,
    DeliveryTechnology as DT,
    EvidenceTier,
    FormulationSpec,
    ModernizedSKU,
    PhysicochemicalProfile,
)
from herbenzo.services.records import (
    MarkerRecord,
    ResearchError,
    marker_dose_mg,
    normalize_extract_ratio,
    reconcile_percentages,
)
from tests.legacy_snapshot import legacy_lookup


@pytest.fixture(scope="module")
def engine() -> ModernizerEngine:
    return ModernizerEngine(legacy_lookup())


def _spec(ingredient_id: str, botanical: str, **kw) -> dict:
    base = {
        "formulation_id": "F-TEST-001",
        "product_name": "Test Product",
        "dosage_form": "capsule",
        "target_market": "US",
        "servings_per_day": 2,
        "confidence": 0.9,
        "ingredients": [
            {
                "ingredient_id": ingredient_id,
                "botanical_name": botanical,
                "quantity_mg": 500.0,
                **kw,
            }
        ],
    }
    return base


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------

class TestContractValidation:
    def test_rejects_unknown_field(self, engine):
        spec = _spec("HB-TURM", "Curcuma longa")
        spec["hallucinated_field"] = "should not be accepted"
        with pytest.raises(ValidationError):
            engine.modernize(spec)

    def test_rejects_missing_required_field(self, engine):
        spec = _spec("HB-TURM", "Curcuma longa")
        del spec["target_market"]
        with pytest.raises(ValidationError):
            engine.modernize(spec)

    def test_rejects_non_positive_quantity(self, engine):
        spec = _spec("HB-TURM", "Curcuma longa")
        spec["ingredients"][0]["quantity_mg"] = 0
        with pytest.raises(ValidationError):
            engine.modernize(spec)

    def test_rejects_out_of_range_confidence(self, engine):
        spec = _spec("HB-TURM", "Curcuma longa")
        spec["confidence"] = 1.4
        with pytest.raises(ValidationError):
            engine.modernize(spec)

    def test_rejects_malformed_extract_ratio(self, engine):
        spec = _spec("HB-TURM", "Curcuma longa", extract_ratio="ten-to-one")
        with pytest.raises(ValidationError):
            engine.modernize(spec)

    def test_rejects_percent_without_named_marker(self, engine):
        spec = _spec("HB-TURM", "Curcuma longa", standardized_percent=95.0)
        with pytest.raises(ValidationError):
            engine.modernize(spec)

    def test_rejects_duplicate_ingredient_ids(self, engine):
        spec = _spec("HB-TURM", "Curcuma longa")
        spec["ingredients"].append(dict(spec["ingredients"][0]))
        with pytest.raises(ValidationError):
            engine.modernize(spec)

    def test_unknown_ingredient_raises_rather_than_guessing(self, engine):
        spec = _spec("HB-NOPE", "Nonexistentia fictiva")
        with pytest.raises(ResearchError) as raised:
            engine.modernize(spec)
        assert raised.value.code == "not_approved"


# ---------------------------------------------------------------------------
# Confidence floor invariant
# ---------------------------------------------------------------------------

class TestConfidenceFloor:
    def test_output_confidence_never_exceeds_input(self, engine):
        for inherited in (0.2, 0.5, 0.75, 0.95, 1.0):
            spec = _spec("HB-ASHW", "Withania somnifera")
            spec["confidence"] = inherited
            sku = engine.modernize(spec)
            assert sku.confidence <= inherited + 1e-9
            assert sku.inherited_confidence == inherited

    def test_low_input_confidence_clamps_output(self, engine):
        spec = _spec("HB-ASHW", "Withania somnifera")
        spec["confidence"] = 0.10
        sku = engine.modernize(spec)
        assert sku.confidence == pytest.approx(0.10)

    def test_contract_rejects_raised_confidence(self):
        with pytest.raises((ConfidenceFloorViolation, ValidationError)):
            ModernizedSKU.model_validate(
                {
                    "sku_id": "SKU-X",
                    "source_formulation_id": "F-X",
                    "product_name": "X",
                    "dosage_form": "capsule",
                    "target_market": "US",
                    "servings_per_day": 1,
                    "ingredients": _MINIMAL_INGREDIENT_LIST(),
                    "confidence": 0.99,
                    "inherited_confidence": 0.40,
                    "engine_version": "test",
                }
            )


def _MINIMAL_INGREDIENT_LIST() -> list[dict]:
    """A structurally valid single-ingredient payload for contract-level tests."""
    return [
        {
            "ingredient_id": "HB-TURM",
            "botanical_name": "Curcuma longa",
            "quantity_mg": 500.0,
            "marker": {
                "ingredient_id": "HB-TURM",
                "marker_name": "Curcumin",
                "rationale": "test",
                "is_proxy": True,
                "properties": {
                    "pubchem_cid": 969516,
                    "molecular_weight": 368.4,
                    "xlogp": 3.2,
                    "tpsa": 93.1,
                    "hbd": 2,
                    "hba": 6,
                    "rotatable_bonds": 8,
                    "source": "test",
                },
            },
            "bcs": {
                "bcs_class": "II",
                "solubility_call": "low",
                "permeability_call": "high",
                "evidence_basis": "computed_descriptors",
                "rationale": ["test"],
                "confidence": 0.6,
            },
            "delivery": {
                "primary": "phytosome_phospholipid_complex",
                "alternatives": [],
                "rationale": ["test"],
                "excipients": [],
                "bioavailability": {"qualitative_expectation": "unquantified"},
            },
        }
    ]


# ---------------------------------------------------------------------------
# Golden cases from the specification
# ---------------------------------------------------------------------------

class TestGoldenCases:
    def test_curcumin_class_ii_lipid_based_delivery(self, engine):
        spec = _spec(
            "HB-TURM", "Curcuma longa",
            standardized_marker="Curcumin", standardized_percent=95.0,
        )
        sku = engine.modernize(spec)
        ing = sku.ingredients[0]
        assert ing.marker.marker_name == "Curcumin"
        assert ing.bcs.bcs_class in (BCSClass.II, BCSClass.IV)
        assert ing.bcs.solubility_call == "low"
        assert ing.delivery.primary in (
            DT.PHYTOSOME, DT.LIPOSOME, DT.SNEDDS, DT.NANOEMULSION,
        )
        # 95% standardized, 500 mg extract → 475 mg curcuminoids per serving
        assert ing.marker_dose_mg == pytest.approx(475.0)

    def test_ashwagandha_withanolides_class_ii_enhanced_delivery(self, engine):
        spec = _spec(
            "HB-ASHW", "Withania somnifera",
            extract_ratio="10:1",
            standardized_marker="Withaferin A", standardized_percent=5.0,
        )
        sku = engine.modernize(spec)
        ing = sku.ingredients[0]
        assert ing.marker.marker_name == "Withaferin A"
        assert ing.bcs.bcs_class is BCSClass.II
        assert ing.delivery.primary is not DT.CONVENTIONAL
        assert ing.crude_equivalent_mg == pytest.approx(5000.0)
        assert ing.marker_dose_mg == pytest.approx(25.0)

    def test_berberine_class_iv_via_efflux_override(self, engine):
        spec = _spec("HB-BERB", "Berberis aristata")
        sku = engine.modernize(spec)
        ing = sku.ingredients[0]
        assert ing.marker.marker_name == "Berberine"
        # Descriptors alone predict high passive permeability; the curated P-gp
        # override is what produces the correct Class IV call.
        assert ing.bcs.bcs_class is BCSClass.IV
        assert ing.bcs.permeability_call == "low"
        assert ing.bcs.evidence_basis == "curated_override"
        assert any("efflux" in r.lower() or "P-glycoprotein" in r for r in ing.bcs.rationale)
        assert ing.delivery.primary in (DT.PHYTOSOME, DT.NANOPARTICLE)
        assert DT.NANOPARTICLE in ing.delivery.alternatives

    def test_berberine_without_override_would_be_class_ii(self, engine):
        """Guards the override: it must be the thing doing the work, not a coincidence."""
        client = legacy_lookup()
        props = client.get_physicochemical_properties("Berberine")
        assert classify(props, marker=None).bcs_class is BCSClass.II


# ---------------------------------------------------------------------------
# Quantitative-claim guardrail
# ---------------------------------------------------------------------------

class TestBioavailabilityGuardrail:
    def test_bare_fold_change_is_rejected(self):
        with pytest.raises(ValidationError):
            BioavailabilityEvidence(fold_change=2.0)

    def test_fold_change_requires_full_provenance(self):
        with pytest.raises(ValidationError):
            BioavailabilityEvidence(fold_change=2.0, pmid="12345678")

    def test_cited_fold_change_is_accepted(self):
        ev = BioavailabilityEvidence(
            fold_change=2.0,
            pmid="12345678",
            evidence_tier=EvidenceTier.HUMAN_RCT,
            model_system="healthy human volunteers",
            applies_to_same_delivery_system=True,
        )
        assert ev.fold_change == 2.0

    def test_default_output_carries_no_number(self, engine):
        sku = engine.modernize(_spec("HB-TURM", "Curcuma longa"))
        bio = sku.ingredients[0].delivery.bioavailability
        assert bio.fold_change is None
        assert bio.fold_change_range is None
        assert bio.qualitative_expectation

    def test_empty_evidence_object_is_rejected(self):
        with pytest.raises(ValidationError):
            BioavailabilityEvidence()

    def test_measured_solubility_requires_citation(self):
        with pytest.raises(ValidationError):
            PhysicochemicalProfile(
                pubchem_cid=1, molecular_weight=300.0, xlogp=2.0, tpsa=60.0,
                hbd=1, hba=3, rotatable_bonds=2, source="test",
                measured_solubility_mg_per_ml=0.01,
            )


# ---------------------------------------------------------------------------
# Invariants that must hold for every registry ingredient
# ---------------------------------------------------------------------------

_ALL_IDS = [
    "HB-ASHW", "HB-TURM", "HB-BERB", "HB-BOSW", "HB-PIPL",
    "HB-HARI", "HB-BIBH", "HB-AMLA", "HB-CINN", "HB-ELAA",
]


@pytest.mark.parametrize("ingredient_id", _ALL_IDS)
def test_every_registry_ingredient_modernizes(engine, ingredient_id):
    client = legacy_lookup()
    rec = client.lookup_ingredient(ingredient_id)
    sku = engine.modernize(_spec(ingredient_id, rec.botanical_name))
    ing = sku.ingredients[0]
    assert ing.delivery.active_unchanged is True
    assert ing.delivery.advisory_only is True
    assert ing.bcs.is_regulatory_determination is False
    assert ing.bcs.rationale, "every classification must state its reasoning"
    assert ing.delivery.excipients or ing.delivery.primary is DT.CONVENTIONAL
    assert ing.marker.properties.pubchem_cid > 0


def test_class_i_marker_gets_no_enhancement(engine):
    """A well-absorbed marker must not be sold an unnecessary delivery system."""
    sku = engine.modernize(_spec("HB-BIBH", "Terminalia bellirica"))  # gallic acid
    ing = sku.ingredients[0]
    if ing.bcs.bcs_class is BCSClass.I:
        assert ing.delivery.primary is DT.CONVENTIONAL
        assert ing.delivery.bioavailability.fold_change is None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_extract_ratio(self):
        assert normalize_extract_ratio(500, "10:1") == pytest.approx(5000.0)
        assert normalize_extract_ratio(250, "4:1") == pytest.approx(1000.0)
        assert normalize_extract_ratio(500, None) is None

    def test_marker_dose(self):
        assert marker_dose_mg(500, 95) == pytest.approx(475.0)
        assert marker_dose_mg(500, None) is None

    def test_percentages_sum_to_100(self):
        pct = reconcile_percentages({"a": 16, "b": 8, "c": 4, "d": 2, "e": 1})
        assert sum(pct.values()) == pytest.approx(100.0)
        assert pct["a"] == pytest.approx(16 / 31 * 100)

    def test_zero_total_rejected(self):
        with pytest.raises(ValueError):
            reconcile_percentages({"a": 0, "b": 0})


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def test_stock_ids_are_fixture_only():
    """Identity is resolved by NCBI during research, not by a synonym index."""
    lookup = legacy_lookup()
    assert lookup.lookup_ingredient("HB-ASHW").botanical_name == "Withania somnifera"
    with pytest.raises(ResearchError):
        lookup.lookup_ingredient("definitely not a plant")


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

def test_sku_round_trips_through_json(engine):
    sku = engine.modernize(_spec("HB-BOSW", "Boswellia serrata"))
    restored = ModernizedSKU.model_validate_json(sku.model_dump_json())
    assert restored.sku_id == sku.sku_id
    assert restored.ingredients[0].bcs.bcs_class == sku.ingredients[0].bcs.bcs_class


def test_formulation_spec_json_schema_is_exportable():
    schema = FormulationSpec.model_json_schema()
    assert schema["additionalProperties"] is False
    assert "confidence" in schema["properties"]


# ---------------------------------------------------------------------------
# Override and edge-case branches
# ---------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402

from herbenzo.components.modernizer.delivery_recommender import recommend  # noqa: E402
from herbenzo.services.records import MarkerRecord  # noqa: E402


def _props(**kw) -> PhysicochemicalProfile:
    base = dict(
        pubchem_cid=1, molecular_weight=300.0, xlogp=1.0, tpsa=60.0,
        hbd=1, hba=3, rotatable_bonds=2, source="test",
    )
    base.update(kw)
    return PhysicochemicalProfile(**base)


class TestClassifierBranches:
    def test_measured_solubility_overrides_descriptor_proxy(self):
        """A cited measured value must beat the logP proxy — the quercetin problem."""
        descriptor_call = classify(_props(xlogp=1.5, tpsa=127.0, hbd=5))
        assert descriptor_call.solubility_call == "high"

        measured = classify(
            _props(
                xlogp=1.5, tpsa=127.0, hbd=5,
                measured_solubility_mg_per_ml=0.001,
                solubility_source_pmid="00000000",
            )
        )
        assert measured.solubility_call == "low"
        assert measured.evidence_basis == "measured_solubility"
        assert measured.confidence > descriptor_call.confidence
        assert measured.bcs_class is BCSClass.II

    def test_measured_high_solubility_is_reported_as_such(self):
        a = classify(
            _props(measured_solubility_mg_per_ml=50.0, solubility_source_pmid="00000000")
        )
        assert a.solubility_call == "high"
        assert a.bcs_class is BCSClass.I

    def test_missing_xlogp_caps_confidence(self):
        a = classify(_props(xlogp=None))
        assert a.confidence <= 0.45
        assert any("XLogP unavailable" in r for r in a.rationale)

    def test_efflux_override_does_not_fire_when_permeability_already_low(self):
        marker = MarkerRecord(
            "X", "test", efflux_substrate=True, efflux_transporter="P-glycoprotein"
        )
        a = classify(_props(tpsa=200.0), marker)  # already low permeability
        assert a.permeability_call == "low"
        assert a.evidence_basis == "computed_descriptors"


class TestDeliveryBranches:
    def test_acid_labile_marker_triggers_enteric(self):
        marker = MarkerRecord("X", "test", acid_labile=True)
        rec = recommend(classify(_props(xlogp=4.0), marker), marker)
        assert DT.ENTERIC in rec.alternatives
        assert any("acid-labile" in r for r in rec.rationale)

    def test_cited_evidence_from_other_delivery_system_is_flagged(self):
        ev = BioavailabilityEvidence(
            fold_change=1.8, pmid="00000000",
            evidence_tier=EvidenceTier.ANIMAL, model_system="Wistar rat",
            applies_to_same_delivery_system=False,
        )
        rec = recommend(classify(_props(xlogp=4.0)), None, ev)
        assert rec.bioavailability.fold_change == 1.8
        assert any("different delivery system" in r for r in rec.rationale)

    def test_class_i_states_no_enhancement_claimed(self):
        rec = recommend(classify(_props()))
        assert rec.primary is DT.CONVENTIONAL
        assert "none is claimed" in rec.bioavailability.qualitative_expectation

    def test_efflux_marker_adds_interaction_warning(self):
        marker = MarkerRecord(
            "X", "test", efflux_substrate=True, efflux_transporter="P-glycoprotein"
        )
        rec = recommend(classify(_props(xlogp=4.0), marker), marker)
        assert any("drug-interaction risk" in r for r in rec.rationale)


def test_engine_accepts_validated_spec_instance(engine):
    spec = FormulationSpec.model_validate(_spec("HB-PIPL", "Piper longum"))
    sku = engine.modernize(spec)
    assert sku.source_formulation_id == spec.formulation_id


def test_registry_marker_records_are_immutable():
    """Curated flags must not be mutable at runtime by a caller."""
    marker = legacy_lookup().lookup_marker("HB-BERB")
    with pytest.raises(Exception):
        marker.efflux_substrate = False  # type: ignore[misc]
    assert replace(marker, efflux_substrate=False).efflux_substrate is False

"""Classical Ayurvedic preparations with no registry active marker.

The indicator is advisory: the run finishes, marker-backed ingredients are
still modernized and adjudicated, and confidence is not lowered because of
the flag. The stock registry assigns a marker to every row, so these tests
inject a fixture registries client with an empty marker list. That fixture
is not a monograph.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.api import app
from herbenzo.cli import main
from herbenzo.components.modernizer.modernizer import ModernizerEngine
from herbenzo.services.classical_marker_gap import matched_classical_forms
from herbenzo.services.evidence import EvidenceStore
from herbenzo.services.registries import (
    IngredientRecord,
    MarkerRecord,
    StaticRegistriesClient,
    UnknownIngredient,
    UnknownMarker,
)

ROOT = Path(__file__).resolve().parents[1]


class MarkerGapRegistries(StaticRegistriesClient):
    """Stock registry plus two fixtures that are not botanical monographs."""

    def lookup_ingredient(self, ingredient_id: str) -> IngredientRecord:
        if ingredient_id == "HB-NOMARK":
            return IngredientRecord(
                "HB-NOMARK",
                "Fixture unmarked ingredient",
                "Fixture",
                None,
                (),
                "prepared",
                markers=(),
            )
        if ingredient_id == "HB-NODESC":
            return IngredientRecord(
                "HB-NODESC",
                "Fixture named marker",
                "Fixture",
                None,
                (),
                "prepared",
                markers=(MarkerRecord("Not A Real Compound", "fixture; no descriptor cache"),),
            )
        return super().lookup_ingredient(ingredient_id)


class _MemEvidence(EvidenceStore):
    def __init__(self) -> None:
        self._manifest = {"searches": {}, "records": {}}
        self.manifest_path = Path("/tmp/herbenzo-evidence-unused.json")
        self.client = None


def _spec(**overrides) -> dict:
    base = {
        "formulation_id": "F-CLASS-1",
        "product_name": "Example Kwath",
        "dosage_form": "decoction",
        "target_market": "IN",
        "servings_per_day": 1,
        "confidence": 0.72,
        "ingredients": [
            {
                "ingredient_id": "HB-NOMARK",
                "botanical_name": "Fixture unmarked ingredient",
                "quantity_mg": 1000.0,
            }
        ],
    }
    base.update(overrides)
    return base


def _turmeric() -> dict:
    return {
        "ingredient_id": "HB-TURM",
        "botanical_name": "Curcuma longa",
        "quantity_mg": 500.0,
    }


def _pipeline():
    from herbenzo.pipeline import Pipeline

    return Pipeline(
        allow_network=False,
        evidence=_MemEvidence(),
        registries=MarkerGapRegistries(),
    )


# ---------------------------------------------------------------------------
# Form recognition — names only, not chemistry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "dosage_form,product_name,expected",
    [
        ("decoction", "Example", "decoction"),
        ("powder", "Example Kwath", "kwath"),
        ("lehya", "Example", "lehya"),
        ("capsule", "Example Bhasma", "bhasma"),
        ("powder", "Example Churna", "churna"),
        ("avaleha", "Example", "avaleha"),
    ],
)
def test_classical_form_tokens(dosage_form, product_name, expected):
    assert expected in matched_classical_forms(dosage_form, product_name)


def test_modern_capsule_is_not_a_classical_form():
    assert matched_classical_forms("capsule", "Ashwagandha Root Extract 500 mg") == ()


def test_plural_form_name_matches_the_stem():
    assert "decoction" in matched_classical_forms("decoctions", "Example")


# ---------------------------------------------------------------------------
# (a) classical + missing marker → flag, run succeeds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "dosage_form,product_name",
    [
        ("decoction", "Example decoction"),
        ("lehya", "Example lehya"),
        ("bhasma", "Example bhasma"),
        ("powder", "Example churna"),
    ],
)
def test_classical_missing_marker_flags_and_modernize_returns(dosage_form, product_name):
    engine = ModernizerEngine(MarkerGapRegistries())
    sku = engine.modernize(_spec(dosage_form=dosage_form, product_name=product_name))
    gap = engine.classical_active_marker_gap
    assert sku is None
    assert gap is not None
    assert gap["present"] is True
    assert gap["blocking"] is False
    assert gap["advisory_only"] is True
    assert gap["affects_confidence_floor"] is False
    assert gap["code"] == "classical_active_marker_gap"
    assert gap["ingredients"][0]["ingredient_id"] == "HB-NOMARK"
    assert "registry" in gap["ingredients"][0]["reason"]


def test_pipeline_classical_missing_marker_succeeds_without_inventing_sku(tmp_path):
    report = _pipeline().run(_spec())
    gap = report["classical_active_marker_gap"]
    assert gap["present"] is True
    assert gap["blocking"] is False
    assert report["sku"] is None
    assert report["claims"] == []
    assert report["confidence"]["after_modernization"] == report["confidence"]["inherited_from_A"]
    assert report["confidence"]["after_adjudication"] == report["confidence"]["inherited_from_A"]
    assert report["manifest"]["offline"] is True


def test_api_classical_missing_marker_is_200_not_422():
    client = TestClient(app)
    engine = ModernizerEngine(MarkerGapRegistries())
    app_engine = "herbenzo.api._ENGINE"
    import herbenzo.api as api_mod

    previous = api_mod._ENGINE
    api_mod._ENGINE = engine
    try:
        response = client.post("/modernize", json=_spec())
    finally:
        api_mod._ENGINE = previous
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sku"] is None
    assert body["classical_active_marker_gap"]["blocking"] is False
    assert body["classical_active_marker_gap"]["advisory_only"] is True


# ---------------------------------------------------------------------------
# (b) modern / registry-backed ingredients with markers → no flag
# ---------------------------------------------------------------------------

def test_modern_registry_ingredient_has_no_flag():
    engine = ModernizerEngine(MarkerGapRegistries())
    sku = engine.modernize(
        _spec(
            product_name="Turmeric Extract",
            dosage_form="capsule",
            ingredients=[_turmeric()],
        )
    )
    assert sku is not None
    assert engine.classical_active_marker_gap is None
    assert sku.ingredients[0].marker.marker_name == "Curcumin"
    assert "classical_active_marker_gap" not in sku.model_dump(mode="json")


def test_classical_name_with_registry_markers_has_no_flag():
    """Triphala Churna is a classical form, but each fruit has a registry marker."""
    raw = json.loads((ROOT / "examples" / "triphala.json").read_text())
    engine = ModernizerEngine()
    sku = engine.modernize(raw)
    assert sku is not None
    assert engine.classical_active_marker_gap is None
    assert len(sku.ingredients) == 3
    assert "churna" in matched_classical_forms(raw["dosage_form"], raw["product_name"])


def test_pipeline_and_cli_leave_marker_backed_classical_unflagged(tmp_path, monkeypatch):
    from herbenzo.pipeline import Pipeline

    raw = json.loads((ROOT / "examples" / "triphala.json").read_text())
    report = Pipeline(
        allow_network=False,
        evidence=_MemEvidence(),
        registries=StaticRegistriesClient(),
    ).run(raw)
    assert report["classical_active_marker_gap"] is None
    assert report["sku"]["product_name"] == "Triphala Churna"
    assert len(report["sku"]["ingredients"]) == 3
    assert "classical_active_marker_gap" not in report["sku"]

    monkeypatch.chdir(tmp_path)
    out = tmp_path / "report.json"
    assert main(["run", str(ROOT / "examples" / "triphala.json"), "-o", str(out), "--offline"]) == 0
    cli_report = json.loads(out.read_text())
    assert cli_report["classical_active_marker_gap"] is None
    assert cli_report["sku"]["sku_id"] == "SKU-F-TRIP-001"


def test_api_ashwagandha_omits_the_indicator():
    client = TestClient(app)
    raw = json.loads((ROOT / "examples" / "ashwagandha.json").read_text())
    response = client.post("/modernize", json=raw)
    assert response.status_code == 200, response.text
    assert "classical_active_marker_gap" not in response.json()


# ---------------------------------------------------------------------------
# (c) the flag never blocks modernization
# ---------------------------------------------------------------------------

def test_flag_does_not_block_or_penalize_remaining_ingredients():
    """A decoction that mixes a marker-backed herb with a marker gap still runs.

    Confidence matches the same decoction without the gap ingredient, so the
    indicator itself is not a confidence-floor penalty. Adjudication still
    emits claims for the marker-backed ingredient.
    """
    pipe = _pipeline()
    control = pipe.run(
        _spec(formulation_id="F-CONTROL", ingredients=[_turmeric()])
    )
    mixed = pipe.run(
        _spec(
            formulation_id="F-MIXED",
            ingredients=[_turmeric(), _spec()["ingredients"][0]],
        )
    )

    assert control["classical_active_marker_gap"] is None
    assert mixed["classical_active_marker_gap"]["blocking"] is False
    assert mixed["classical_active_marker_gap"]["affects_confidence_floor"] is False
    assert [i["ingredient_id"] for i in mixed["classical_active_marker_gap"]["ingredients"]] == [
        "HB-NOMARK"
    ]
    assert [i["ingredient_id"] for i in mixed["sku"]["ingredients"]] == ["HB-TURM"]
    assert mixed["sku"]["ingredients"][0]["marker"]["marker_name"] == "Curcumin"
    assert "classical_active_marker_gap" not in mixed["sku"]

    assert mixed["confidence"]["after_modernization"] == control["confidence"]["after_modernization"]
    assert mixed["confidence"]["after_adjudication"] == control["confidence"]["after_adjudication"]
    assert mixed["confidence"]["after_adjudication"] <= mixed["confidence"]["inherited_from_A"]
    assert mixed["claims"]
    assert {c["verdict"] for c in mixed["claims"]} <= {"computed", "unsupported"}
    assert len(mixed["claims"]) == len(control["claims"])


def test_api_mixed_classical_gap_returns_sku_and_indicator():
    import herbenzo.api as api_mod

    client = TestClient(app)
    previous = api_mod._ENGINE
    api_mod._ENGINE = ModernizerEngine(MarkerGapRegistries())
    raw = _spec(ingredients=[_turmeric(), _spec()["ingredients"][0]])
    raw["source_spec_id"] = "spec-gap-1"
    raw["provenance_thread"] = {
        "schema_version": "1.0.0",
        "spec_id": "spec-gap-1",
        "formulation_id": raw["formulation_id"],
        "stages": ["A"],
    }
    try:
        response = client.post("/modernize", json=raw)
    finally:
        api_mod._ENGINE = previous

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sku_id"] == "SKU-F-CLASS-1"
    assert [i["ingredient_id"] for i in body["ingredients"]] == ["HB-TURM"]
    assert body["classical_active_marker_gap"]["blocking"] is False
    assert body["confidence"] <= raw["confidence"]
    assert body["inherited_confidence"] == raw["confidence"]
    thread = body["provenance_thread"]
    assert thread["spec_id"] == "spec-gap-1"
    assert thread["sku_id"] == body["sku_id"]
    assert "B" in thread["stages"]


def test_non_classical_missing_marker_still_raises():
    engine = ModernizerEngine(MarkerGapRegistries())
    with pytest.raises(UnknownMarker):
        engine.modernize(
            _spec(
                product_name="Unmarked capsule",
                dosage_form="capsule",
            )
        )
    assert engine.classical_active_marker_gap is None


def test_pipeline_non_classical_missing_marker_still_raises():
    with pytest.raises(UnknownMarker):
        _pipeline().run(_spec(product_name="Unmarked capsule", dosage_form="capsule"))


def test_api_non_classical_missing_marker_is_422():
    import herbenzo.api as api_mod

    client = TestClient(app)
    previous = api_mod._ENGINE
    api_mod._ENGINE = ModernizerEngine(MarkerGapRegistries())
    try:
        response = client.post(
            "/modernize",
            json=_spec(product_name="Unmarked capsule", dosage_form="capsule"),
        )
    finally:
        api_mod._ENGINE = previous
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unknown_ingredient"


def test_unknown_identity_still_raises_for_a_classical_form():
    engine = ModernizerEngine(MarkerGapRegistries())
    spec = _spec()
    spec["ingredients"] = [
        {
            "ingredient_id": "HB-NOPE",
            "botanical_name": "Nonexistentia fictiva",
            "quantity_mg": 100.0,
        }
    ]
    with pytest.raises(UnknownIngredient):
        engine.modernize(spec)


def test_named_marker_without_descriptors_still_raises_for_classical_forms():
    """A missing physicochemical cache is not the active-marker indicator."""
    engine = ModernizerEngine(MarkerGapRegistries())
    spec = _spec()
    spec["ingredients"] = [
        {
            "ingredient_id": "HB-NODESC",
            "botanical_name": "Fixture named marker",
            "quantity_mg": 100.0,
        }
    ]
    with pytest.raises(UnknownMarker):
        engine.modernize(spec)

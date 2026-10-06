"""Advisory finished-format suggestions. They never change ModernizedSKU."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.cli import main
from herbenzo.format_suggestions import (
    GUMMY_FORMAT_ID,
    NANOEMULSION_FORMAT_ID,
    OWNER_NANOEMULSION_CAUTION,
    SOFT_CHEW_FORMAT_ID,
    IngredientProfile,
    is_classical_or_chyawanprash,
    load_catalog,
    profile_from_mapping,
    rank_formats,
    suggest_formats,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "format_suggestions"
EXAMPLE = ROOT / "examples" / "ashwagandha.json"

PROTECTED_HEAT = frozenset({"none", "low"})
PROTECTED_TASTE = frozenset({"strong", "moderate"})


@pytest.fixture
def client() -> TestClient:
    from herbenzo.api import app

    return TestClient(app)


def _bitter_profiles() -> list[IngredientProfile]:
    raw = json.loads((FIXTURES / "bitter_heat.json").read_text(encoding="utf-8"))
    return [profile_from_mapping(row) for row in raw]


def _chyawanprash() -> dict:
    return json.loads((FIXTURES / "chyawanprash.json").read_text(encoding="utf-8"))


def _protected_ids() -> set[str]:
    return {
        fmt.format_id
        for fmt in load_catalog()
        if fmt.taste_masking in PROTECTED_TASTE and fmt.process_heat in PROTECTED_HEAT
    }


def test_catalog_loads_and_validates():
    formats = load_catalog()
    ids = [fmt.format_id for fmt in formats]
    assert ids == list(dict.fromkeys(ids))
    assert NANOEMULSION_FORMAT_ID in ids
    assert GUMMY_FORMAT_ID in ids
    assert SOFT_CHEW_FORMAT_ID in ids
    by_id = {fmt.format_id: fmt for fmt in formats}
    for fmt in formats:
        assert fmt.category in {"textbook", "market"}
        assert fmt.textbook_rationale
        assert fmt.constraints
        assert fmt.excipients
        for ref in fmt.references:
            assert ref.brand
            assert ref.category
            assert ref.url.startswith("https://")
    gummy_urls = {ref.url for ref in by_id[GUMMY_FORMAT_ID].references}
    assert "https://gruns.co/" in gummy_urls
    assert by_id[NANOEMULSION_FORMAT_ID].references == []
    assert by_id["frozen_dessert"].references == []
    assert all("owner guidance" not in line for line in by_id[NANOEMULSION_FORMAT_ID].constraints)


def test_ranking_is_deterministic_for_fixture_ingredients():
    profiles = _bitter_profiles()
    first = rank_formats(profiles)
    second = rank_formats(profiles)
    assert first == second
    scores = [row["score"] for row in first]
    assert scores == sorted(scores, reverse=True)
    ids = [row["format_id"] for row in first]
    assert ids == sorted(ids, key=lambda format_id: (-dict(zip(ids, scores))[format_id], format_id))


def test_bitter_heat_fixture_favours_taste_masked_low_heat_formats():
    ranked = rank_formats(_bitter_profiles())
    protected = _protected_ids()
    assert ranked[0]["format_id"] in protected
    ids = [row["format_id"] for row in ranked]
    for taste_masked in ("capsule", "oral_film", "effervescent_tablet"):
        assert ids.index(taste_masked) < ids.index("gummy")
    assert ids.index("capsule") < ids.index("powder_sachet")
    top_reasons = " ".join(ranked[0]["fit_reasons"]).lower()
    assert "taste" in top_reasons
    assert "heat" in top_reasons


def test_chyawanprash_downranks_nanoemulsion_below_gummy_and_chew():
    body = suggest_formats(**_chyawanprash())
    assert body["advisory_only"] is True
    assert body["changes_modernized_sku"] is False
    assert body["classical_like"] is True
    assert body["ingredient_ids"] == ["HB-AMLA", "HB-PIPL", "HB-ASHW"]
    suggestions = body["suggestions"]
    ids = [row["format_id"] for row in suggestions]
    assert NANOEMULSION_FORMAT_ID in ids
    assert len(ids) == len(load_catalog())
    assert ids.index(GUMMY_FORMAT_ID) < ids.index(NANOEMULSION_FORMAT_ID)
    assert ids.index(SOFT_CHEW_FORMAT_ID) < ids.index(NANOEMULSION_FORMAT_ID)
    nano = next(row for row in suggestions if row["format_id"] == NANOEMULSION_FORMAT_ID)
    gummy = next(row for row in suggestions if row["format_id"] == GUMMY_FORMAT_ID)
    chew = next(row for row in suggestions if row["format_id"] == SOFT_CHEW_FORMAT_ID)
    assert nano["score"] < gummy["score"]
    assert nano["score"] < chew["score"]
    assert OWNER_NANOEMULSION_CAUTION in nano["cautions"]
    assert OWNER_NANOEMULSION_CAUTION not in gummy["cautions"]


def test_plain_capsule_does_not_apply_chyawanprash_caution():
    assert is_classical_or_chyawanprash("capsule", "Ashwagandha Root Extract") is False
    assert is_classical_or_chyawanprash("avaleha", None) is True
    body = suggest_formats(["HB-ASHW"], dosage_form="capsule", product_name="Ashwagandha Root Extract")
    assert body["classical_like"] is False
    nano = next(row for row in body["suggestions"] if row["format_id"] == NANOEMULSION_FORMAT_ID)
    assert OWNER_NANOEMULSION_CAUTION not in nano["cautions"]


def test_multi_ingredient_request_is_stable(client: TestClient):
    payload = {
        "ingredients": [
            {"ingredient_id": "HB-HARI", "quantity_mg": 1000},
            {"ingredient_id": "HB-BIBH", "quantity_mg": 1000},
            {"ingredient_id": "HB-AMLA", "quantity_mg": 1000},
        ],
        "dosage_form": "powder",
        "audience": "adults",
    }
    first = client.post("/suggest-formats", json=payload)
    second = client.post("/suggest-formats", json=payload)
    assert first.status_code == 200, first.text
    assert second.json() == first.json()
    body = first.json()
    assert body["ingredient_ids"] == ["HB-HARI", "HB-BIBH", "HB-AMLA"]
    assert body["quantities_mg"]["HB-HARI"] == 1000
    assert len(body["suggestions"]) == len(load_catalog())


def test_unknown_ingredient_is_422(client: TestClient):
    missing = client.post("/suggest-formats", json={"ingredient_ids": ["HB-NOPE"]})
    assert missing.status_code == 422
    assert missing.json()["detail"]["error"] == "unknown_ingredient"

    mixed = client.post(
        "/suggest-formats",
        json={"ingredient_ids": ["HB-ASHW", "HB-NOPE"]},
    )
    assert mixed.status_code == 422
    assert mixed.json()["detail"]["error"] == "unknown_ingredient"


def test_suggest_formats_rejects_bad_body(client: TestClient):
    empty = client.post("/suggest-formats", json={"ingredient_ids": []})
    assert empty.status_code == 422
    duplicate = client.post(
        "/suggest-formats",
        json={"ingredient_ids": ["HB-ASHW", "HB-ASHW"]},
    )
    assert duplicate.status_code == 422
    audience = client.post(
        "/suggest-formats",
        json={"ingredient_ids": ["HB-ASHW"], "audience": "infants"},
    )
    assert audience.status_code == 422


def test_modernize_response_is_unchanged_by_format_suggestions(client: TestClient):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    modernized = client.post("/modernize", json=raw)
    assert modernized.status_code == 200, modernized.text
    body = modernized.json()
    assert body["sku_id"] == "SKU-F-ASHW-001"
    assert "suggestions" not in body
    assert "format_suggestions" not in body
    assert body["dosage_form"] == raw["dosage_form"]


def test_health_and_ui_expose_format_suggestions(client: TestClient):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["endpoints"]["suggest_formats"] == "POST /suggest-formats"
    assert health.json()["endpoints"]["modernize"] == "POST /modernize"

    page = client.get("/")
    assert page.status_code == 200
    assert "Suggested modern formats" in page.text
    assert 'id="format-suggestions"' in page.text
    assert "/static/format_suggestions.js" in page.text
    assert 'id="spec-input"' in page.text
    assert "Advanced / Raw JSON" in page.text

    script = client.get("/static/format_suggestions.js")
    assert script.status_code == 200
    assert "/suggest-formats" in script.text
    style = client.get("/static/format_suggestions.css")
    assert style.status_code == 200


def test_cli_suggest_formats(capsys: pytest.CaptureFixture[str]):
    code = main(["suggest-formats", "HB-ASHW", "HB-TURM", "--audience", "adults", "--quantity", "HB-ASHW=300"])
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ingredient_ids"] == ["HB-ASHW", "HB-TURM"]
    assert body["quantities_mg"]["HB-ASHW"] == 300
    assert body["suggestions"]

    unknown = main(["suggest-formats", "HB-NOPE"])
    assert unknown == 2
    err = capsys.readouterr().err
    assert "unknown_ingredient" in err

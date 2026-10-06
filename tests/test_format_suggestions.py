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
)
from herbenzo.format_suggestions import suggest_formats as _suggest_formats
from tests.legacy_snapshot import approval_for, envelope, legacy_lookup


def suggest_formats(*args, **kwargs):
    kwargs.setdefault("registries", legacy_lookup())
    return _suggest_formats(*args, **kwargs)

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
    fssai = "https://www.fssai.gov.in/upload/advisories/2022/03/6243ef28079ceDirection_Nutra_30_03_2022.pdf"
    fda_nano = (
        "https://www.fda.gov/regulatory-information/search-fda-guidance-documents/"
        "drug-products-including-biological-products-contain-nanomaterials-guidance-industry"
    )
    ema_nano = (
        "https://www.ema.europa.eu/en/human-regulatory-overview/research-development/"
        "scientific-guidelines/multidisciplinary-guidelines/multidisciplinary-nanomedicines"
    )
    fda_iid = "https://www.fda.gov/drugs/drug-approvals-and-databases/inactive-ingredients-database-download"
    for fmt_id in ("gummy", "soft_chew", "agar_jelly", "oral_film", "odt_melt", "nutrition_bar"):
        notes = [ref.note or "" for ref in by_id[fmt_id].references if ref.url == fssai]
        assert notes, fmt_id
        assert "5(1)" in notes[0]
        assert "nutraceutical" in notes[0].lower()
        assert "FSMP" in notes[0]
    nano_urls = {ref.url for ref in by_id[NANOEMULSION_FORMAT_ID].references}
    assert fda_nano in nano_urls
    assert ema_nano in nano_urls
    assert fda_iid in nano_urls
    emulsion_notes = " ".join(
        ref.note or ""
        for ref in by_id["emulsion"].references
        if ref.url in {fda_nano, ema_nano}
    ).lower()
    assert "conventional emulsion" in emulsion_notes
    assert "not itself a nanomaterial" in emulsion_notes
    assert fda_iid in {ref.url for ref in by_id["capsule"].references}
    assert fda_iid in {ref.url for ref in by_id["suspension"].references}
    assert by_id["frozen_dessert"].references == []
    assert all("owner guidance" not in line for line in by_id[NANOEMULSION_FORMAT_ID].constraints)


def test_hmpc_and_evidence_links_are_static_and_offline(monkeypatch: pytest.MonkeyPatch):
    import socket
    import urllib.request

    def _blocked(*_args, **_kwargs):
        raise AssertionError("format ranking must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)

    body = suggest_formats(["HB-ASHW", "HB-TURM"], dosage_form="gummy")
    gummy = next(row for row in body["suggestions"] if row["format_id"] == GUMMY_FORMAT_ID)
    urls = {ref["url"] for ref in gummy["references"]}
    assert "https://www.ema.europa.eu/en/medicines/herbal/withaniae-somniferae-radix" in urls
    assert "https://www.ema.europa.eu/en/medicines/herbal/curcumae-longae-rhizoma" in urls
    assert "https://gruns.co/" in urls

    searches = {row["source"]: row for row in gummy["evidence_searches"]}
    europe = searches["Europe PMC"]
    assert europe["url"].startswith("https://europepmc.org/search?query=")
    assert "Ashwagandha" in europe["query"]
    assert "Withania somnifera" in europe["query"]
    assert "Turmeric" in europe["query"]
    assert "Curcuma longa" in europe["query"]
    assert "gummy" in europe["query"].lower()
    dsld = searches["NIH DSLD"]
    assert dsld["url"].startswith("https://api.ods.od.nih.gov/dsld/v9/search-filter?q=")
    assert "Ashwagandha" in dsld["query"]
    assert "gummy" in dsld["query"].lower()
    lnhpd = searches["Health Canada LNHPD"]
    assert lnhpd["url"].startswith("https://health-products.canada.ca/lnhpd-bdpsnh/")
    assert "search-recherche-type=advanced-avancee" in lnhpd["url"]
    assert "Ashwagandha" not in lnhpd["url"]
    assert "ingredient=" not in lnhpd["url"]

    amla = suggest_formats(["HB-AMLA"])
    amla_gummy = next(row for row in amla["suggestions"] if row["format_id"] == GUMMY_FORMAT_ID)
    assert all(
        "/medicines/herbal/" not in ref["url"] for ref in amla_gummy["references"]
    )
    assert amla_gummy["evidence_searches"][0]["query"].startswith(
        '("Indian gooseberry" OR "Phyllanthus emblica") AND '
    )


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
    ids = ["HB-HARI", "HB-BIBH", "HB-AMLA"]
    payload = {
        "ingredients": [
            {"ingredient_id": "HB-HARI", "quantity_mg": 1000},
            {"ingredient_id": "HB-BIBH", "quantity_mg": 1000},
            {"ingredient_id": "HB-AMLA", "quantity_mg": 1000},
        ],
        "approvals": [approval_for(item) for item in ids],
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
    assert missing.json()["detail"]["error"] == "not_approved"

    mixed = client.post(
        "/suggest-formats",
        json={
            "ingredient_ids": ["HB-ASHW", "HB-NOPE"],
            "approvals": [approval_for("HB-ASHW")],
        },
    )
    assert mixed.status_code == 422
    assert mixed.json()["detail"]["error"] == "not_approved"


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
    modernized = client.post("/modernize", json=envelope(raw))
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
    assert "evidence_searches" in script.text
    assert "Evidence searches" in script.text
    style = client.get("/static/format_suggestions.css")
    assert style.status_code == 200


def test_cli_suggest_formats(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    approvals = tmp_path / "approvals.json"
    approvals.write_text(json.dumps([approval_for("HB-ASHW"), approval_for("HB-TURM")]))
    code = main([
        "suggest-formats",
        str(approvals),
        "HB-ASHW",
        "HB-TURM",
        "--audience",
        "adults",
        "--quantity",
        "HB-ASHW=300",
    ])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    body = json.loads(captured.out)
    assert body["ingredient_ids"] == ["HB-ASHW", "HB-TURM"]
    assert body["quantities_mg"]["HB-ASHW"] == 300
    assert body["suggestions"]

    unknown = main(["suggest-formats", str(approvals), "HB-NOPE"])
    assert unknown == 2
    err = capsys.readouterr().err
    assert "not_approved" in err

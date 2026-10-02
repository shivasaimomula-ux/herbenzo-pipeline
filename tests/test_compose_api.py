"""Compose UI support: registry listing and on-disk FormulationSpec drafts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.api import _DEFAULT_DRAFTS_DIR, app
from herbenzo.services.registries import _INGREDIENTS

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ashwagandha.json"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def drafts_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HERBENZO_COMPOSE_DRAFTS_DIR", str(tmp_path))
    return tmp_path


def _example() -> dict:
    return json.loads(EXAMPLE.read_text())


def test_default_drafts_dir_is_under_service_data():
    assert _DEFAULT_DRAFTS_DIR.name == "compose_drafts"
    assert _DEFAULT_DRAFTS_DIR.parent.name == "data"
    assert _DEFAULT_DRAFTS_DIR.parent.parent.name == "herbenzo"


def test_ingredients_are_the_stock_registry(client: TestClient):
    r = client.get("/ingredients")
    assert r.status_code == 200
    rows = r.json()["ingredients"]
    assert [row["ingredient_id"] for row in rows] == list(_INGREDIENTS)
    for row, (ingredient_id, rec) in zip(rows, _INGREDIENTS.items(), strict=True):
        assert row["ingredient_id"] == ingredient_id
        assert row["botanical_name"] == rec.botanical_name
        assert row["common_name"] == rec.common_name
        assert row["sanskrit_name"] == rec.sanskrit_name
        assert row["synonyms"] == list(rec.synonyms)
        assert row["part_used"] == rec.part_used
        assert [m["marker_name"] for m in row["markers"]] == [
            m.marker_name for m in rec.markers
        ]
        assert all(m["rationale"] for m in row["markers"])


def test_health_lists_compose_endpoints(client: TestClient):
    endpoints = client.get("/health").json()["endpoints"]
    assert endpoints["ingredients"] == "GET /ingredients"
    assert endpoints["drafts"] == "GET/POST /drafts"
    assert endpoints["draft"] == "GET/DELETE /drafts/{id}"
    assert endpoints["modernize"] == "POST /modernize"


def test_compose_shaped_spec_still_modernizes(client: TestClient):
    """A form-built FormulationSpec uses the existing modernize path."""
    payload = {
        "formulation_id": "F-COMPOSE-1",
        "product_name": "Turmeric gummy",
        "dosage_form": "gummy",
        "target_market": "IN",
        "servings_per_day": 2,
        "confidence": 0.85,
        "ingredients": [
            {
                "ingredient_id": "HB-TURM",
                "botanical_name": "Curcuma longa",
                "common_name": "Turmeric",
                "part_used": "rhizome",
                "quantity_mg": 250,
                "extract_ratio": "10:1",
                "standardized_marker": "Curcumin",
                "standardized_percent": 95,
            }
        ],
    }
    r = client.post("/modernize", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sku_id"] == "SKU-F-COMPOSE-1"
    assert body["dosage_form"] == "gummy"
    assert body["target_market"] == "IN"
    assert body["servings_per_day"] == 2
    assert body["ingredients"][0]["ingredient_id"] == "HB-TURM"
    assert body["ingredients"][0]["quantity_mg"] == 250


def test_ui_offers_compose_and_raw_json(client: TestClient):
    page = client.get("/")
    assert page.status_code == 200
    assert "Compose" in page.text
    assert "Advanced / Raw JSON" in page.text
    assert "Run Modernize" in page.text
    assert 'id="spec-input"' in page.text
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "/ingredients" in js.text
    assert "/drafts" in js.text
    assert "classical_active_marker_gap" in js.text
    assert "POST /modernize" in js.text or "/modernize" in js.text


def test_draft_crud_and_reload_modernizes(client: TestClient, drafts_dir: Path):
    empty = client.get("/drafts")
    assert empty.status_code == 200
    assert empty.json()["drafts"] == []

    partial = client.post(
        "/drafts",
        json={"name": "  half-finished  ", "spec": {"product_name": "Untitled gummy"}},
    )
    assert partial.status_code == 201, partial.text
    partial_body = partial.json()
    assert partial_body["name"] == "half-finished"
    assert partial_body["complete"] is False
    assert partial_body["spec"]["product_name"] == "Untitled gummy"
    assert (drafts_dir / f"{partial_body['id']}.json").is_file()

    created = client.post("/drafts", json={"name": "Ashwagandha capsule", "spec": _example()})
    assert created.status_code == 201, created.text
    saved = created.json()
    assert saved["complete"] is True
    draft_id = saved["id"]
    saved_at = saved["saved_at"]

    listed = client.get("/drafts")
    assert listed.status_code == 200
    summaries = listed.json()["drafts"]
    assert {row["id"] for row in summaries} == {partial_body["id"], draft_id}
    assert all("spec" not in row for row in summaries)
    by_id = {row["id"]: row for row in summaries}
    assert by_id[draft_id]["complete"] is True
    assert by_id[draft_id]["ingredient_count"] == 1
    assert by_id[partial_body["id"]]["complete"] is False

    loaded = client.get(f"/drafts/{draft_id}")
    assert loaded.status_code == 200
    assert loaded.json()["spec"]["formulation_id"] == "F-ASHW-001"
    modernized = client.post("/modernize", json=loaded.json()["spec"])
    assert modernized.status_code == 200, modernized.text
    assert modernized.json()["sku_id"] == "SKU-F-ASHW-001"

    updated_spec = _example()
    updated_spec["product_name"] = "Ashwagandha gummy"
    updated_spec["dosage_form"] = "gummy"
    updated_spec["servings_per_day"] = 2
    updated = client.post(
        "/drafts",
        json={"id": draft_id, "name": "Ashwagandha gummy", "spec": updated_spec},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == draft_id
    assert updated.json()["saved_at"] == saved_at
    assert updated.json()["name"] == "Ashwagandha gummy"
    assert updated.json()["spec"]["dosage_form"] == "gummy"
    assert updated.json()["complete"] is True
    again = client.post("/modernize", json=updated.json()["spec"])
    assert again.status_code == 200, again.text
    assert again.json()["dosage_form"] == "gummy"
    assert again.json()["product_name"] == "Ashwagandha gummy"

    unknown = _example()
    unknown["ingredients"][0]["ingredient_id"] = "HB-NOPE"
    unknown_draft = client.post("/drafts", json={"name": "unknown id", "spec": unknown})
    assert unknown_draft.status_code == 201
    assert unknown_draft.json()["complete"] is False
    rejected = client.post("/modernize", json=unknown)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["error"] == "unknown_ingredient"

    deleted = client.delete(f"/drafts/{draft_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/drafts/{draft_id}").status_code == 404
    assert client.delete(f"/drafts/{draft_id}").status_code == 404
    remaining = {row["id"] for row in client.get("/drafts").json()["drafts"]}
    assert draft_id not in remaining
    assert partial_body["id"] in remaining


def test_draft_validation(client: TestClient):
    missing_name = client.post("/drafts", json={"spec": {"product_name": "x"}})
    assert missing_name.status_code == 422

    missing_spec = client.post("/drafts", json={"name": "nope"})
    assert missing_spec.status_code == 422

    bad_spec = client.post("/drafts", json={"name": "nope", "spec": ["not", "an", "object"]})
    assert bad_spec.status_code == 422

    bad_id = client.get("/drafts/not-an-id")
    assert bad_id.status_code == 422

    missing = client.post(
        "/drafts",
        json={"id": "d" + "ab" * 8, "name": "ghost", "spec": {"product_name": "x"}},
    )
    assert missing.status_code == 404

    traversal = client.get("/drafts/..%2F..%2Fetc%2Fpasswd")
    assert traversal.status_code in {404, 422}

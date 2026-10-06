"""Polyherbal FormulationSpec: the contract already carries a list of ingredients."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.api import app
from tests.legacy_snapshot import approval_for, envelope

ROOT = Path(__file__).resolve().parents[1]
TRIPHALA = ROOT / "examples" / "triphala.json"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def drafts_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HERBENZO_COMPOSE_DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("HERBENZO_REGISTRY_DIR", str(tmp_path / "registry"))
    return tmp_path


def _triphala() -> dict:
    return json.loads(TRIPHALA.read_text())


def test_multi_ingredient_spec_modernizes(client: TestClient):
    spec = _triphala()
    response = client.post("/modernize", json=envelope(spec))
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["ingredient_id"] for row in body["ingredients"]] == ["HB-HARI", "HB-BIBH", "HB-AMLA"]
    assert body["product_name"] == "Triphala Churna"
    assert len(body["ingredients"]) == 3


def test_mixed_known_and_unknown_ids_report_the_unknown_id(client: TestClient):
    spec = _triphala()
    spec["ingredients"][1]["ingredient_id"] = "HB-NOPE"
    spec["ingredients"].append(
        {
            "ingredient_id": "HB-ALSO",
            "botanical_name": "Nowhere officinalis",
            "quantity_mg": 50,
        }
    )
    response = client.post(
        "/modernize",
        json={
            "spec": spec,
            "approvals": [approval_for("HB-HARI"), approval_for("HB-AMLA")],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "not_approved"
    assert detail["ingredient_ids"] == ["HB-NOPE", "HB-ALSO"]
    assert "HB-HARI" not in detail["ingredient_ids"]
    assert "HB-NOPE" in detail["message"]
    assert "HB-ALSO" in detail["message"]
    assert '\\"' not in detail["message"]


def test_draft_round_trips_multiple_ingredients(client: TestClient):
    spec = _triphala()
    saved = client.post(
        "/drafts",
        json={"name": "Triphala", "spec": spec, "approvals": envelope(spec)["approvals"]},
    )
    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["complete"] is True
    assert body["ingredient_count"] == 3
    loaded = client.get(f"/drafts/{body['id']}")
    assert loaded.status_code == 200
    ids = [row["ingredient_id"] for row in loaded.json()["spec"]["ingredients"]]
    assert ids == ["HB-HARI", "HB-BIBH", "HB-AMLA"]
    assert loaded.json()["spec"]["ingredients"][0]["quantity_mg"] == 1000.0
    again = client.post(
        "/modernize",
        json={"spec": loaded.json()["spec"], "approvals": loaded.json()["approvals"]},
    )
    assert again.status_code == 200, again.text
    assert len(again.json()["ingredients"]) == 3


def test_compose_picker_is_multi_select_and_separate_from_review(client: TestClient):
    page = client.get("/")
    assert page.status_code == 200
    text = page.text
    assert 'id="ingredient-search"' in text
    assert "Load Triphala" in text
    assert "Review candidates" in text
    assert "Live species search" in text or "NCBI Taxonomy" in text
    assert 'id="ingredient-search"' in text
    script = client.get("/static/app.js")
    assert script.status_code == 200
    source = script.text
    assert "/research/suggest" in source
    assert 'fetch("/ingredients"' not in source
    assert "TRIPHALA_SPEC" in source
    assert source.count("ingredient_id:") >= 3

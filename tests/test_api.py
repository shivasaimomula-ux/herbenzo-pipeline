"""HTTP API tests for Stage B FastAPI service (Tasks T9–T10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.api import app
from herbenzo.components.modernizer.modernizer import ENGINE_VERSION

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ashwagandha.json"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_ok(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["stage"] == "B"
    assert body["port_contract"] == 8003
    assert body["llm"] is False
    assert body["engine_version"] == ENGINE_VERSION
    assert body["contracts"] == "herbenzo-contracts"
    assert body["ui"] == "available"
    assert body["endpoints"]["ui"] == "/"


def test_root_serves_ui(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    text = r.text
    assert "Herbenzo" in text
    assert "Stage B" in text
    assert "FormulationSpec" in text
    assert "/static/app.js" in text


def test_static_assets_served(client: TestClient):
    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert "text/css" in css.headers.get("content-type", "")
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "modernize" in js.text


def test_modernize_ashwagandha(client: TestClient):
    raw = json.loads(EXAMPLE.read_text())
    raw["source_spec_id"] = "spec-demo-1"
    raw["provenance_thread"] = {
        "schema_version": "1.0.0",
        "spec_id": "spec-demo-1",
        "formulation_id": "F-ASHW-001",
        "stages": ["F", "A"],
    }
    r = client.post("/modernize", json=raw)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sku_id"] == "SKU-F-ASHW-001"
    assert body["source_formulation_id"] == "F-ASHW-001"
    assert body["engine_version"] == ENGINE_VERSION
    assert len(body["ingredients"]) == 1
    assert body["ingredients"][0]["ingredient_id"] == "HB-ASHW"
    assert "bcs" in body["ingredients"][0]
    assert "delivery" in body["ingredients"][0]
    # Confidence may only fall.
    assert body["confidence"] <= raw["confidence"]
    assert body["inherited_confidence"] == raw["confidence"]
    pt = body.get("provenance_thread") or {}
    assert pt.get("spec_id") == "spec-demo-1"
    assert pt.get("formulation_id") == "F-ASHW-001"
    assert pt.get("sku_id") == "SKU-F-ASHW-001"
    assert "B" in (pt.get("stages") or [])


def test_modernize_rejects_unknown_field(client: TestClient):
    raw = json.loads(EXAMPLE.read_text())
    raw["not_a_field"] = True
    r = client.post("/modernize", json=raw)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "contract_validation"


def test_modernize_rejects_raised_confidence_floor(client: TestClient):
    raw = json.loads(EXAMPLE.read_text())
    raw["inherited_confidence"] = 0.5
    raw["confidence"] = 0.9
    r = client.post("/modernize", json=raw)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "contract_validation"


def test_modernize_rejects_unknown_ingredient(client: TestClient):
    raw = json.loads(EXAMPLE.read_text())
    raw["ingredients"][0]["ingredient_id"] = "HB-NOPE"
    r = client.post("/modernize", json=raw)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_ingredient"


def test_modernize_rejects_non_object(client: TestClient):
    r = client.post("/modernize", json=["not", "an", "object"])
    assert r.status_code == 422

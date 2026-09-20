"""HTTP API tests for Stage B FastAPI service (Task T9)."""

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
    assert body["ui"] == "deferred-to-T10"


def test_root_health_json(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["port_contract"] == 8003


def test_modernize_ashwagandha(client: TestClient):
    raw = json.loads(EXAMPLE.read_text())
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

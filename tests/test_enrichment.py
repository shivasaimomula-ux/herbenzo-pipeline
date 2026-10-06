"""Client tests for NCBI, PubChem, chemical taxonomy, and the optional LLM.

Persisted candidate and registry tests were retired with the ingredient registry.
Live research coverage is in tests/test_research_flow.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from herbenzo.clients.chemclass import ChemicalTaxonomyClient
from herbenzo.clients.eutils import EutilsClient
from herbenzo.clients.httpjson import CachedJsonClient, HttpError
from herbenzo.clients.llm import LlmClient
from herbenzo.clients.pubchem_lookup import PubChemLookup

def test_ncbi_rate_limit_and_identity_params(tmp_path: Path):
    seen: dict[str, str] = {}

    def transport(url: str, headers: dict, timeout: float):
        seen["url"] = url
        body = json.dumps({"esearchresult": {"count": "0", "idlist": []}}).encode()
        return 200, body

    keyed = EutilsClient(
        api_key="abc",
        email="dev@example.com",
        tool="herbenzo-pipeline",
        cache_dir=tmp_path / "keyed",
        transport=transport,
        sleep=lambda _seconds: None,
    )
    assert keyed.min_interval_s == pytest.approx(0.1)
    keyed.search("taxonomy", "Bacopa monnieri", retmax=1)
    assert "api_key=abc" in seen["url"]
    assert "email=dev%40example.com" in seen["url"] or "email=dev@example.com" in seen["url"]
    assert "tool=herbenzo-pipeline" in seen["url"]
    assert "db=taxonomy" in seen["url"]

    def keyless_transport(url: str, headers: dict, timeout: float):
        seen["keyless"] = url
        return 200, json.dumps({"esearchresult": {"count": "0", "idlist": []}}).encode()

    keyless = EutilsClient(
        api_key=None,
        email=None,
        tool="herbenzo-pipeline",
        cache_dir=tmp_path / "keyless",
        transport=keyless_transport,
        sleep=lambda _seconds: None,
    )
    assert keyless.min_interval_s == pytest.approx(1 / 3)
    keyless.search("pubmed", "Bacopa", retmax=1)
    assert "api_key=" not in seen["keyless"]


def test_json_cache_and_retry(tmp_path: Path):
    calls = {"n": 0}

    def transport(url: str, headers: dict, timeout: float):
        calls["n"] += 1
        if calls["n"] == 1:
            return 503, b"busy"
        return 200, b'{"ok": true}'

    slept: list[float] = []
    client = CachedJsonClient(
        cache_dir=tmp_path,
        transport=transport,
        min_interval_s=0,
        max_retries=3,
        backoff_base_s=0.2,
        sleep=slept.append,
    )
    body, retrieved = client.get_json("https://example.test/a", cache_key="alpha")
    again, retrieved_again = client.get_json("https://example.test/a", cache_key="alpha")
    assert body == {"ok": True}
    assert again == body
    assert retrieved == retrieved_again
    assert calls["n"] == 2
    assert slept == [0.2]

    def missing(url: str, headers: dict, timeout: float):
        calls["miss"] = calls.get("miss", 0) + 1
        return 404, b"nope"

    missing_client = CachedJsonClient(
        cache_dir=tmp_path / "miss",
        transport=missing,
        min_interval_s=0,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(HttpError) as raised:
        missing_client.get_json("https://example.test/missing", cache_key="missing")
    assert raised.value.status == 404
    assert calls["miss"] == 1


def test_chemical_taxonomy_pubchem_preferred_and_cached(tmp_path: Path):
    calls: list[str] = []

    def transport(url: str, headers: dict, timeout: float):
        calls.append(url)
        if "classification/JSON" in url:
            payload = {
                "Hierarchies": {
                    "Hierarchy": {
                        "SourceName": "ClassyFire",
                        "Node": {
                            "Information": {
                                "Name": "Organic compounds",
                                "Description": "Kingdom",
                                "URL": "http://classyfire.wishartlab.com/tax_nodes/CHEMONTID:0000000",
                            },
                            "Node": {
                                "Information": {"Name": "Lipids", "Description": "Superclass"},
                                "Node": {
                                    "Information": {"Name": "Steroids", "Description": "Class"},
                                    "Node": {
                                        "Information": {"Name": "Lactones", "Description": "Subclass"},
                                        "Node": {
                                            "Information": {"Name": "Withanolides", "Description": "Direct parent"}
                                        },
                                    },
                                },
                            },
                        },
                    }
                }
            }
        elif "npclassifier" in url:
            payload = {
                "pathway_results": ["Terpenoids"],
                "superclass_results": ["Triterpenoids"],
                "class_results": ["Steroids"],
                "isglycoside": False,
            }
        else:
            raise AssertionError(url)
        return 200, json.dumps(payload).encode()

    client = ChemicalTaxonomyClient(
        cache_dir=tmp_path,
        transport=transport,
        min_interval_s=0,
        sleep=lambda _seconds: None,
    )
    first = client.classify(cid=5, inchikey="AAAAAAAAAAAAAA-AAAAAAAAAA-A", smiles="CCO")
    second = client.classify(cid=5, inchikey="AAAAAAAAAAAAAA-AAAAAAAAAA-A", smiles="CCO")
    assert first["classyfire"]["status"] == "ok"
    assert first["classyfire"]["kingdom"] == "Organic compounds"
    assert first["classyfire"]["superclass"] == "Lipids"
    assert first["classyfire"]["class"] == "Steroids"
    assert first["classyfire"]["subclass"] == "Lactones"
    assert first["classyfire"]["direct_parent"] == "Withanolides"
    assert first["classyfire"]["source"] == "PubChem classification (ClassyFire/ChemOnt)"
    assert first["classyfire"]["retrieved_at"]
    assert first["npclassifier"]["pathway"] == ["Terpenoids"]
    assert first["npclassifier"]["retrieved_at"]
    assert second["classyfire"]["retrieved_at"] == first["classyfire"]["retrieved_at"]
    assert len(calls) == 2
    assert all("classyfire.wishartlab.com/entities" not in url for url in calls)


def test_missing_chemical_taxonomy_is_unavailable(tmp_path: Path):
    def transport(url: str, headers: dict, timeout: float):
        return 404, b"missing"

    client = ChemicalTaxonomyClient(
        cache_dir=tmp_path,
        transport=transport,
        min_interval_s=0,
        sleep=lambda _seconds: None,
    )
    result = client.classify(cid=9, inchikey="BBBBBBBBBBBBBB-BBBBBBBBBB-B", smiles="N")
    assert result["classyfire"]["status"] == "unavailable"
    assert result["classyfire"]["kingdom"] is None
    assert result["npclassifier"]["status"] == "unavailable"
    assert result["npclassifier"]["pathway"] == []


def test_classyfire_api_fallback_when_pubchem_has_no_tree(tmp_path: Path):
    def transport(url: str, headers: dict, timeout: float):
        if "classification/JSON" in url:
            return 404, b"no tree"
        if "classyfire.wishartlab.com" in url:
            payload = {
                "kingdom": {"name": "Organic compounds", "chemont_id": "CHEMONTID:0000000"},
                "superclass": {"name": "Organoheterocyclic compounds", "chemont_id": "CHEMONTID:0000002"},
                "class": {"name": "Lactones", "chemont_id": "CHEMONTID:0000010"},
                "subclass": {"name": "Gamma-lactones", "chemont_id": "CHEMONTID:0000011"},
                "direct_parent": {"name": "Butenolides", "chemont_id": "CHEMONTID:0000012"},
            }
            return 200, json.dumps(payload).encode()
        if "npclassifier" in url:
            return 200, json.dumps({"pathway_results": [], "superclass_results": [], "class_results": []}).encode()
        raise AssertionError(url)

    client = ChemicalTaxonomyClient(
        cache_dir=tmp_path, transport=transport, min_interval_s=0, sleep=lambda _seconds: None
    )
    result = client.classify(cid=3, inchikey="CCCCCCCCCCCCCC-CCCCCCCCCC-C", smiles=None)
    assert result["classyfire"]["source"] == "ClassyFire API"
    assert result["classyfire"]["direct_parent"] == "Butenolides"
    assert result["classyfire"]["chemont_ids"]["kingdom"] == "CHEMONTID:0000000"
    assert result["npclassifier"]["status"] == "unavailable"


def test_llm_client_strips_numeric_keys(tmp_path: Path):
    def transport(url: str, body: bytes, headers: dict, timeout: float):
        assert url.endswith("/chat/completions")
        assert headers["Authorization"] == "Bearer test-key"
        content = json.dumps(
            {
                "narrative": "Ranked from the abstracts.",
                "molecular_weight": 1,
                "ranking": [{"name": "Bacoside A", "rationale": "cited", "xlogp": 99}],
            }
        )
        payload = {"choices": [{"message": {"content": content}}]}
        return 200, json.dumps(payload).encode()

    client = LlmClient(api_key="test-key", base_url="https://llm.example/v1", model="fake", transport=transport)
    result = client.justify({"markers": ["Bacoside A"]})
    assert result["status"] == "ok"
    assert result["numerics_ignored"] is True
    assert result["narrative"] == "Ranked from the abstracts."
    assert result["ranking"] == [{"name": "Bacoside A", "rationale": "cited"}]


def test_pubchem_aid_counts_treat_404_as_zero(tmp_path: Path):
    def transport(url: str, headers: dict, timeout: float):
        if "/property/" in url:
            payload = {
                "PropertyTable": {
                    "Properties": [
                        {
                            "CID": 7,
                            "Title": "Example",
                            "MolecularWeight": "10",
                            "XLogP": 0.4,
                            "TPSA": 1,
                            "HBondDonorCount": 0,
                            "HBondAcceptorCount": 1,
                            "RotatableBondCount": 0,
                            "InChIKey": "DDDDDDDDDDDDDD-DDDDDDDDDD-D",
                            "CanonicalSMILES": "C",
                        }
                    ]
                }
            }
            return 200, json.dumps(payload).encode()
        return 404, b"no assays"

    lookup = PubChemLookup(cache_dir=tmp_path, transport=transport, min_interval_s=0, sleep=lambda _s: None)
    props, _retrieved = lookup.properties(7)
    assert props["molecular_weight"] == 10.0
    assert props["xlogp"] == 0.4
    assays, _when = lookup.bioassays(7)
    assert assays["status"] == "ok"
    assert assays["total"] == 0
    assert assays["active"] == 0



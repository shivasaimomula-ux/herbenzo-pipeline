"""Ingredient enrichment: mocked NCBI/PubChem/LLM, registry gate, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.api import app
from herbenzo.clients.chemclass import ChemicalTaxonomyClient
from herbenzo.clients.eutils import EutilsClient
from herbenzo.clients.httpjson import CachedJsonClient, HttpError
from herbenzo.clients.llm import LlmClient
from herbenzo.clients.pubchem_lookup import PubChemLookup
from herbenzo.services.candidate_store import CandidateStore
from herbenzo.services.enrichment import EnrichmentError, EnrichmentService, registry_properties
from herbenzo.services.registries import StaticRegistriesClient

_TAXONOMY_XML = """<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>999001</TaxId>
  <ScientificName>Bacopa monnieri</ScientificName>
  <Rank>species</Rank>
  <Division>Plants and Fungi</Division>
  <OtherNames>
    <Synonym>Herpestis monniera</Synonym>
    <CommonName>bacopa</CommonName>
  </OtherNames>
  <Lineage>cellular organisms; Eukaryota; Bacopa</Lineage>
</Taxon></TaxaSet>
"""

_RETRIEVED = "2026-10-06T00:00:00+00:00"


class FakeEutils:
    def __init__(self, *, taxonomy_ids: list[str] | None = None) -> None:
        self.taxonomy_ids = ["999001"] if taxonomy_ids is None else taxonomy_ids

    def search(self, db: str, term: str, *, retmax: int = 5) -> dict:
        ids = {
            "taxonomy": self.taxonomy_ids,
            "pccompound": ["100", "200"],
            "pubmed": ["321"],
            "gene": ["55"],
            "protein": ["AAA000.1"],
        }.get(db, [])
        return {
            "db": db,
            "term": term,
            "count": len(ids),
            "ids": ids[:retmax],
            "retrieved_at": _RETRIEVED,
        }

    def summary(self, db: str, ids: list[str]) -> dict:
        records = []
        if db == "pubmed":
            records = [
                {"uid": "321", "title": "Bacosides of Bacopa monnieri", "source": "Phytochemistry", "pubdate": "2020"}
            ]
        elif db == "gene":
            records = [{"uid": "55", "name": "BACO", "description": "example gene", "organism": {"scientificname": "Bacopa monnieri"}}]
        elif db == "protein":
            records = [{"accessionversion": "AAA000.1", "title": "example protein", "taxname": "Bacopa monnieri", "slen": 40}]
        return {"db": db, "records": records, "retrieved_at": _RETRIEVED}

    def fetch_text(self, db: str, ids: list[str], *, retmode: str = "xml") -> tuple[str, str]:
        assert db == "taxonomy"
        assert ids == ["999001"]
        return _TAXONOMY_XML, _RETRIEVED


def _props(cid: int, title: str, xlogp: float, weight: float) -> dict:
    return {
        "cid": cid,
        "title": title,
        "molecular_formula": "C10H10O2",
        "molecular_weight": weight,
        "xlogp": xlogp,
        "tpsa": 40.0,
        "hbd": 1,
        "hba": 2,
        "rotatable_bonds": 3,
        "inchi_key": "AAAAAAAAAAAAAA-AAAAAAAAAA-A",
        "canonical_smiles": "CCO",
        "iupac_name": title.lower(),
    }


class FakePubChem:
    def properties(self, cid: int):
        table = {
            100: _props(100, "Bacoside A", 1.5, 769.0),
            200: _props(200, "Bacopaside I", 2.5, 979.0),
        }
        return table[cid], _RETRIEVED

    def synonyms(self, cid: int):
        return ["synonym"], _RETRIEVED

    def bioassays(self, cid: int):
        return {
            "status": "ok",
            "total": 4,
            "active": 1,
            "source": "PubChem PUG-REST",
            "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
            "retrieved_at": _RETRIEVED,
            "error": None,
        }, _RETRIEVED


class FakeChem:
    def classify(self, *, cid: int | None, inchikey: str | None, smiles: str | None) -> dict:
        return {
            "classyfire": {
                "status": "ok",
                "kingdom": "Organic compounds",
                "superclass": "Lipids and lipid-like molecules",
                "class": "Steroids and steroid derivatives",
                "subclass": "Steroid lactones",
                "direct_parent": "Withanolides",
                "chemont_ids": {"kingdom": "CHEMONTID:0000000"},
                "source": "ClassyFire API",
                "url": "http://classyfire.wishartlab.com/entities/AAAAAAAAAAAAAA-AAAAAAAAAA-A",
                "retrieved_at": _RETRIEVED,
                "error": None,
            },
            "npclassifier": {
                "status": "ok",
                "pathway": ["Terpenoids"],
                "superclass": ["Triterpenoids"],
                "class": ["Steroids"],
                "isglycoside": False,
                "source": "NPClassifier",
                "url": "https://npclassifier.gnps2.org/classify",
                "retrieved_at": _RETRIEVED,
                "error": None,
            },
        }


class RankingLlm:
    available = True

    def justify(self, context: dict) -> dict:
        return {
            "status": "ok",
            "provider": "openai-compatible",
            "model": "fake",
            "narrative": "Prefer the more specific saponin.",
            "ranking": [
                {
                    "name": "Bacopaside I",
                    "rationale": "more specific marker",
                    "xlogp": 99,
                    "molecular_weight": 1,
                },
                {"name": "NotARealCompound", "rationale": "hallucinated", "logp": 5},
            ],
            "xlogp": 99,
            "molecular_weight": 1,
            "numerics_ignored": False,
            "ignored_names": [],
            "error": None,
        }


def _service(tmp_path: Path, llm) -> EnrichmentService:
    return EnrichmentService(
        eutils=FakeEutils(),
        pubchem=FakePubChem(),
        chemclass=FakeChem(),
        llm=llm,
        store=CandidateStore(tmp_path),
    )


def _spec(ingredient_id: str) -> dict:
    return {
        "formulation_id": "F-BACO-1",
        "product_name": "Bacopa capsule",
        "dosage_form": "capsule",
        "target_market": "US",
        "servings_per_day": 1,
        "confidence": 0.8,
        "ingredients": [
            {
                "ingredient_id": ingredient_id,
                "botanical_name": "Bacopa monnieri",
                "common_name": "bacopa",
                "part_used": "herb",
                "quantity_mg": 300,
            }
        ],
    }


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


def test_pending_candidate_is_gated_until_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERBENZO_REGISTRY_DIR", str(tmp_path))
    service = _service(tmp_path, RankingLlm())
    doc = service.propose("Bacopa monnieri", part_used="herb")
    assert doc["status"] == "pending"
    assert doc["proposed_ingredient_id"] == "HB-BACO"
    assert doc["llm_numerics_applied"] is False
    assert doc["numeric_policy"] == "pubchem_only"
    assert doc["justification"]["numerics_ignored"] is True
    assert "NotARealCompound" in doc["justification"]["ignored_names"]
    names = [marker["name"] for marker in doc["markers"]]
    assert names == ["Bacopaside I", "Bacoside A"]
    top = doc["markers"][0]
    assert top["pubchem"]["xlogp"] == 2.5
    assert top["pubchem"]["molecular_weight"] == 979.0
    assert top["pubchem"]["source"].startswith("PubChem PUG-REST")
    assert top["chemical_taxonomy"]["classyfire"]["kingdom"] == "Organic compounds"
    assert top["chemical_taxonomy"]["classyfire"]["direct_parent"] == "Withanolides"
    assert top["chemical_taxonomy"]["npclassifier"]["pathway"] == ["Terpenoids"]
    assert top["chemical_taxonomy"]["classyfire"]["retrieved_at"] == _RETRIEVED
    assert doc["taxonomy"]["tax_id"] == 999001
    assert doc["literature"]["articles"][0]["pmid"] == "321"
    copied = registry_properties(top)
    assert copied["xlogp"] == 2.5
    assert copied["molecular_weight"] == 979.0
    assert "xlogp" not in doc["justification"]

    client = TestClient(app)
    listed = client.get("/ingredients")
    assert "HB-BACO" not in [row["ingredient_id"] for row in listed.json()["ingredients"]]
    assert all("Bacopa" not in row["botanical_name"] for row in listed.json()["ingredients"])
    rejected = client.post("/modernize", json=_spec("HB-BACO"))
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["error"] == "unknown_ingredient"
    assert rejected.json()["detail"]["unknown_ids"] == ["HB-BACO"]

    monkeypatch.setattr("herbenzo.enrich_api.build_enrichment_service", lambda: service)
    approved = client.post(f"/enrich/candidates/{doc['candidate_id']}/approve", json={})
    assert approved.status_code == 200, approved.text
    assert approved.json()["decision"]["ingredient_id"] == "HB-BACO"
    rows = client.get("/ingredients").json()["ingredients"]
    match = next(row for row in rows if row["ingredient_id"] == "HB-BACO")
    assert match["botanical_name"] == "Bacopa monnieri"
    assert match["markers"][0]["marker_name"] == "Bacopaside I"
    modernized = client.post("/modernize", json=_spec("HB-BACO"))
    assert modernized.status_code == 200, modernized.text
    props = modernized.json()["ingredients"][0]["marker"]["properties"]
    assert props["xlogp"] == 2.5
    assert props["molecular_weight"] == 979.0
    assert props["pubchem_cid"] == 200
    assert StaticRegistriesClient().lookup_ingredient("HB-BACO").common_name == "bacopa"


def test_llm_absent_keeps_pubchem_order(tmp_path: Path):
    service = _service(tmp_path, LlmClient(api_key=None))
    doc = service.propose("Bacopa monnieri")
    assert doc["justification"]["status"] == "unavailable"
    assert [marker["name"] for marker in doc["markers"]] == ["Bacoside A", "Bacopaside I"]
    assert doc["markers"][0]["pubchem"]["xlogp"] == 1.5


def test_reject_path_stays_out_of_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERBENZO_REGISTRY_DIR", str(tmp_path))
    service = _service(tmp_path, LlmClient(api_key=None))
    doc = service.propose("Bacopa monnieri")
    rejected = service.reject(doc["candidate_id"], reason="not a marker we standardize")
    assert rejected["status"] == "rejected"
    assert rejected["decision"]["reason"] == "not a marker we standardize"
    client = TestClient(app)
    ids = [row["ingredient_id"] for row in client.get("/ingredients").json()["ingredients"]]
    assert doc["proposed_ingredient_id"] not in ids
    modernized = client.post("/modernize", json=_spec(doc["proposed_ingredient_id"]))
    assert modernized.status_code == 422
    assert modernized.json()["detail"]["unknown_ids"] == [doc["proposed_ingredient_id"]]
    with pytest.raises(EnrichmentError) as raised:
        service.approve(doc["candidate_id"])
    assert raised.value.status_code == 409


def test_taxonomy_miss_does_not_create_a_candidate(tmp_path: Path):
    service = EnrichmentService(
        eutils=FakeEutils(taxonomy_ids=[]),
        pubchem=FakePubChem(),
        chemclass=FakeChem(),
        llm=LlmClient(api_key=None),
        store=CandidateStore(tmp_path),
    )
    with pytest.raises(EnrichmentError) as raised:
        service.propose("not a real organism")
    assert raised.value.code == "taxonomy_unresolved"
    assert service.list() == []


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


def test_cli_lists_and_shows_a_pending_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("HERBENZO_REGISTRY_DIR", str(tmp_path))
    monkeypatch.setenv("HERBENZO_ENRICHMENT_CACHE_DIR", str(tmp_path / "cache"))
    service = _service(tmp_path, LlmClient(api_key=None))
    doc = service.propose("Bacopa monnieri")
    from herbenzo.cli import main

    assert main(["enrich", "list"]) == 0
    listed = capsys.readouterr().out
    assert doc["candidate_id"] in listed
    assert "pending" in listed
    assert main(["enrich", "show", doc["candidate_id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["candidate_id"] == doc["candidate_id"]
    assert shown["status"] == "pending"
    assert main(["enrich", "reject", doc["candidate_id"], "--reason", "duplicate"]) == 0
    assert "rejected" in capsys.readouterr().out

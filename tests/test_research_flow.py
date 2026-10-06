"""Registry-free research: live lookups, Clitoria marker guardrails, Gemini front door.

Network clients are fakes. Nothing is written to an ingredient list.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.api import app
from herbenzo.clients.llm import LlmClient
from herbenzo.services.gemini_research import ResearchFrontDoor
from herbenzo.services.records import ResearchError
from herbenzo.services.research import ResearchService

_RETRIEVED = "2026-10-06T00:00:00+00:00"
_CLITORIA_XML = """<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>43366</TaxId>
  <ScientificName>Clitoria ternatea</ScientificName>
  <Rank>species</Rank>
  <OtherNames><CommonName>butterfly pea</CommonName></OtherNames>
</Taxon></TaxaSet>
"""
_WITHANIA_XML = """<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>126636</TaxId>
  <ScientificName>Withania somnifera</ScientificName>
  <Rank>species</Rank>
  <OtherNames><CommonName>ashwagandha</CommonName></OtherNames>
</Taxon></TaxaSet>
"""
_NOVEL_XML = """<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>999111</TaxId>
  <ScientificName>Novelus herbacea</ScientificName>
  <Rank>species</Rank>
  <OtherNames><CommonName>never-before-seen herb</CommonName></OtherNames>
</Taxon></TaxaSet>
"""
_UNII = [176489889, 176489876, 176489874, 176489868, 176489837, 176489822]
_PMIDS = ["26120869", "18926895", "14568080", "34975979", "21214440", "12490229"]
_TITLES = {
    "26120869": "Ternatins inhibit NF-kB translocation and iNOS in macrophages",
    "18926895": "Clitoria ternatea pharmacology review of constituents and pathways",
    "14568080": "Flavonoid composition and ternatins in Clitoria ternatea petals",
    "34975979": "Anthocyanin pathway of Clitoria ternatea flower phytochemistry",
    "21214440": "Clitoria ternatea extracts and acetylcholine in a memory model",
    "12490229": "Root extract raises acetylcholine content",
}


def _props(cid: int, title: str, *, xlogp, weight: float) -> dict:
    return {
        "cid": cid,
        "title": title,
        "molecular_weight": weight,
        "xlogp": xlogp,
        "tpsa": 80.0,
        "hbd": 2,
        "hba": 4,
        "rotatable_bonds": 3,
        "inchi_key": "AAAAAAAAAAAAAA-AAAAAAAAAA-A",
        "canonical_smiles": "C",
        "iupac_name": title,
    }


class CountingEutils:
    def __init__(self, *, scientific: str, tax_xml: str, pccompound: dict[str, list[str]] | None = None) -> None:
        self.scientific = scientific
        self.tax_xml = tax_xml
        self.pccompound = pccompound or {}
        self.calls: list[tuple] = []

    def search(self, db: str, term: str, *, retmax: int = 5, sort: str | None = None) -> dict:
        self.calls.append((db, term, sort))
        if db == "taxonomy":
            ids = ["43366"] if "Clitoria" in self.scientific else ["126636"] if "Withania" in self.scientific else ["999111"]
        elif db == "pubmed":
            ids = list(_PMIDS)
        elif db == "pccompound":
            ids = list(self.pccompound.get(term, []))
        else:
            ids = []
        return {"db": db, "term": term, "count": len(ids), "ids": ids[:retmax], "retrieved_at": _RETRIEVED}

    def summary(self, db: str, ids: list[str]) -> dict:
        records = []
        if db == "pubmed":
            records = [{"uid": pmid, "title": _TITLES[pmid], "pubdate": "2015"} for pmid in ids if pmid in _TITLES]
        return {"db": db, "records": records, "retrieved_at": _RETRIEVED}

    def fetch_text(self, db: str, ids: list[str], *, retmode: str = "xml", rettype: str | None = None) -> tuple[str, str]:
        if db == "taxonomy":
            return self.tax_xml, _RETRIEVED
        return "", _RETRIEVED


class ClitoriaPubChem:
    def __init__(self) -> None:
        self.name_calls: list[str] = []

    def cids_by_name(self, name: str):
        self.name_calls.append(name)
        key = " ".join(name.split()).casefold()
        table = {
            "ternatin": [5459184, 192406],
            "ternatin a1": [16173494],
            "clitorin": [11592917],
            "noveloside": [777],
        }
        return table.get(key, []), _RETRIEVED

    def taxonomy_links(self, cid: int) -> dict:
        linked = {16173494, 11592917, 777, *_UNII}
        wrong = {5459184: [999001], 192406: [999002]}
        if cid in wrong:
            return {"status": "ok", "tax_ids": wrong[cid], "retrieved_at": _RETRIEVED}
        if cid in linked:
            return {"status": "ok", "tax_ids": [43366 if cid != 777 else 999111], "retrieved_at": _RETRIEVED}
        return {"status": "ok", "tax_ids": [], "retrieved_at": _RETRIEVED}

    def properties(self, cid: int):
        if cid == 16173494:
            return _props(cid, "Ternatin A1", xlogp=None, weight=2108.8), _RETRIEVED
        if cid == 11592917:
            return _props(cid, "Clitorin", xlogp=-2.0, weight=740.7), _RETRIEVED
        if cid == 5459184:
            return _props(cid, "Ternatin", xlogp=3.1, weight=374.3), _RETRIEVED
        if cid == 192406:
            return _props(cid, "Ternatin", xlogp=3.8, weight=738.0), _RETRIEVED
        if cid == 777:
            return _props(cid, "Noveloside", xlogp=1.2, weight=400.0), _RETRIEVED
        if cid in _UNII:
            return _props(cid, "VW8J4G7G7W", xlogp=None, weight=2107.8), _RETRIEVED
        raise KeyError(cid)

    def synonyms(self, cid: int):
        if cid in _UNII:
            return ["Ternatin A1", "VW8J4G7G7W"], _RETRIEVED
        return [], _RETRIEVED

    def bioassays(self, cid: int):
        return {"status": "ok", "total": 0, "active": 0, "source": "PubChem", "url": "", "retrieved_at": _RETRIEVED, "error": None}, _RETRIEVED


class QuietChem:
    def classify(self, **kwargs):
        return {"classyfire": {"status": "unavailable"}, "npclassifier": {"status": "unavailable"}}


def _clitoria_service() -> tuple[ResearchService, CountingEutils, ClitoriaPubChem]:
    eutils = CountingEutils(
        scientific="Clitoria ternatea",
        tax_xml=_CLITORIA_XML,
        pccompound={
            '"Clitoria ternatea"': [],
            "Clitoria ternatea": [str(cid) for cid in _UNII],
        },
    )
    pubchem = ClitoriaPubChem()
    service = ResearchService(
        eutils=eutils,
        pubchem=pubchem,
        chemclass=QuietChem(),
        llm=LlmClient(api_key=None),
        min_pubmed_refs=1,
    )
    return service, eutils, pubchem


def _spec(ingredient_id: str, botanical: str) -> dict:
    return {
        "formulation_id": "F-LIVE-1",
        "product_name": botanical,
        "dosage_form": "capsule",
        "target_market": "US",
        "servings_per_day": 1,
        "confidence": 0.8,
        "ingredients": [
            {
                "ingredient_id": ingredient_id,
                "botanical_name": botanical,
                "quantity_mg": 250,
            }
        ],
    }


def test_runtime_registry_modules_are_gone():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "herbenzo" / "data" / "marker_properties.json").exists()
    for name in (
        "herbenzo.services.registries",
        "herbenzo.services.overlay",
        "herbenzo.services.candidate_store",
        "herbenzo.services.live_registries",
        "herbenzo.services.enrichment",
        "herbenzo.enrich_api",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_clitoria_quoted_search_is_empty_and_bare_ternatin_is_not_attached():
    service, eutils, pubchem = _clitoria_service()
    first = service.research("Clitoria ternatea")
    second = service.research("Clitoria ternatea")
    assert len(eutils.calls) >= 2
    assert sum(1 for db, term, _sort in eutils.calls if db == "taxonomy") >= 2
    assert first["ingredient_id"] == "tax-43366"
    assert first["ingredient_id"] != "HB-CLIT"
    assert first["species_search"]["quoted"]["count"] == 0
    assert first["species_search"]["unquoted"]["count"] == 6
    pubmed = [call for call in eutils.calls if call[0] == "pubmed"]
    assert pubmed
    assert pubmed[0][2] == "relevance"
    assert "pharmacology" in pubmed[0][1]
    pmids = [article["pmid"] for article in first["literature"]["articles"]]
    assert "26120869" in pmids
    assert any("NF-kB" in path or "iNOS" in path for path in first["pathways"])
    assert first["marker_status"] == "pending"
    assert first["selected_marker"] is None
    assert 5459184 not in [cid for marker in first["markers"] for cid in marker.get("candidate_cids") or []] or any(
        marker.get("resolution") == "ambiguous" for marker in first["markers"]
    )
    bare = service.approve(first, marker_name="ternatin")
    assert bare["marker_status"] == "pending"
    assert bare["properties"] == {}
    assert "ternatin" in pubchem.name_calls
    attached = service.approve(second, marker_name="ternatin A1")
    assert attached["marker_status"] == "resolved"
    assert attached["properties"]["Ternatin A1"]["pubchem_cid"] == 16173494
    assert attached["properties"]["Ternatin A1"]["xlogp"] is None
    clitorin = service.approve(first, marker_name="clitorin")
    assert clitorin["properties"]["Clitorin"]["pubchem_cid"] == 11592917
    with pytest.raises(ResearchError) as raised:
        service.set_marker(bare, "ternatin")
    assert raised.value.code == "marker_unverified"
    corrected = service.set_marker(bare, "ternatin A1", note="adjudicated")
    assert corrected["properties"]["Ternatin A1"]["pubchem_cid"] == 16173494
    assert corrected["marker_audit"][-1]["action"] == "set_marker"


def test_clitoria_pending_marker_modernizes_with_provenance_and_gap():
    service, _eutils, _pubchem = _clitoria_service()
    candidate = service.research("butterfly pea")
    approval = service.approve(candidate)
    client = TestClient(app)
    pending = client.post(
        "/modernize",
        json={"spec": _spec(approval["ingredient_id"], "Clitoria ternatea"), "approvals": [approval]},
    )
    assert pending.status_code == 200, pending.text
    body = pending.json()
    assert body["sku"] is None
    gap = body["classical_active_marker_gap"]
    assert gap["blocking"] is False
    assert gap["marker_status"] == "pending"
    assert "marker is pending" in gap["message"]
    assert "/research/marker" in gap["message"]
    assert gap["how_to_add_marker"]["path"] == "/research/marker"
    provenance = body["research_provenance"]["ingredients"][0]
    assert provenance["taxonomy_id"] == 43366
    assert "26120869" in provenance["pmids"]
    marked = service.set_marker(approval, "clitorin")
    done = client.post(
        "/modernize",
        json={"spec": _spec(marked["ingredient_id"], "Clitoria ternatea"), "approvals": [marked]},
    )
    assert done.status_code == 200, done.text
    sku = done.json()
    assert sku["ingredients"][0]["marker"]["marker_name"] == "Clitorin"
    assert sku["ingredients"][0]["marker"]["properties"]["xlogp"] == -2.0
    formats = client.post(
        "/suggest-formats",
        json={"ingredient_ids": [approval["ingredient_id"]], "approvals": [approval]},
    )
    assert formats.status_code == 200, formats.text


def test_withania_is_researched_not_loaded_from_stock():
    eutils = CountingEutils(scientific="Withania somnifera", tax_xml=_WITHANIA_XML, pccompound={})
    service = ResearchService(
        eutils=eutils,
        pubchem=ClitoriaPubChem(),
        chemclass=QuietChem(),
        llm=LlmClient(api_key=None),
    )
    doc = service.research("Withania somnifera")
    assert doc["ingredient_id"] == "tax-126636"
    assert doc["ingredient_id"] != "HB-ASHW"
    assert any(call[0] == "taxonomy" for call in eutils.calls)


def test_compose_approve_posts_the_candidate_and_modernizes_it():
    """The Compose Approve button posts {candidate} and modernize uses that document."""
    service, _eutils, _pubchem = _clitoria_service()
    candidate = service.research("Clitoria ternatea")
    client = TestClient(app)
    approved = client.post("/research/approve", json={"candidate": candidate})
    assert approved.status_code == 200, approved.text
    approval = approved.json()
    assert approval["status"] == "approved"
    assert approval["ingredient_id"] == "tax-43366"
    assert approval["marker_status"] == "pending"
    ui = Path("herbenzo/static/app.js").read_text(encoding="utf-8")
    assert "candidate: lastCandidate" in ui
    assert "not stored in an ingredient list" not in ui
    assert "Nothing is selected for you" in ui
    modernized = client.post(
        "/modernize",
        json={"spec": _spec(approval["ingredient_id"], "Clitoria ternatea"), "approvals": [approval]},
    )
    assert modernized.status_code == 200, modernized.text
    body = modernized.json()
    assert body["sku"] is None
    assert "marker is pending" in body["classical_active_marker_gap"]["message"]
    assert body["classical_active_marker_gap"]["how_to_add_marker"]["method"] == "POST"
    again = client.post("/research/approve", json={"candidate": candidate})
    assert again.status_code == 200


_CENTROSEMA_XML = """<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>1300970</TaxId>
  <ScientificName>Centrosema molle</ScientificName>
  <Rank>species</Rank>
  <OtherNames><CommonName>butterfly pea</CommonName></OtherNames>
</Taxon></TaxaSet>
"""


class _ButterflyNames:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def search(self, db: str, term: str, *, retmax: int = 5, sort: str | None = None) -> dict:
        self.calls.append((db, term, sort))
        ids: list[str] = []
        folded = term.casefold()
        if db == "taxonomy" and "butterfly pea" in folded:
            if "[common name]" in folded:
                ids = ["1300970", "43366"]
            elif "[synonym]" in folded:
                ids = ["43366"]
        return {"db": db, "term": term, "count": len(ids), "ids": ids[:retmax], "retrieved_at": _RETRIEVED}

    def summary(self, db: str, ids: list[str]) -> dict:
        return {"db": db, "records": [], "retrieved_at": _RETRIEVED}

    def fetch_text(self, db: str, ids: list[str], *, retmode: str = "xml", rettype: str | None = None) -> tuple[str, str]:
        tax_id = str(ids[0]) if ids else ""
        if tax_id == "43366":
            return _CLITORIA_XML, _RETRIEVED
        if tax_id == "1300970":
            return _CENTROSEMA_XML, _RETRIEVED
        return "", _RETRIEVED


def test_butterfly_pea_suggests_every_taxon_and_picks_none():
    eutils = _ButterflyNames()
    service = ResearchService(eutils=eutils, pubchem=ClitoriaPubChem(), chemclass=QuietChem(), llm=LlmClient(api_key=None))
    found = service.suggest("butterfly pea")
    assert found["auto_selected"] is None
    assert found["ambiguous"] is True
    by_id = {row["tax_id"]: row for row in found["suggestions"]}
    assert set(by_id) == {43366, 1300970}
    assert by_id[43366]["scientific_name"] == "Clitoria ternatea"
    assert by_id[1300970]["scientific_name"] == "Centrosema molle"
    assert "Common Name" in by_id[43366]["matched_fields"] or "Synonym" in by_id[43366]["matched_fields"]
    fields = [call[1] for call in eutils.calls if call[0] == "taxonomy"]
    assert any("[Scientific Name]" in term for term in fields)
    assert any("[Common Name]" in term for term in fields)
    assert any("[Synonym]" in term for term in fields)
    assert found["suggestions"][0]["tax_id"] != found.get("auto_selected")


def test_suggest_is_live_and_empty_on_failure(monkeypatch: pytest.MonkeyPatch):
    eutils = CountingEutils(scientific="Clitoria ternatea", tax_xml=_CLITORIA_XML)
    service = ResearchService(eutils=eutils, pubchem=ClitoriaPubChem(), chemclass=QuietChem(), llm=LlmClient(api_key=None))
    import herbenzo.research_api as research_api

    research_api._FRONT = ResearchFrontDoor(service)
    client = TestClient(app)
    first = client.get("/research/suggest", params={"q": "clit"})
    second = client.get("/research/suggest", params={"q": "clit"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["suggestions"][0]["scientific_name"] == "Clitoria ternatea"
    assert first.json()["suggestions"][0]["tax_id"] == 43366
    taxonomy_calls = [call for call in eutils.calls if call[0] == "taxonomy"]
    assert len(taxonomy_calls) >= 2

    class Broken:
        def search(self, *args, **kwargs):
            raise RuntimeError("upstream down")

        def summary(self, *args, **kwargs):
            return {"records": []}

        def fetch_text(self, *args, **kwargs):
            raise RuntimeError("upstream down")

    research_api._FRONT = ResearchFrontDoor(
        ResearchService(eutils=Broken(), pubchem=ClitoriaPubChem(), chemclass=QuietChem(), llm=LlmClient(api_key=None))
    )
    empty = client.post("/research/suggest", json={"q": "zzzz-no-such"})
    assert empty.status_code == 200
    assert empty.json()["suggestions"] == []
    research_api._FRONT = None


def test_suggestion_still_has_to_pass_the_evidence_gate():
    eutils = CountingEutils(scientific="Clitoria ternatea", tax_xml=_CLITORIA_XML)
    service = ResearchService(
        eutils=eutils,
        pubchem=ClitoriaPubChem(),
        chemclass=QuietChem(),
        llm=LlmClient(api_key=None),
        min_pubmed_refs=50,
    )
    suggestion = service.suggest("Clitoria")
    assert suggestion["suggestions"]
    candidate = service.research(suggestion["suggestions"][0]["scientific_name"])
    with pytest.raises(ResearchError) as raised:
        service.approve(candidate)
    assert raised.value.code == "insufficient_evidence"


def test_unapproved_research_does_not_modernize():
    service, _eutils, _pubchem = _clitoria_service()
    candidate = service.research("Clitoria ternatea")
    client = TestClient(app)
    response = client.post("/modernize", json=_spec(candidate["ingredient_id"], "Clitoria ternatea"))
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "not_approved"


def test_gemini_front_door_strips_hallucinations_and_uses_tool_numbers():
    eutils = CountingEutils(scientific="Novelus herbacea", tax_xml=_NOVEL_XML, pccompound={})
    pubchem = ClitoriaPubChem()
    llm = LlmClient(api_key="test", base_url="https://llm.example/v1", model="fake-model")
    service = ResearchService(eutils=eutils, pubchem=pubchem, chemclass=QuietChem(), llm=llm, min_pubmed_refs=1)
    steps = {"n": 0}

    def completer(messages, tools):
        steps["n"] += 1
        plan = [
            {"name": "taxonomy_search", "arguments": {"query": "Novelus herbacea"}},
            {"name": "pubmed_search", "arguments": {"term": "Novelus herbacea pharmacology"}},
            {"name": "pubchem_name", "arguments": {"name": "noveloside"}},
            {"name": "web_search", "arguments": {"query": "novelus herbacea supplement"}},
        ]
        if steps["n"] <= len(plan):
            item = plan[steps["n"] - 1]
            return {
                "content": "",
                "tool_calls": [{"id": str(steps["n"]), "function": item}],
            }
        return {
            "content": "See PMID 99999999 and CID 1 at https://evil.example/fake. molecular weight 9999.",
            "tool_calls": [],
        }

    door = ResearchFrontDoor(service, completer=completer, max_steps=8)
    doc = door.research("Novelus herbacea")
    assert doc["research_path"] == "gemini"
    assert doc["ingredient_id"] == "tax-999111"
    assert doc["selected_marker"]["pubchem"]["cid"] == 777
    assert doc["selected_marker"]["pubchem"]["molecular_weight"] == 400.0
    assert doc["selected_marker"]["pubchem"]["xlogp"] == 1.2
    ignored = {item["value"] for item in doc["ignored_claims"]}
    assert "99999999" in ignored
    assert "https://evil.example/fake" in ignored
    narrative = doc["justification"]["narrative"] or ""
    assert "99999999" not in narrative
    assert doc["web"]["status"] == "unavailable"
    approval = service.approve(doc)
    client = TestClient(app)
    modernized = client.post(
        "/modernize",
        json={"spec": _spec(approval["ingredient_id"], "Novelus herbacea"), "approvals": [approval]},
    )
    assert modernized.status_code == 200, modernized.text
    assert modernized.json()["ingredients"][0]["marker"]["properties"]["pubchem_cid"] == 777


def test_gemini_loop_cap_and_fallback(monkeypatch: pytest.MonkeyPatch):
    eutils = CountingEutils(scientific="Novelus herbacea", tax_xml=_NOVEL_XML)
    llm = LlmClient(api_key="test", base_url="https://llm.example/v1", model="fake-model")
    service = ResearchService(eutils=eutils, pubchem=ClitoriaPubChem(), chemclass=QuietChem(), llm=llm)
    calls = {"n": 0}

    def always_tools(messages, tools):
        calls["n"] += 1
        return {
            "content": "",
            "tool_calls": [{"id": "t", "function": {"name": "taxonomy_search", "arguments": {"query": "Novelus herbacea"}}}],
        }

    capped = ResearchFrontDoor(service, completer=always_tools, max_steps=2).research("Novelus herbacea")
    assert calls["n"] == 2
    assert capped["truncated"] is True
    assert capped["research_path"] == "gemini"

    def explode(messages, tools):
        raise TimeoutError("too slow")

    fallback = ResearchFrontDoor(service, completer=explode, timeout_s=5).research("Novelus herbacea")
    assert fallback["research_path"] == "deterministic_fallback"

    bad = LlmClient(
        api_key="test",
        base_url="https://generativelanguage.googleapis.com",
        model="gemini-4.0-argon",
    )
    invalid = ResearchService(eutils=eutils, pubchem=ClitoriaPubChem(), chemclass=QuietChem(), llm=bad)
    doc = ResearchFrontDoor(invalid, completer=explode).research("Novelus herbacea")
    assert doc["research_path"] == "deterministic_fallback"
    assert "404" in doc["llm_config"]["message"]
    assert "gemini-2.5-flash" in doc["llm_config"]["message"]


def test_genus_rank_is_not_approvable():
    xml = _CLITORIA_XML.replace("<Rank>species</Rank>", "<Rank>genus</Rank>")
    eutils = CountingEutils(scientific="Clitoria ternatea", tax_xml=xml)
    service = ResearchService(eutils=eutils, pubchem=ClitoriaPubChem(), chemclass=QuietChem(), llm=LlmClient(api_key=None))
    candidate = service.research("Clitoria")
    with pytest.raises(ResearchError) as raised:
        service.approve(candidate)
    assert raised.value.code == "identity_unresolved"


def test_name_only_override_is_rejected_on_modernize():
    service, _eutils, _pubchem = _clitoria_service()
    approval = service.approve(service.research("Clitoria ternatea"))
    client = TestClient(app)
    response = client.post(
        "/modernize",
        json={
            "spec": _spec(approval["ingredient_id"], "Clitoria ternatea"),
            "approvals": [approval],
            "marker_overrides": [{"ingredient_id": approval["ingredient_id"], "marker_name": "clitorin"}],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "marker_unverified"

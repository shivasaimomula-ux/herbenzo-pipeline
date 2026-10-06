"""IMPPAT 3.0 is optional context after NCBI Taxonomy. It is not a registry gate.

Fixtures are synthetic. They use the batch-file column names and obviously
fake organisms. They are not copied from IMPPAT.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.api import app
from herbenzo.clients.llm import LlmClient
from herbenzo.config import get_settings, repo_root
from herbenzo.services.candidate_store import CandidateStore
from herbenzo.services.enrichment import EnrichmentService
from herbenzo.services.imppat import (
    PHYTO_FILE,
    PLANT_FILE,
    POLY_FILE,
    SINGLE_FILE,
    SOURCE,
    ImppatLookup,
)

_RETRIEVED = "2026-10-06T00:00:00+00:00"
_TAXONOMY_XML = """<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>424242</TaxId>
  <ScientificName>Fakus exemplaris</ScientificName>
  <Rank>species</Rank>
  <OtherNames>
    <Synonym>Fakus antiquus</Synonym>
    <CommonName>fakewort</CommonName>
  </OtherNames>
  <Lineage>cellular organisms; Fictionalia</Lineage>
</Taxon></TaxaSet>
"""

_PLANT_HEADER = (
    "Plant_identifier\tIndian_Medicinal_plant\tSynonymous names\tKingdom\tFamily\tGroup\t"
    "Common_name\tIUCN_Red_List_Category\tSystem_of_Medicine"
)
_POLY_HEADER = (
    "Formulation_identifier\tFormulation name_in_AFI_original\tIngredient name in AFI_original\t"
    "Plant part in AFI_original\tTherapeutic uses (according to The Ayurvedic Formulary of India)\t"
    "Ingredient_name_standardised\tPlant_name_standardised\tPlant_part_standardised\t"
    "Therapeutic_uses_standardised\tIMPPAT_identifiers\tReferences"
)
_SINGLE_HEADER = (
    "Formulation_identifier\tFormulation_name_in_API_original\tIngredient name in API_original\t"
    "Plant part in API_original\tTherapeutic_uses (according to The Ayurvedic Pharmacopoeia of India)\t"
    "Ingredient_name_standardised\tPlant_name_standardised\tPlant_part_standardised\t"
    "Therapeutic_uses_standardised\tIMPPAT_identifiers\tReferences"
)
_PHYTO_HEADER = (
    "Plant_identifier\tIndian_Medicinal_plant\tPlant_part\tIMPPAT_Phytochemical_identifier\tReference_identifier"
)


class _Eutils:
    def search(self, db: str, term: str, *, retmax: int = 5) -> dict:
        ids = {"taxonomy": ["424242"], "pccompound": ["100"]}.get(db, [])
        return {"db": db, "term": term, "count": len(ids), "ids": ids[:retmax], "retrieved_at": _RETRIEVED}

    def summary(self, db: str, ids: list[str]) -> dict:
        return {"db": db, "records": [], "retrieved_at": _RETRIEVED}

    def fetch_text(self, db: str, ids: list[str], *, retmode: str = "xml") -> tuple[str, str]:
        assert db == "taxonomy"
        return _TAXONOMY_XML, _RETRIEVED


class _PubChem:
    def properties(self, cid: int):
        return {
            "cid": cid,
            "title": "Fakeoside",
            "molecular_formula": "C2H6O",
            "molecular_weight": 46.0,
            "xlogp": 0.5,
            "tpsa": 20.0,
            "hbd": 1,
            "hba": 1,
            "rotatable_bonds": 0,
            "inchi_key": "FAKEKEYFAKEKEY-FAKEFAKEFAKE-F",
            "canonical_smiles": "CCO",
            "iupac_name": "fakeoside",
        }, _RETRIEVED

    def synonyms(self, cid: int):
        return ["fakeoside"], _RETRIEVED

    def bioassays(self, cid: int):
        return {
            "status": "ok",
            "total": 0,
            "active": 0,
            "source": "PubChem PUG-REST",
            "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
            "retrieved_at": _RETRIEVED,
            "error": None,
        }, _RETRIEVED


class _Chem:
    def classify(self, *, cid: int | None, inchikey: str | None, smiles: str | None) -> dict:
        return {
            "classyfire": {"status": "unavailable", "error": "not used", "retrieved_at": _RETRIEVED},
            "npclassifier": {"status": "unavailable", "error": "not used", "retrieved_at": _RETRIEVED},
        }


def _service(tmp_path: Path, directory: Path | None) -> EnrichmentService:
    return EnrichmentService(
        eutils=_Eutils(),
        pubchem=_PubChem(),
        chemclass=_Chem(),
        llm=LlmClient(api_key=None),
        store=CandidateStore(tmp_path),
        imppat=ImppatLookup(directory),
    )


def _write(directory: Path, name: str, header: str, rows: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    body = "\n".join([header, *rows]) + "\n"
    (directory / name).write_text(body, encoding="utf-8")


def _matched_cache(directory: Path) -> None:
    _write(
        directory,
        PLANT_FILE,
        _PLANT_HEADER,
        [
            "FAKEPLANT0001\tFakus exemplaris\tFakus antiquus|Oldus fakus\tPlantae\tFakaceae\tMadeup\tFakewort\t\tFictional",
            "FAKEPLANT9999\tOtherus fakeus\tDecoyus plantus\tPlantae\tNopeaceae\tMadeup\tDecoywort\t\tFictional",
        ],
    )
    _write(
        directory,
        POLY_FILE,
        _POLY_HEADER,
        [
            "AFI-FAKE-1\tPhony Taila\tPhonyā\tphony root\timaginary colic\tFakus exemplaris\tFakus exemplaris\tfake root\timaginary colic\tFAKE-TPU-1\tISBN:000-FAKE",
            "AFI-FAKE-2\tOldus Taila\tOldusā\told root\timaginary ache\tOldus fakus\tOldus fakus\tfake rhizome\timaginary ache\tFAKE-TPU-2\tISBN:000-FAKE",
            "AFI-DECOY\tDecoy Churna\tDecoyā\tdecoy leaf\tnothing\tOtherus fakeus\tOtherus fakeus\tdecoy leaf\tnothing\tFAKE-TPU-9\tISBN:000-DECOY",
        ],
    )
    _write(
        directory,
        SINGLE_FILE,
        _SINGLE_HEADER,
        [
            "API-FAKE-3\tFakeyashti\tFakus exemplaris Imaginaris\tphony seed\timaginary cough\tFakus antiquus\tFakus antiquus\tfake seed\timaginary cough\tFAKE-TPU-3\tISBN:000-FAKE",
        ],
    )
    _write(
        directory,
        PHYTO_FILE,
        _PHYTO_HEADER,
        ["FAKEPLANT0001\tFakus exemplaris\tfake root\tFAKE-PHY-1\tFAKE-REF-1"],
    )


def _spec(ingredient_id: str) -> dict:
    return {
        "formulation_id": "F-FAKE-1",
        "product_name": "Fake capsule",
        "dosage_form": "capsule",
        "target_market": "US",
        "servings_per_day": 1,
        "confidence": 0.8,
        "ingredients": [
            {
                "ingredient_id": ingredient_id,
                "botanical_name": "Fakus exemplaris",
                "common_name": "fakewort",
                "part_used": "phony root",
                "quantity_mg": 100,
            }
        ],
    }


def test_ncbi_only_match_passes_when_imppat_dir_is_missing(tmp_path: Path):
    doc = _service(tmp_path, tmp_path / "missing-imppat").propose("fakewort")
    assert doc["status"] == "pending"
    assert doc["taxonomy"]["scientific_name"] == "Fakus exemplaris"
    assert doc["taxonomy"]["synonyms"] == ["Fakus antiquus"]
    imppat = doc["imppat"]
    assert imppat["status"] == "unavailable"
    assert imppat["source"] == SOURCE
    assert imppat["advisory"] is True
    assert imppat["files"] == []
    assert "missing" in imppat["error"]


def test_ncbi_and_imppat_match_includes_ayurvedic_fields(tmp_path: Path):
    cache = tmp_path / "cache"
    _matched_cache(cache)
    doc = _service(tmp_path, cache).propose("fakewort")
    assert doc["status"] == "pending"
    assert doc["taxonomy"]["source"] == "NCBI Taxonomy"
    imppat = doc["imppat"]
    assert imppat["status"] == "matched"
    assert imppat["source"] == SOURCE
    assert imppat["advisory"] is True
    names = {item["name"] for item in imppat["files"]}
    assert names == {PLANT_FILE, POLY_FILE, SINGLE_FILE, PHYTO_FILE}
    assert all(item["retrieved_at"] for item in imppat["files"])
    assert imppat["retrieved_at"]
    assert imppat["plants"][0]["plant_identifier"] == "FAKEPLANT0001"
    assert imppat["family"] == "Fakaceae"
    assert imppat["common_names"] == ["Fakewort"]
    assert "Oldus fakus" in imppat["synonyms"]
    assert "Phonyā" in imppat["sanskrit_names"]
    assert "Fakeyashti" in imppat["sanskrit_names"]
    assert "Decoyā" not in imppat["sanskrit_names"]
    parts = {(part["original"], part["standardized"]) for part in imppat["plant_parts"]}
    assert ("phony root", "fake root") in parts
    assert ("phony seed", "fake seed") in parts
    form_ids = {row["formulation_id"] for row in imppat["formulations"]}
    assert {"AFI-FAKE-1", "AFI-FAKE-2", "API-FAKE-3"} <= form_ids
    assert "AFI-DECOY" not in form_ids
    afi = next(row for row in imppat["formulations"] if row["formulation_id"] == "AFI-FAKE-1")
    assert afi["kind"] == "polyherbal"
    assert afi["formulation_name"] == "Phony Taila"
    assert afi["file"] == POLY_FILE
    assert imppat["phytochemicals"][0]["phytochemical_identifier"] == "FAKE-PHY-1"
    assert imppat["phytochemicals"][0]["file"] == PHYTO_FILE
    assert all(plant["plant_identifier"] != "FAKEPLANT9999" for plant in imppat["plants"])


def test_ncbi_match_with_no_imppat_hit_stays_approvable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERBENZO_REGISTRY_DIR", str(tmp_path))
    cache = tmp_path / "cache"
    _write(
        cache,
        PLANT_FILE,
        _PLANT_HEADER,
        ["FAKEPLANT9999\tOtherus fakeus\tDecoyus plantus\tPlantae\tNopeaceae\tMadeup\tDecoywort\t\tFictional"],
    )
    service = _service(tmp_path, cache)
    doc = service.propose("fakewort")
    assert doc["taxonomy"]["scientific_name"] == "Fakus exemplaris"
    assert doc["imppat"]["status"] == "no_match"
    assert doc["imppat"]["source"] == SOURCE
    assert doc["imppat"]["error"] is None
    assert doc["imppat"]["plants"] == []
    assert doc["imppat"]["formulations"] == []
    approved = service.approve(doc["candidate_id"])
    assert approved["status"] == "approved"
    assert approved["imppat"]["status"] == "no_match"

    client = TestClient(app)
    unknown = client.post("/modernize", json=_spec("HB-NOPE"))
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["error"] == "unknown_ingredient"
    assert unknown.json()["detail"]["unknown_ids"] == ["HB-NOPE"]
    ingredient_id = approved["decision"]["ingredient_id"]
    modernized = client.post("/modernize", json=_spec(ingredient_id))
    assert modernized.status_code == 200, modernized.text


@pytest.mark.parametrize("kind", ["missing", "empty", "corrupt", "header_only"])
def test_missing_empty_and_corrupt_imppat_are_unavailable(tmp_path: Path, kind: str):
    directory = tmp_path / kind
    if kind == "empty":
        directory.mkdir()
    elif kind == "corrupt":
        directory.mkdir()
        (directory / PLANT_FILE).write_bytes(b"\xff\xfe this is not a tsv")
        (directory / POLY_FILE).write_text("not\ta\treal\theader\n", encoding="utf-8")
    elif kind == "header_only":
        _write(directory, PLANT_FILE, _PLANT_HEADER, [])
    doc = _service(tmp_path, directory).propose("fakewort")
    assert doc["status"] == "pending"
    assert doc["taxonomy"]["scientific_name"] == "Fakus exemplaris"
    assert doc["imppat"]["status"] == "unavailable"
    assert doc["imppat"]["advisory"] is True
    assert doc["imppat"]["error"]


def test_ambiguous_imppat_hit_does_not_block_propose(tmp_path: Path):
    cache = tmp_path / "cache"
    _write(
        cache,
        PLANT_FILE,
        _PLANT_HEADER,
        [
            "FAKEPLANT0001\tFakus exemplaris\t\tPlantae\tFakaceae\tMadeup\tFakewort\t\tFictional",
            "FAKEPLANT0002\tFakus antiquus\t\tPlantae\tFakaceae\tMadeup\tOld fakewort\t\tFictional",
        ],
    )
    doc = _service(tmp_path, cache).propose("fakewort")
    assert doc["status"] == "pending"
    assert doc["imppat"]["status"] == "ambiguous"
    assert {row["plant_identifier"] for row in doc["imppat"]["plants"]} == {"FAKEPLANT0001", "FAKEPLANT0002"}


def test_settings_imppat_dir_and_disable_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("HERBENZO_IMPPAT_DIR", raising=False)
    assert get_settings().imppat_dir == repo_root() / "data" / "external" / "imppat" / "cache"
    monkeypatch.setenv("HERBENZO_IMPPAT_DIR", "off")
    assert get_settings().imppat_dir is None
    monkeypatch.setenv("HERBENZO_IMPPAT_DIR", str(tmp_path))
    assert get_settings().imppat_dir == tmp_path


def test_standardized_spelling_is_accepted(tmp_path: Path):
    cache = tmp_path / "cache"
    header = (
        "Formulation_identifier\tFormulation name_in_AFI_original\tIngredient name in AFI_original\t"
        "Plant part in AFI_original\tPlant_name_standardized\tPlant_part_standardized"
    )
    _write(
        cache,
        POLY_FILE,
        header,
        ["AFI-FAKE-7\tPhony Taila\tPhonyā\tphony root\tFakus exemplaris\tfake root"],
    )
    found = ImppatLookup(cache).lookup("Fakus exemplaris", [])
    assert found["status"] == "matched"
    assert found["sanskrit_names"] == ["Phonyā"]
    assert found["plant_parts"][0]["standardized"] == "fake root"
    assert found["plant_parts"][0]["original"] == "phony root"

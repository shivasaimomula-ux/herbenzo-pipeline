"""Offline tests for the clients, evidence store, adjudicator and pipeline.

No network access. Fixtures stand in for retrieved records so the suite is
deterministic and runnable in CI.
"""

from __future__ import annotations

import json

import pytest

from herbenzo.clients.pubmed import Article, _parse_articles
from herbenzo.services.adjudication import AdjudicationService, ReasonCode, Verdict
from herbenzo.services.evidence import EvidenceStore
from herbenzo.services.live_registries import LiveRegistriesClient
from herbenzo.services.registries import UnknownMarker

# ---------------------------------------------------------------------------
# Fixtures — shaped exactly as the PubMed client emits them
# ---------------------------------------------------------------------------

FIXTURES = {
    # Real defect: a crop-fertilizer study cited as botanical safety evidence.
    "fertilizer": Article({
        "pmid": "37257749", "year": "2023", "doi": "10.x/y",
        "title": "Deciphering the fertilizing and disease suppression potential of "
                 "phytofabricated zinc oxide nanoparticles on Brassica juncea",
        "abstract": "Zinc oxide nanoparticles were applied to mustard crop and "
                    "growth parameters were measured in pot trials.",
        "journal": "J Env", "publication_types": ["Journal Article"],
        "mesh_terms": ["Zinc Oxide", "Mustard Plant"], "retracted": False,
    }),
    # Safety word present in the abstract body only — not a safety study.
    "weak_safety": Article({
        "pmid": "99000001", "year": "2024", "doi": "",
        "title": "Plant-derived neuroprotective compounds targeting Parkinson disease",
        "abstract": "Phyllanthus emblica extracts were reviewed. Safety and efficacy "
                    "of botanicals are discussed briefly in cultured cells.",
        "journal": "J Neuro", "publication_types": ["Review"],
        "mesh_terms": ["Parkinson Disease"], "retracted": False,
    }),
    # A genuine safety record — declares itself in the title.
    "real_safety": Article({
        "pmid": "99000002", "year": "2022", "doi": "",
        "title": "Acute and subchronic oral toxicity and tolerability of "
                 "Phyllanthus emblica fruit extract in rats",
        "abstract": "No adverse effects were observed. The NOAEL in rats was "
                    "established at 2000 mg/kg.",
        "journal": "Food Chem Tox", "publication_types": ["Journal Article"],
        "mesh_terms": ["Toxicity Tests"], "retracted": False,
    }),
    "retracted": Article({
        "pmid": "99000003", "year": "2019", "doi": "",
        "title": "Withania somnifera phytosome absorption in volunteers",
        "abstract": "A randomized trial in human volunteers of a phytosome.",
        "journal": "J Retracted", "publication_types": ["Randomized Controlled Trial"],
        "mesh_terms": [], "retracted": True,
    }),
    "in_vitro": Article({
        "pmid": "99000004", "year": "2021", "doi": "",
        "title": "Curcuma aromatica inhibits UVA-induced melanogenesis",
        "abstract": "Tyrosinase activity and melanin content were demonstrated to "
                    "fall in cultured G361 cells exposed to UVA.",
        "journal": "Cell Biol Tox", "publication_types": ["Journal Article"],
        "mesh_terms": ["Melanins"], "retracted": False,
    }),
}


class FakeStore(EvidenceStore):
    """EvidenceStore with the network replaced by the fixture table."""

    def __init__(self, tmp_path):
        self._by_pmid = {a["pmid"]: a for a in FIXTURES.values()}
        self._manifest = {"searches": {}, "records": {}}
        self.manifest_path = tmp_path / "manifest.json"

    def record(self, pmid):
        return self._by_pmid.get(str(pmid))

    def records(self, pmids):
        return {p: self._by_pmid[p] for p in map(str, pmids) if p in self._by_pmid}


@pytest.fixture
def svc(tmp_path):
    return AdjudicationService(FakeStore(tmp_path))


# ---------------------------------------------------------------------------
# Adjudication — the defect cases this service exists to catch
# ---------------------------------------------------------------------------

class TestAdjudication:
    def test_rejects_citation_about_a_different_subject(self, svc):
        """The fertilizer paper cited as Bibhitaki safety evidence."""
        a = svc.adjudicate(
            claim="Bibhitaki is well tolerated with no adverse effects",
            subject="Terminalia bellirica", subject_aliases=("bibhitaki",),
            pmid="37257749", claim_domain="safety")
        assert a.verdict == Verdict.REJECT
        assert a.reason_code == ReasonCode.SUBJECT_ABSENT
        assert a.subject_match is False

    def test_safety_claim_cannot_be_supported_on_abstract_mention_alone(self, svc):
        """A Parkinson's review is not safety evidence, even if it says 'safety'."""
        a = svc.adjudicate(
            claim="Amalaki has an established human safety profile",
            subject="Phyllanthus emblica", subject_aliases=("amalaki",),
            pmid="99000001", claim_domain="safety")
        assert a.verdict == Verdict.PARTIAL
        assert a.reason_code == ReasonCode.SAFETY_SIGNAL_WEAK

    def test_genuine_safety_record_is_supported(self, svc):
        a = svc.adjudicate(
            claim="Amalaki showed no adverse effects in subchronic toxicity testing",
            subject="Phyllanthus emblica", pmid="99000002", claim_domain="safety")
        assert a.verdict == Verdict.SUPPORT
        assert a.evidence_tier == "animal"

    def test_safety_claim_with_no_safety_content_is_rejected(self, svc):
        a = svc.adjudicate(
            claim="Curcuma aromatica is safe and well tolerated",
            subject="Curcuma aromatica", pmid="99000004", claim_domain="safety")
        assert a.verdict == Verdict.REJECT
        assert a.reason_code == ReasonCode.WRONG_CLAIM_DOMAIN

    def test_retracted_source_is_always_rejected(self, svc):
        a = svc.adjudicate(
            claim="Phytosome improves absorption of withanolides",
            subject="Withania somnifera", pmid="99000003")
        assert a.verdict == Verdict.REJECT
        assert a.reason_code == ReasonCode.RETRACTED_SOURCE

    def test_missing_record_is_rejected_not_assumed(self, svc):
        a = svc.adjudicate(claim="anything", subject="anything", pmid="00000000")
        assert a.verdict == Verdict.REJECT
        assert a.reason_code == ReasonCode.RECORD_NOT_FOUND

    def test_in_vitro_study_is_not_labelled_animal(self, svc):
        """Regression: substring matching made 'demonstrated' match 'rat'."""
        a = svc.adjudicate(
            claim="Curcuma aromatica reduces melanin content and tyrosinase activity",
            subject="Curcuma aromatica", pmid="99000004", claim_domain="mechanism")
        assert a.verdict == Verdict.SUPPORT
        assert a.model_system == "in_vitro"
        assert a.evidence_tier == "in_vitro"

    def test_every_verdict_carries_a_reason_and_note(self, svc):
        for pmid in ["37257749", "99000001", "99000002", "99000003", "99000004"]:
            a = svc.adjudicate(claim="test claim about absorption",
                               subject="Phyllanthus emblica", pmid=pmid)
            assert a.reason_code and a.note


# ---------------------------------------------------------------------------
# PubMed XML parsing
# ---------------------------------------------------------------------------

_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation>
<PMID Version="1">12345678</PMID>
<Article><ArticleTitle>A trial of something</ArticleTitle>
<Abstract><AbstractText Label="BACKGROUND">First part.</AbstractText>
<AbstractText Label="RESULTS">Second part.</AbstractText></Abstract>
<Journal><Title>J Test</Title><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
<PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList>
</Article>
<MeshHeadingList><MeshHeading><DescriptorName>Curcumin</DescriptorName></MeshHeading></MeshHeadingList>
</MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1000/xyz</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""


class TestPubMedParsing:
    def test_parses_all_fields(self):
        art = _parse_articles(_XML)[0]
        assert art["pmid"] == "12345678"
        assert art["year"] == "2020"
        assert art["doi"] == "10.1000/xyz"
        assert "First part. Second part." in art["abstract"]
        assert art["mesh_terms"] == ["Curcumin"]
        assert art.evidence_tier == "human_rct"
        assert art.is_retracted is False


# ---------------------------------------------------------------------------
# Registry fallback and pipeline
# ---------------------------------------------------------------------------

def test_offline_registry_refuses_network_for_unknown_marker():
    client = LiveRegistriesClient(allow_network=False)
    with pytest.raises(UnknownMarker):
        client.get_physicochemical_properties("Not A Real Compound")


def test_cached_markers_resolve_without_network():
    client = LiveRegistriesClient(allow_network=False)
    p = client.get_physicochemical_properties("Curcumin")
    assert p.pubchem_cid == 969516


def test_offline_pipeline_runs_and_declares_no_support(tmp_path):
    from herbenzo.pipeline import Pipeline
    pipe = Pipeline(allow_network=False,
                    evidence=FakeStore(tmp_path),
                    registries=LiveRegistriesClient(allow_network=False))
    report = pipe.run({
        "formulation_id": "F-T", "product_name": "T", "dosage_form": "capsule",
        "target_market": "US", "servings_per_day": 1, "confidence": 0.9,
        "ingredients": [{"ingredient_id": "HB-TURM",
                         "botanical_name": "Curcuma longa", "quantity_mg": 500.0}],
    })
    assert report["manifest"]["offline"] is True
    assert report["confidence"]["after_adjudication"] <= 0.9
    assert all(c["verdict"] in ("computed", "unsupported") for c in report["claims"])


def test_confidence_never_rises_through_the_pipeline(tmp_path):
    from herbenzo.pipeline import Pipeline
    pipe = Pipeline(allow_network=False,
                    evidence=FakeStore(tmp_path),
                    registries=LiveRegistriesClient(allow_network=False))
    for inherited in (0.2, 0.5, 0.95):
        rep = pipe.run({
            "formulation_id": "F", "product_name": "P", "dosage_form": "capsule",
            "target_market": "US", "servings_per_day": 1, "confidence": inherited,
            "ingredients": [{"ingredient_id": "HB-BERB",
                             "botanical_name": "Berberis aristata",
                             "quantity_mg": 400.0}],
        })
        c = rep["confidence"]
        assert c["after_modernization"] <= c["inherited_from_A"]
        assert c["after_adjudication"] <= c["after_modernization"]


def test_claim_ids_are_stable_and_unique(tmp_path):
    from herbenzo.pipeline import Pipeline
    spec = {
        "formulation_id": "F-ID", "product_name": "P", "dosage_form": "capsule",
        "target_market": "US", "servings_per_day": 1, "confidence": 0.8,
        "ingredients": [{"ingredient_id": "HB-PIPL",
                         "botanical_name": "Piper longum", "quantity_mg": 100.0}],
    }
    mk = lambda: Pipeline(allow_network=False, evidence=FakeStore(tmp_path),
                          registries=LiveRegistriesClient(allow_network=False)).run(spec)
    a, b = mk(), mk()
    ids_a = [c["claim_id"] for c in a["claims"]]
    assert ids_a == [c["claim_id"] for c in b["claims"]]
    assert len(ids_a) == len(set(ids_a))

"""Parsers for enrichment payloads. No network."""

from __future__ import annotations

from herbenzo.services.enrich_parse import (
    aids_from_payload,
    classyfire_from_entity,
    classyfire_from_pubchem,
    gene_from_summary,
    npclassifier_from_payload,
    properties_from_pubchem_row,
    protein_from_summary,
    pubmed_from_summary,
    strip_llm_numerics,
    taxonomy_from_summary,
    taxonomy_from_xml,
)

_TAXONOMY_XML = """<?xml version="1.0" ?>
<TaxaSet><Taxon>
  <TaxId>126910</TaxId>
  <ScientificName>Withania somnifera</ScientificName>
  <Rank>species</Rank>
  <Division>Plants and Fungi</Division>
  <OtherNames>
    <Synonym>Physalis somnifera</Synonym>
    <CommonName>ashwagandha</CommonName>
  </OtherNames>
  <Lineage>cellular organisms; Eukaryota; Withania</Lineage>
  <LineageEx>
    <Taxon><TaxId>2759</TaxId><ScientificName>Eukaryota</ScientificName><Rank>domain</Rank></Taxon>
  </LineageEx>
</Taxon></TaxaSet>
"""


def test_taxonomy_xml_uses_the_requested_taxon_not_lineage_children():
    parsed = taxonomy_from_xml(_TAXONOMY_XML)
    assert parsed is not None
    assert parsed["tax_id"] == 126910
    assert parsed["scientific_name"] == "Withania somnifera"
    assert parsed["rank"] == "species"
    assert parsed["common_names"] == ["ashwagandha"]
    assert parsed["synonyms"] == ["Physalis somnifera"]
    assert parsed["lineage"].startswith("cellular organisms")


def test_taxonomy_summary_parser():
    parsed = taxonomy_from_summary(
        {
            "uid": "126910",
            "taxid": 126910,
            "scientificname": "Withania somnifera",
            "commonname": "ashwagandha",
            "rank": "species",
            "division": "eudicots",
        }
    )
    assert parsed["scientific_name"] == "Withania somnifera"
    assert parsed["common_names"] == ["ashwagandha"]


def test_pubmed_gene_protein_summaries():
    article = pubmed_from_summary(
        {"uid": "37257749", "title": "Withania review", "source": "J Ethnopharmacol", "pubdate": "2023 Jun"}
    )
    assert article["pmid"] == "37257749"
    assert article["year"] == "2023"
    assert article["journal"] == "J Ethnopharmacol"
    gene = gene_from_summary(
        {
            "uid": "55",
            "name": "CYP3A4",
            "description": "cytochrome",
            "organism": {"scientificname": "Homo sapiens"},
        }
    )
    assert gene["symbol"] == "CYP3A4"
    assert gene["organism"] == "Homo sapiens"
    protein = protein_from_summary(
        {"uid": "1", "accessionversion": "AAA000.1", "title": "hypothetical", "taxname": "Bacopa monnieri", "slen": 120}
    )
    assert protein["accession"] == "AAA000.1"
    assert protein["length"] == 120


def test_pubchem_properties_allow_list_drops_unknown_fields():
    parsed = properties_from_pubchem_row(
        {
            "CID": 100,
            "Title": "Bacoside A",
            "MolecularFormula": "C41H68O13",
            "MolecularWeight": "769.0",
            "XLogP": 1.5,
            "TPSA": 200,
            "HBondDonorCount": 8,
            "HBondAcceptorCount": 13,
            "RotatableBondCount": 10,
            "InChIKey": "InChIKey=AAAAAAAAAAAAAA-AAAAAAAAAA-A",
            "ConnectivitySMILES": "C",
            "llm_xlogp": 99,
            "molecular_weight": 1,
        }
    )
    assert parsed["cid"] == 100
    assert parsed["molecular_weight"] == 769.0
    assert parsed["xlogp"] == 1.5
    assert parsed["inchi_key"] == "AAAAAAAAAAAAAA-AAAAAAAAAA-A"
    assert parsed["canonical_smiles"] == "C"
    assert "llm_xlogp" not in parsed
    assert parsed["molecular_weight"] != 1


def test_classyfire_entity_and_pubchem_hierarchy_and_npclassifier():
    entity = classyfire_from_entity(
        {
            "kingdom": {"name": "Organic compounds", "chemont_id": "CHEMONTID:0000000"},
            "superclass": {"name": "Phenylpropanoids and polyketides", "chemont_id": "CHEMONTID:0000261"},
            "class": {"name": "Diarylheptanoids", "chemont_id": "CHEMONTID:0002650"},
            "subclass": {"name": "Linear diarylheptanoids", "chemont_id": "CHEMONTID:0002651"},
            "direct_parent": {"name": "Curcuminoids", "chemont_id": "CHEMONTID:0000356"},
        }
    )
    assert entity["kingdom"]["name"] == "Organic compounds"
    assert entity["direct_parent"]["name"] == "Curcuminoids"
    hierarchy = classyfire_from_pubchem(
        {
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
                            "Information": {
                                "Name": "Phenylpropanoids and polyketides",
                                "Description": "Superclass",
                                "URL": "http://classyfire.wishartlab.com/tax_nodes/CHEMONTID:0000261",
                            },
                            "Node": {
                                "Information": {"Name": "Diarylheptanoids", "Description": "Class"},
                                "Node": {
                                    "Information": {"Name": "Linear diarylheptanoids", "Description": "Subclass"},
                                    "Node": {
                                        "Information": {"Name": "Curcuminoids", "Description": "Direct parent"}
                                    },
                                },
                            },
                        },
                    },
                }
            }
        }
    )
    assert hierarchy["kingdom"]["name"] == "Organic compounds"
    assert hierarchy["kingdom"]["chemont_id"] == "CHEMONTID:0000000"
    assert hierarchy["class"]["name"] == "Diarylheptanoids"
    assert hierarchy["subclass"]["name"] == "Linear diarylheptanoids"
    assert hierarchy["direct_parent"]["name"] == "Curcuminoids"
    npc = npclassifier_from_payload(
        {
            "class_results": ["Linear diarylheptanoids"],
            "superclass_results": ["Diarylheptanoids"],
            "pathway_results": ["Shikimates and Phenylpropanoids"],
            "isglycoside": False,
        }
    )
    assert npc["pathway"] == ["Shikimates and Phenylpropanoids"]
    assert npc["isglycoside"] is False


def test_empty_classyfire_entity_is_empty_not_an_error():
    assert classyfire_from_entity({"subclass": None, "kingdom": None}) == {}
    assert classyfire_from_pubchem({"Hierarchies": {"Hierarchy": []}}) == {}


def test_aids_parser():
    assert aids_from_payload({"IdentifierList": {"AID": [4, "5"]}}) == [4, 5]
    assert aids_from_payload({}) == []


def test_strip_llm_numerics_keeps_narrative():
    cleaned, found = strip_llm_numerics(
        {
            "narrative": "Use the withanolide marker.",
            "xlogp": 99,
            "molecular_weight": 1,
            "ranking": [{"name": "Withaferin A", "rationale": "literature", "logP": 3.2, "bcs_class": "II"}],
        }
    )
    assert found is True
    assert cleaned["narrative"] == "Use the withanolide marker."
    assert "xlogp" not in cleaned
    assert cleaned["ranking"] == [{"name": "Withaferin A", "rationale": "literature"}]

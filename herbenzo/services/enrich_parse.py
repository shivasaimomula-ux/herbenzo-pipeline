"""Pure parsers for enrichment payloads. No network."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any

__all__ = [
    "LEVELS",
    "aids_from_payload",
    "classyfire_from_entity",
    "classyfire_from_pubchem",
    "gene_from_summary",
    "npclassifier_from_payload",
    "parse_llm_content",
    "properties_from_pubchem_row",
    "protein_from_summary",
    "pubmed_from_summary",
    "strip_llm_numerics",
    "synonyms_from_payload",
    "taxonomy_from_summary",
    "taxonomy_from_xml",
]

LEVELS = ("kingdom", "superclass", "class", "subclass", "direct_parent")

_LLM_NUMERIC_KEYS = {
    "molecular_weight",
    "molecularweight",
    "mw",
    "exact_mass",
    "monoisotopic_mass",
    "xlogp",
    "xlogp3",
    "logp",
    "alogp",
    "tpsa",
    "hbd",
    "hba",
    "pka",
    "pkas",
    "solubility",
    "measured_solubility",
    "measured_solubility_mg_per_ml",
    "bcs",
    "bcs_class",
    "rotatable_bonds",
    "rotatablebondcount",
    "pubchem_cid",
    "cid",
    "heavy_atom_count",
}

_CHEMONT = re.compile(r"CHEMONTID:\d+", re.IGNORECASE)


def taxonomy_from_summary(row: dict[str, Any]) -> dict[str, Any]:
    tax_raw = row.get("taxid") or row.get("uid")
    try:
        tax_id = int(tax_raw)
    except (TypeError, ValueError):
        tax_id = None
    common = _text(row.get("commonname"))
    return {
        "tax_id": tax_id,
        "scientific_name": _text(row.get("scientificname")),
        "rank": _text(row.get("rank")),
        "common_names": [common] if common else [],
        "synonyms": [],
        "lineage": _text(row.get("lineage")),
        "division": _text(row.get("division")),
    }


def taxonomy_from_xml(raw: str | bytes) -> dict[str, Any] | None:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    taxon = root.find("Taxon")
    if taxon is None and root.tag == "Taxon":
        taxon = root
    if taxon is None:
        return None

    def text(tag: str) -> str:
        el = taxon.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    try:
        tax_id = int(text("TaxId"))
    except ValueError:
        return None
    commons: list[str] = []
    for tag in ("GenbankCommonName", "CommonName"):
        for el in taxon.findall(f"./OtherNames/{tag}"):
            if el.text and el.text.strip():
                commons.append(el.text.strip())
    synonyms = [
        el.text.strip()
        for el in taxon.findall("./OtherNames/Synonym")
        if el.text and el.text.strip()
    ]
    return {
        "tax_id": tax_id,
        "scientific_name": text("ScientificName"),
        "rank": text("Rank"),
        "common_names": _unique(commons),
        "synonyms": _unique(synonyms),
        "lineage": text("Lineage"),
        "division": text("Division"),
    }


def pubmed_from_summary(row: dict[str, Any]) -> dict[str, Any]:
    pmid = str(row.get("uid") or row.get("articleids") or "").strip()
    pubdate = _text(row.get("pubdate") or row.get("sortpubdate"))
    year = pubdate[:4] if len(pubdate) >= 4 and pubdate[:4].isdigit() else ""
    return {
        "pmid": pmid,
        "title": _text(row.get("title")),
        "journal": _text(row.get("fulljournalname") or row.get("source")),
        "year": year,
    }


def gene_from_summary(row: dict[str, Any]) -> dict[str, Any]:
    organism = row.get("organism")
    if isinstance(organism, dict):
        organism_name = _text(organism.get("scientificname") or organism.get("commonname"))
    else:
        organism_name = _text(organism)
    return {
        "gene_id": str(row.get("uid") or "").strip(),
        "symbol": _text(row.get("name") or row.get("nomenclaturesymbol")),
        "description": _text(row.get("description")),
        "organism": organism_name,
    }


def protein_from_summary(row: dict[str, Any]) -> dict[str, Any]:
    length = row.get("slen")
    try:
        length_val = int(length) if length is not None and str(length).strip() else None
    except (TypeError, ValueError):
        length_val = None
    return {
        "accession": _text(row.get("accessionversion") or row.get("caption") or row.get("uid")),
        "title": _text(row.get("title")),
        "organism": _text(row.get("taxname")),
        "length": length_val,
    }


def properties_from_pubchem_row(row: dict[str, Any]) -> dict[str, Any]:
    """Allow-list PubChem property fields. Anything else in the payload is dropped."""
    smiles = _text(
        row.get("CanonicalSMILES") or row.get("IsomericSMILES") or row.get("ConnectivitySMILES")
    )
    return {
        "cid": int(row["CID"]) if row.get("CID") is not None else None,
        "title": _text(row.get("Title")) or None,
        "molecular_formula": _text(row.get("MolecularFormula")) or None,
        "molecular_weight": _float_or_none(row.get("MolecularWeight")),
        "xlogp": _float_or_none(row.get("XLogP")),
        "tpsa": _float_or_none(row.get("TPSA")),
        "hbd": _int_or_none(row.get("HBondDonorCount")),
        "hba": _int_or_none(row.get("HBondAcceptorCount")),
        "rotatable_bonds": _int_or_none(row.get("RotatableBondCount")),
        "inchi_key": _inchikey(row.get("InChIKey")),
        "canonical_smiles": smiles or None,
        "iupac_name": _text(row.get("IUPACName")) or None,
    }


def synonyms_from_payload(payload: dict[str, Any]) -> list[str]:
    info = (payload.get("InformationList") or {}).get("Information") or []
    names: list[str] = []
    if isinstance(info, dict):
        info = [info]
    for block in info:
        if not isinstance(block, dict):
            continue
        values = block.get("Synonym") or []
        if isinstance(values, str):
            values = [values]
        for name in values:
            text = _text(name)
            if text:
                names.append(text)
    return _unique(names)


def aids_from_payload(payload: dict[str, Any]) -> list[int]:
    raw = (payload.get("IdentifierList") or {}).get("AID") or []
    if isinstance(raw, int):
        raw = [raw]
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def classyfire_from_entity(payload: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    levels: dict[str, dict[str, str | None]] = {}
    for key in LEVELS:
        node = _chemont_node(payload.get(key))
        if node:
            levels[key] = node
    return levels


def classyfire_from_pubchem(payload: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    """Pull ClassyFire/ChemOnt nodes out of a PUG-REST classification document."""
    hierarchies = (payload.get("Hierarchies") or {}).get("Hierarchy") or []
    if isinstance(hierarchies, dict):
        hierarchies = [hierarchies]
    chosen: dict | None = None
    for item in hierarchies:
        if not isinstance(item, dict):
            continue
        source = _text(item.get("SourceName")).casefold()
        if "classyfire" in source or "chemont" in source:
            chosen = item
            break
    if chosen is None:
        for item in hierarchies:
            if isinstance(item, dict) and "classyfire" in json.dumps(item).casefold():
                chosen = item
                break
    if chosen is None:
        return {}
    described = _levels_from_descriptions(chosen)
    if described:
        return described
    return _levels_from_path(chosen)


def npclassifier_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "pathway": _as_str_list(payload.get("pathway_results")),
        "superclass": _as_str_list(payload.get("superclass_results")),
        "class": _as_str_list(payload.get("class_results")),
        "isglycoside": payload.get("isglycoside") if isinstance(payload.get("isglycoside"), bool) else None,
    }


def parse_llm_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("LLM response JSON must be an object")
    return payload


def strip_llm_numerics(payload: Any) -> tuple[Any, bool]:
    """Drop physicochemical keys. The LLM is not a source of those numbers."""
    found = False

    def walk(obj: Any) -> Any:
        nonlocal found
        if isinstance(obj, dict):
            kept: dict[str, Any] = {}
            for key, value in obj.items():
                normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
                if normalized in _LLM_NUMERIC_KEYS:
                    found = True
                    continue
                kept[key] = walk(value)
            return kept
        if isinstance(obj, list):
            return [walk(item) for item in obj]
        return obj

    return walk(payload), found


def _levels_from_descriptions(hierarchy: dict) -> dict[str, dict[str, str | None]]:
    found: dict[str, dict[str, str | None]] = {}

    def walk(node: Any) -> None:
        nodes = node if isinstance(node, list) else [node]
        for item in nodes:
            if not isinstance(item, dict):
                continue
            info = item.get("Information") or {}
            if isinstance(info, list):
                info = info[0] if info else {}
            if isinstance(info, dict):
                label = _text(info.get("Description")).casefold().replace("_", " ")
                level = _description_to_level(label)
                parsed = _chemont_node(
                    {
                        "name": info.get("Name"),
                        "chemont_id": _chemont_from_url(info.get("URL")),
                        "url": info.get("URL"),
                    }
                )
                if level and parsed and level not in found:
                    found[level] = parsed
            if "Node" in item:
                walk(item.get("Node"))

    walk(hierarchy.get("Node"))
    return found


def _levels_from_path(hierarchy: dict) -> dict[str, dict[str, str | None]]:
    path: list[dict[str, str | None]] = []

    def deepest(node: Any) -> None:
        nodes = node if isinstance(node, list) else [node]
        first = next((item for item in nodes if isinstance(item, dict)), None)
        if first is None:
            return
        info = first.get("Information") or {}
        if isinstance(info, list):
            info = info[0] if info else {}
        parsed = _chemont_node(
            {
                "name": info.get("Name") if isinstance(info, dict) else None,
                "chemont_id": _chemont_from_url(info.get("URL") if isinstance(info, dict) else None),
                "url": info.get("URL") if isinstance(info, dict) else None,
            }
        )
        if parsed:
            path.append(parsed)
        if "Node" in first:
            deepest(first.get("Node"))

    deepest(hierarchy.get("Node"))
    if not path:
        return {}
    mapping = {
        0: "kingdom",
        1: "superclass",
        2: "class",
        3: "subclass",
    }
    levels: dict[str, dict[str, str | None]] = {}
    for index, node in enumerate(path[:4]):
        levels[mapping[index]] = node
    levels["direct_parent"] = path[-1]
    return levels


def _description_to_level(label: str) -> str | None:
    if label in {"kingdom"}:
        return "kingdom"
    if label in {"superclass", "super class"}:
        return "superclass"
    if label in {"class"}:
        return "class"
    if label in {"subclass", "sub class"}:
        return "subclass"
    if label in {"direct parent", "directparent", "parent"}:
        return "direct_parent"
    return None


def _chemont_node(block: Any) -> dict[str, str | None] | None:
    if not isinstance(block, dict):
        return None
    name = _text(block.get("name") or block.get("Name"))
    if not name:
        return None
    chemont = _text(block.get("chemont_id") or block.get("chemontid")) or _chemont_from_url(
        block.get("url") or block.get("URL")
    )
    url = _text(block.get("url") or block.get("URL")) or None
    return {"name": name, "chemont_id": chemont or None, "url": url}


def _chemont_from_url(url: Any) -> str | None:
    match = _CHEMONT.search(_text(url))
    if not match:
        return None
    return match.group(0).upper()


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _inchikey(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    if text.lower().startswith("inchikey="):
        text = text.split("=", 1)[1].strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out

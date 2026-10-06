"""Propose, enrich, and gate ingredient candidates.

Stage B modernization does not call this module. A candidate stays out of
``GET /ingredients`` until a person approves it. Physicochemical numbers are
copied from PubChem property records only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from secrets import token_hex
from typing import Any

from herbenzo.clients.chemclass import ChemicalTaxonomyClient
from herbenzo.clients.eutils import EutilsClient, EutilsError
from herbenzo.clients.llm import LlmClient
from herbenzo.clients.pubchem_lookup import PubChemLookup, PubChemLookupError
from herbenzo.config import get_settings
from herbenzo.services.imppat import ImppatLookup
from herbenzo.services.candidate_store import CandidateStore
from herbenzo.services.enrich_parse import (
    gene_from_summary,
    protein_from_summary,
    pubmed_from_summary,
    strip_llm_numerics,
    taxonomy_from_summary,
    taxonomy_from_xml,
)
from herbenzo.services.overlay import allocate_ingredient_id, save_approved_document
from herbenzo.services.registries import _INGREDIENTS, merged_ingredient_ids

__all__ = ["EnrichmentError", "EnrichmentService", "build_enrichment_service", "summarize_candidate"]

_PHYSCHEM_READY = ("molecular_weight", "tpsa", "hbd", "hba", "rotatable_bonds")


class EnrichmentError(Exception):
    def __init__(self, message: str, *, status_code: int = 422, code: str = "enrichment_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class EnrichmentService:
    def __init__(
        self,
        *,
        eutils: EutilsClient | None = None,
        pubchem: PubChemLookup | None = None,
        chemclass: ChemicalTaxonomyClient | None = None,
        llm: LlmClient | None = None,
        store: CandidateStore | None = None,
        imppat: ImppatLookup | None = None,
    ) -> None:
        self.eutils = eutils if eutils is not None else EutilsClient()
        self.pubchem = pubchem if pubchem is not None else PubChemLookup()
        self.chemclass = chemclass if chemclass is not None else ChemicalTaxonomyClient()
        self.llm = llm if llm is not None else LlmClient()
        self.store = store if store is not None else CandidateStore()
        self.imppat = imppat

    def propose(
        self,
        query: str,
        *,
        part_used: str | None = None,
        max_markers: int = 3,
        max_pmids: int = 5,
    ) -> dict[str, Any]:
        name = (query or "").strip()
        if not name:
            raise EnrichmentError("query is required")
        max_markers = max(1, min(int(max_markers), 8))
        max_pmids = max(0, min(int(max_pmids), 10))

        taxonomy = self._taxonomy(name)
        imppat = self._imppat_context(taxonomy)
        scientific = taxonomy["scientific_name"] or name
        markers = self._markers(scientific, max_markers=max_markers)
        literature = self._literature(scientific, max_pmids=max_pmids)
        genes = self._linked_records("gene", scientific, gene_from_summary, "https://www.ncbi.nlm.nih.gov/gene/")
        proteins = self._linked_records(
            "protein", scientific, protein_from_summary, "https://www.ncbi.nlm.nih.gov/protein/"
        )
        justification = self._justify(taxonomy, markers, literature)
        markers = _apply_ranking(markers, justification)
        now = _now()
        candidate_id = "c" + token_hex(8)
        doc = {
            "schema_version": "enrichment/1",
            "candidate_id": candidate_id,
            "status": "pending",
            "query": name,
            "part_used": (part_used or "").strip() or None,
            "proposed_ingredient_id": self._propose_id(scientific),
            "created_at": now,
            "updated_at": now,
            "numeric_policy": "pubchem_only",
            "llm_numerics_applied": False,
            "taxonomy": taxonomy,
            "imppat": imppat,
            "literature": literature,
            "genes": genes,
            "proteins": proteins,
            "markers": markers,
            "justification": justification,
            "decision": None,
        }
        self.store.save(doc)
        return doc

    def approve(
        self,
        candidate_id: str,
        *,
        marker_name: str | None = None,
        part_used: str | None = None,
        common_name: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        doc = self._require(candidate_id)
        if doc.get("status") != "pending":
            raise EnrichmentError(
                f"candidate {candidate_id} is {doc.get('status')}, not pending",
                status_code=409,
                code="invalid_state",
            )
        marker = _select_marker(doc.get("markers") or [], marker_name)
        if marker is None:
            raise EnrichmentError(
                "candidate has no PubChem-backed marker to approve",
                code="marker_not_ready",
            )
        props = registry_properties(marker)
        taxonomy = doc.get("taxonomy") or {}
        ingredient_id = _claim_ingredient_id(
            doc.get("proposed_ingredient_id"),
            taxonomy.get("scientific_name") or doc.get("query") or "NEW",
        )
        chosen_common = (common_name or "").strip() or _first(taxonomy.get("common_names")) or doc.get("query")
        chosen_part = (part_used or "").strip() or (doc.get("part_used") or "").strip() or "unspecified"
        synonyms = _synonyms_for_registry(doc, chosen_common)
        rationale = _approval_rationale(doc, marker, note)
        approved = {
            "ingredient": {
                "ingredient_id": ingredient_id,
                "botanical_name": taxonomy.get("scientific_name") or doc.get("query"),
                "common_name": chosen_common,
                "sanskrit_name": None,
                "synonyms": synonyms,
                "part_used": chosen_part,
                "markers": [{"marker_name": marker["name"], "rationale": rationale}],
            },
            "properties": {marker["name"]: props},
            "source_candidate_id": candidate_id,
            "approved_at": _now(),
        }
        if ingredient_id in _INGREDIENTS:
            raise EnrichmentError(f"{ingredient_id} is a stock registry id", code="id_collision")
        save_approved_document(approved)
        doc["status"] = "approved"
        doc["updated_at"] = approved["approved_at"]
        doc["decision"] = {
            "action": "approve",
            "at": approved["approved_at"],
            "ingredient_id": ingredient_id,
            "marker_name": marker["name"],
            "reason": (note or "").strip(),
        }
        self.store.save(doc)
        return doc

    def reject(self, candidate_id: str, *, reason: str = "") -> dict[str, Any]:
        doc = self._require(candidate_id)
        if doc.get("status") != "pending":
            raise EnrichmentError(
                f"candidate {candidate_id} is {doc.get('status')}, not pending",
                status_code=409,
                code="invalid_state",
            )
        now = _now()
        doc["status"] = "rejected"
        doc["updated_at"] = now
        doc["decision"] = {
            "action": "reject",
            "at": now,
            "ingredient_id": None,
            "marker_name": None,
            "reason": (reason or "").strip(),
        }
        self.store.save(doc)
        return doc

    def get(self, candidate_id: str) -> dict[str, Any]:
        return self._require(candidate_id)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        return [summarize_candidate(doc) for doc in self.store.list(status=status)]

    def _require(self, candidate_id: str) -> dict[str, Any]:
        try:
            doc = self.store.get(candidate_id)
        except ValueError as exc:
            raise EnrichmentError("candidate id is invalid") from exc
        if doc is None:
            raise EnrichmentError(f"no candidate {candidate_id}", status_code=404, code="not_found")
        return doc

    def _taxonomy(self, query: str) -> dict[str, Any]:
        try:
            found = self.eutils.search("taxonomy", query, retmax=5)
        except EutilsError as exc:
            raise EnrichmentError(f"NCBI Taxonomy search failed: {exc}", code="taxonomy_unresolved") from exc
        if not found["ids"]:
            raise EnrichmentError(
                f"NCBI Taxonomy has no record for {query!r}",
                code="taxonomy_unresolved",
            )
        tax_id = found["ids"][0]
        record: dict[str, Any] | None = None
        retrieved_at = found["retrieved_at"]
        try:
            raw, retrieved_at = self.eutils.fetch_text("taxonomy", [tax_id], retmode="xml")
            record = taxonomy_from_xml(raw)
        except EutilsError:
            record = None
        if not record or not record.get("scientific_name"):
            try:
                summary = self.eutils.summary("taxonomy", [tax_id])
            except EutilsError as exc:
                raise EnrichmentError(
                    f"NCBI Taxonomy has no usable record for {query!r}",
                    code="taxonomy_unresolved",
                ) from exc
            rows = summary["records"]
            record = taxonomy_from_summary(rows[0]) if rows else None
            retrieved_at = summary.get("retrieved_at") or retrieved_at
        if not record or not record.get("scientific_name"):
            raise EnrichmentError(
                f"NCBI Taxonomy has no usable record for {query!r}",
                code="taxonomy_unresolved",
            )
        record["source"] = "NCBI Taxonomy"
        record["url"] = f"https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id={record['tax_id']}"
        record["retrieved_at"] = retrieved_at
        return record

    def _imppat_context(self, taxonomy: dict[str, Any]) -> dict[str, Any]:
        """Ayurvedic context after the binomial is known. Never raises."""
        lookup = self.imppat if self.imppat is not None else ImppatLookup(get_settings().imppat_dir)
        synonyms = taxonomy.get("synonyms") if isinstance(taxonomy.get("synonyms"), list) else []
        return lookup.lookup(str(taxonomy.get("scientific_name") or ""), synonyms)

    def _markers(self, scientific_name: str, *, max_markers: int) -> list[dict[str, Any]]:
        try:
            found = self.eutils.search("pccompound", f'"{scientific_name}"', retmax=max_markers)
        except EutilsError:
            return []
        markers: list[dict[str, Any]] = []
        for cid_text in found["ids"][:max_markers]:
            try:
                cid = int(cid_text)
            except ValueError:
                continue
            try:
                props, retrieved_at = self.pubchem.properties(cid)
            except PubChemLookupError:
                continue
            try:
                synonyms, _syn_at = self.pubchem.synonyms(cid)
            except PubChemLookupError:
                synonyms = []
            try:
                assays, _assay_at = self.pubchem.bioassays(cid)
            except PubChemLookupError:
                assays = {
                    "status": "unavailable",
                    "total": None,
                    "active": None,
                    "source": "PubChem PUG-REST",
                    "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    "retrieved_at": None,
                    "error": "bioassay summary failed",
                }
            chemical = self.chemclass.classify(
                cid=cid,
                inchikey=props.get("inchi_key"),
                smiles=props.get("canonical_smiles"),
            )
            markers.append(_marker(props, synonyms, assays, chemical, retrieved_at))
        return markers

    def _literature(self, scientific_name: str, *, max_pmids: int) -> dict[str, Any]:
        term = f'"{scientific_name}"[Title/Abstract]'
        if max_pmids == 0:
            return _literature_block(term, [], 0, None, status="ok")
        try:
            found = self.eutils.search("pubmed", term, retmax=max_pmids)
            summary = self.eutils.summary("pubmed", found["ids"][:max_pmids])
        except EutilsError as exc:
            return _literature_block(term, [], 0, None, status="unavailable", error=str(exc))
        articles = []
        for row in summary["records"]:
            article = pubmed_from_summary(row)
            if not article["pmid"]:
                continue
            article["url"] = f"https://pubmed.ncbi.nlm.nih.gov/{article['pmid']}/"
            articles.append(article)
        return _literature_block(
            term,
            articles,
            int(found["count"]),
            summary.get("retrieved_at") or found["retrieved_at"],
        )

    def _linked_records(self, db: str, scientific_name: str, parser, url_prefix: str) -> dict[str, Any]:
        term = f"{scientific_name}[Organism]"
        try:
            found = self.eutils.search(db, term, retmax=3)
            summary = self.eutils.summary(db, found["ids"][:3])
        except EutilsError as exc:
            return {
                "status": "unavailable",
                "source": f"NCBI {db}",
                "query": term,
                "records": [],
                "retrieved_at": None,
                "error": str(exc),
            }
        records = []
        for row in summary["records"]:
            item = parser(row)
            ident = item.get("gene_id") or item.get("accession")
            if ident:
                item["url"] = f"{url_prefix}{ident}"
            records.append(item)
        return {
            "status": "ok",
            "source": f"NCBI {db}",
            "query": term,
            "records": records,
            "retrieved_at": summary.get("retrieved_at") or found["retrieved_at"],
            "error": None,
            "note": "Records linked to the organism in NCBI. Not pharmacological targets.",
        }

    def _justify(self, taxonomy: dict, markers: list[dict], literature: dict) -> dict[str, Any]:
        context = {
            "scientific_name": taxonomy.get("scientific_name"),
            "common_names": taxonomy.get("common_names") or [],
            "markers": [marker["name"] for marker in markers],
            "pubmed_titles": [article.get("title") for article in literature.get("articles") or []],
        }
        result = self.llm.justify(context)
        cleaned, stripped = strip_llm_numerics(result)
        if isinstance(cleaned, dict):
            result = cleaned
        result["numerics_ignored"] = bool(result.get("numerics_ignored") or stripped)
        result.setdefault("ignored_names", [])
        return result

    def _propose_id(self, scientific_name: str) -> str:
        return allocate_ingredient_id(scientific_name, self._taken_ids())

    def _taken_ids(self) -> set[str]:
        taken = set(merged_ingredient_ids())
        for doc in self.store.list():
            proposed = doc.get("proposed_ingredient_id")
            if isinstance(proposed, str):
                taken.add(proposed)
            decision = doc.get("decision") or {}
            decided = decision.get("ingredient_id") if isinstance(decision, dict) else None
            if isinstance(decided, str):
                taken.add(decided)
        return taken


def build_enrichment_service() -> EnrichmentService:
    settings = get_settings()
    cache = settings.enrichment_cache_dir
    return EnrichmentService(
        eutils=EutilsClient(
            api_key=settings.ncbi_api_key,
            email=settings.ncbi_email,
            tool=settings.ncbi_tool,
            cache_dir=cache / "eutils",
            min_interval_s=settings.ncbi_min_interval_s,
        ),
        pubchem=PubChemLookup(cache_dir=cache / "pubchem"),
        chemclass=ChemicalTaxonomyClient(cache_dir=cache / "chemclass"),
        llm=LlmClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        ),
        store=CandidateStore(settings.registry_dir),
        imppat=ImppatLookup(settings.imppat_dir),
    )


def summarize_candidate(doc: dict[str, Any]) -> dict[str, Any]:
    taxonomy = doc.get("taxonomy") or {}
    justification = doc.get("justification") or {}
    return {
        "candidate_id": doc.get("candidate_id"),
        "status": doc.get("status"),
        "query": doc.get("query"),
        "scientific_name": taxonomy.get("scientific_name"),
        "imppat_status": (doc.get("imppat") or {}).get("status"),
        "proposed_ingredient_id": doc.get("proposed_ingredient_id"),
        "marker_names": [marker.get("name") for marker in doc.get("markers") or []],
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "justification_status": justification.get("status"),
    }


def registry_properties(marker: dict[str, Any]) -> dict[str, Any]:
    """Copy the Stage B descriptor record from the PubChem block only."""
    if not marker.get("usable_for_approval"):
        raise EnrichmentError(
            f"marker {marker.get('name')!r} has no complete PubChem descriptor record",
            code="marker_not_ready",
        )
    pubchem = marker.get("pubchem") or {}
    cid = pubchem.get("cid")
    return {
        "marker_name": marker["name"],
        "pubchem_cid": int(cid),
        "molecular_weight": float(pubchem["molecular_weight"]),
        "xlogp": None if pubchem.get("xlogp") is None else float(pubchem["xlogp"]),
        "tpsa": float(pubchem["tpsa"]),
        "hbd": int(pubchem["hbd"]),
        "hba": int(pubchem["hba"]),
        "rotatable_bonds": int(pubchem["rotatable_bonds"]),
        "source": pubchem.get("source") or f"PubChem PUG-REST CID {cid} (computed descriptors)",
    }


def _marker(props: dict, synonyms: list[str], assays: dict, chemical: dict, retrieved_at: str) -> dict:
    cid = props.get("cid")
    title = props.get("title") or _preferred_synonym(synonyms) or f"CID {cid}"
    pubchem = {
        "cid": cid,
        "molecular_formula": props.get("molecular_formula"),
        "molecular_weight": props.get("molecular_weight"),
        "xlogp": props.get("xlogp"),
        "tpsa": props.get("tpsa"),
        "hbd": props.get("hbd"),
        "hba": props.get("hba"),
        "rotatable_bonds": props.get("rotatable_bonds"),
        "inchi_key": props.get("inchi_key"),
        "canonical_smiles": props.get("canonical_smiles"),
        "iupac_name": props.get("iupac_name"),
        "source": f"PubChem PUG-REST CID {cid} (computed descriptors)",
        "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
        "retrieved_at": retrieved_at,
    }
    usable = cid is not None and all(pubchem.get(key) is not None for key in _PHYSCHEM_READY)
    extra_names = [name for name in synonyms if name.casefold() != str(title).casefold()]
    return {
        "name": title,
        "synonyms": extra_names[:8],
        "pubchem": pubchem,
        "usable_for_approval": usable,
        "bioassays": assays,
        "chemical_taxonomy": chemical,
        "llm_rationale": None,
    }


def _apply_ranking(markers: list[dict], justification: dict) -> list[dict]:
    ranking = justification.get("ranking") or []
    by_name = {str(marker["name"]).casefold(): marker for marker in markers}
    ordered: list[dict] = []
    used: set[str] = set()
    ignored: list[str] = []
    for item in ranking:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.strip().casefold()
        marker = by_name.get(key)
        if marker is None or key in used:
            ignored.append(name.strip())
            continue
        rationale = item.get("rationale")
        if isinstance(rationale, str) and rationale.strip():
            marker["llm_rationale"] = rationale.strip()
        ordered.append(marker)
        used.add(key)
    for marker in markers:
        if str(marker["name"]).casefold() not in used:
            ordered.append(marker)
    justification["ignored_names"] = ignored
    return ordered


def _select_marker(markers: list[dict], marker_name: str | None) -> dict | None:
    usable = [marker for marker in markers if marker.get("usable_for_approval")]
    if marker_name:
        wanted = marker_name.strip().casefold()
        for marker in markers:
            if str(marker.get("name") or "").casefold() == wanted:
                return marker if marker.get("usable_for_approval") else None
        return None
    return usable[0] if usable else None


def _claim_ingredient_id(proposed: str | None, scientific_name: str) -> str:
    registered = set(merged_ingredient_ids())
    if isinstance(proposed, str) and proposed not in registered:
        return proposed
    return allocate_ingredient_id(scientific_name or "NEW", registered)


def _approval_rationale(doc: dict, marker: dict, note: str | None) -> str:
    pubchem = marker.get("pubchem") or {}
    parts = [
        f"Approved enrichment candidate {doc.get('candidate_id')}.",
        f"Marker {marker.get('name')} from PubChem CID {pubchem.get('cid')}.",
        f"Descriptors retrieved from PubChem PUG-REST ({pubchem.get('retrieved_at') or 'undated'}).",
    ]
    justification = doc.get("justification") or {}
    narrative = justification.get("narrative")
    if justification.get("status") == "ok" and isinstance(narrative, str) and narrative.strip():
        parts.append(narrative.strip()[:500])
    else:
        parts.append("LLM justification was unavailable.")
    if isinstance(marker.get("llm_rationale"), str) and marker["llm_rationale"].strip():
        parts.append(marker["llm_rationale"].strip()[:300])
    if note and note.strip():
        parts.append(note.strip())
    return " ".join(parts)


def _synonyms_for_registry(doc: dict, common_name: str | None) -> list[str]:
    taxonomy = doc.get("taxonomy") or {}
    values: list[str] = []
    for item in list(taxonomy.get("synonyms") or []) + list(taxonomy.get("common_names") or []):
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
    query = doc.get("query")
    if isinstance(query, str) and query.strip():
        values.append(query.strip())
    skip = {str(common_name or "").casefold(), str(taxonomy.get("scientific_name") or "").casefold()}
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        key = item.casefold()
        if key in skip or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:12]


def _literature_block(term, articles, total, retrieved_at, status="ok", error=None) -> dict[str, Any]:
    return {
        "status": status,
        "query": term,
        "total": total,
        "articles": articles,
        "source": "PubMed E-utilities",
        "url": "https://pubmed.ncbi.nlm.nih.gov/",
        "retrieved_at": retrieved_at,
        "error": error,
    }


def _preferred_synonym(synonyms: list[str]) -> str | None:
    for name in synonyms:
        if name and not name[:1].isdigit():
            return name
    return synonyms[0] if synonyms else None


def _first(values: Any) -> str | None:
    if isinstance(values, list) and values:
        text = str(values[0]).strip()
        return text or None
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat()

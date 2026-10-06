"""Live, uncached ingredient research.

Nothing here is written to an ingredient list. Two calls for the same name
each talk to NCBI and PubChem again. A returned document is only an input to
approval on this request.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from herbenzo.clients.chemclass import ChemicalTaxonomyClient
from herbenzo.clients.eutils import EutilsClient, EutilsError
from herbenzo.clients.llm import LlmClient
from herbenzo.clients.pubchem_lookup import PubChemLookup, PubChemLookupError
from herbenzo.config import get_settings
from herbenzo.services.enrich_parse import (
    gene_from_summary,
    protein_from_summary,
    pubmed_from_summary,
    strip_llm_numerics,
    taxonomy_from_summary,
    taxonomy_from_xml,
)
from herbenzo.services.imppat import ImppatLookup, approved_context
from herbenzo.services.llm_config import validate_llm_settings
from herbenzo.services.records import ResearchError
from herbenzo.services.research_extract import (
    constituent_names,
    pathway_mentions,
    strip_unverified_citations,
)

__all__ = [
    "PHYSCHEM_READY",
    "ResearchService",
    "build_research_service",
    "stored_pubmed_count",
]

PHYSCHEM_READY = ("molecular_weight", "tpsa", "hbd", "hba", "rotatable_bonds")
_UNII_TITLE = re.compile(r"^[A-Z0-9]{6,12}$")
_MAX_NAME_LOOKUPS = 6
_MAX_CID_LINKS = 8
_TAXONOMY_NAME_FIELDS = ("Scientific Name", "Common Name", "Synonym")


def _suggest_payload(query: str, suggestions: list[dict[str, Any]], *, error: str | None) -> dict[str, Any]:
    exact = [row for row in suggestions if row.get("exact_scientific_match")]
    ambiguous = len(suggestions) != 1 or not exact
    return {
        "query": query,
        "suggestions": suggestions,
        "ambiguous": ambiguous,
        "auto_selected": None,
        "source": "NCBI Taxonomy",
        "error": error,
    }


def stored_pubmed_count(literature: dict[str, Any] | None) -> int:
    articles = (literature or {}).get("articles") or []
    count = 0
    for article in articles:
        if not isinstance(article, dict):
            continue
        pmid = str(article.get("pmid") or "").strip()
        title = str(article.get("title") or "").strip()
        if pmid and title:
            count += 1
    return count


class ResearchService:
    def __init__(
        self,
        *,
        eutils: EutilsClient | None = None,
        pubchem: PubChemLookup | None = None,
        chemclass: ChemicalTaxonomyClient | None = None,
        llm: LlmClient | None = None,
        imppat: ImppatLookup | None = None,
        min_pubmed_refs: int | None = None,
        web_search_endpoint: str | None = None,
    ) -> None:
        settings = get_settings()
        self.eutils = eutils if eutils is not None else _live_eutils(settings)
        self.pubchem = pubchem if pubchem is not None else _live_pubchem()
        self.chemclass = chemclass if chemclass is not None else ChemicalTaxonomyClient(cache_enabled=False)
        self.llm = llm if llm is not None else LlmClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        )
        self.imppat = imppat if imppat is not None else ImppatLookup(settings.imppat_dir)
        self.min_pubmed_refs = settings.min_pubmed_refs if min_pubmed_refs is None else int(min_pubmed_refs)
        self.web_search_endpoint = (
            settings.web_search_endpoint if web_search_endpoint is None else web_search_endpoint
        )

    def research(
        self,
        query: str,
        *,
        part_used: str | None = None,
        max_markers: int = 8,
        max_pmids: int = 5,
    ) -> dict[str, Any]:
        name = (query or "").strip()
        if not name:
            raise ResearchError("query is required", code="identity_unresolved")
        max_markers = max(1, min(int(max_markers), 8))
        max_pmids = max(0, min(int(max_pmids), 10))
        taxonomy = self.taxonomy_record(name)
        if not taxonomy.get("scientific_name") or taxonomy.get("tax_id") is None:
            raise ResearchError(
                f"NCBI Taxonomy has no usable record for {name!r}",
                code="identity_unresolved",
            )
        scientific = str(taxonomy["scientific_name"])
        imppat = self.imppat.lookup(scientific, list(taxonomy.get("synonyms") or []))
        literature = self.literature_record(scientific, max_pmids=max_pmids)
        texts = _literature_texts(literature)
        constituents = constituent_names(*texts)
        pathways = pathway_mentions(*texts)
        markers, species_search = self.marker_candidates(
            scientific,
            constituents,
            tax_id=_as_int(taxonomy.get("tax_id")),
            max_markers=max_markers,
        )
        selected = _auto_selected(markers)
        genes = self.linked_records(
            "gene", scientific, gene_from_summary, "https://www.ncbi.nlm.nih.gov/gene/"
        )
        proteins = self.linked_records(
            "protein", scientific, protein_from_summary, "https://www.ncbi.nlm.nih.gov/protein/"
        )
        justification = self._advisory_narrative(taxonomy, literature, markers)
        now = _now()
        tax_id = taxonomy["tax_id"]
        return {
            "schema_version": "research/1",
            "status": "pending",
            "query": name,
            "part_used": (part_used or "").strip() or None,
            "ingredient_id": f"tax-{tax_id}",
            "created_at": now,
            "retrieved_at": now,
            "research_path": "deterministic",
            "numeric_policy": "pubchem_only",
            "llm_numerics_applied": False,
            "taxonomy": taxonomy,
            "imppat": imppat,
            "literature": literature,
            "constituents": constituents,
            "pathways": pathways,
            "genes": genes,
            "proteins": proteins,
            "species_search": species_search,
            "markers": markers,
            "selected_marker": selected,
            "marker_status": "resolved" if selected else "pending",
            "justification": justification,
            "ignored_claims": list(justification.get("ignored_claims") or []),
            "evidence": {
                "pubmed_refs": stored_pubmed_count(literature),
                "min_pubmed_refs": self.min_pubmed_refs,
                "meets_threshold": stored_pubmed_count(literature) >= self.min_pubmed_refs
                and literature.get("status") == "ok",
            },
            "web": {"status": "not_requested", "results": []},
        }

    def suggest(self, query: str) -> dict[str, Any]:
        """Live taxonomy suggestions across scientific, common, and synonym names.

        Every matching taxon is returned. Nothing is chosen for the caller.
        """
        text = (query or "").strip()
        if len(text) < 2:
            return _suggest_payload(text, [], error=None)
        ids: list[str] = []
        matched: dict[str, list[str]] = {}
        errors: list[str] = []
        for field in _TAXONOMY_NAME_FIELDS:
            try:
                found = self._search("taxonomy", f"{text}[{field}]", retmax=20)
            except Exception as exc:
                errors.append(str(exc))
                continue
            for tax_id in found.get("ids") or []:
                key = str(tax_id)
                if key not in matched:
                    ids.append(key)
                    matched[key] = []
                if field not in matched[key]:
                    matched[key].append(field)
        suggestions: list[dict[str, Any]] = []
        lookup_error: str | None = None
        for tax_id in ids[:20]:
            try:
                record = self._taxonomy_by_id(str(tax_id), query_label=text)
            except Exception as exc:
                lookup_error = str(exc)
                continue
            if not record or not record.get("scientific_name"):
                continue
            scientific = str(record.get("scientific_name"))
            suggestions.append(
                {
                    "scientific_name": scientific,
                    "common_names": list(record.get("common_names") or []),
                    "synonyms": list(record.get("synonyms") or []),
                    "rank": record.get("rank"),
                    "tax_id": record.get("tax_id"),
                    "matched_fields": list(matched.get(str(tax_id), [])),
                    "exact_scientific_match": scientific.casefold() == text.casefold(),
                }
            )
        error = None if suggestions else (lookup_error or (errors[0] if errors and not ids else None))
        return _suggest_payload(text, suggestions, error=error)

    def approve(
        self,
        candidate: dict[str, Any],
        *,
        marker_name: str | None = None,
        part_used: str | None = None,
        common_name: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            raise ResearchError("candidate must be an object", code="not_approved")
        self._require_identity(candidate)
        self._require_evidence(candidate)
        taxonomy = candidate.get("taxonomy") or {}
        tax_id = _as_int(taxonomy.get("tax_id"))
        attempt: dict[str, Any] | None = None
        selected = candidate.get("selected_marker") if candidate.get("marker_status") == "resolved" else None
        if isinstance(marker_name, str) and marker_name.strip():
            attempt = self.resolve_named_marker(marker_name.strip(), tax_id, strict=False)
            if attempt.get("attached"):
                selected = attempt["marker"]
            else:
                selected = None if candidate.get("marker_status") != "resolved" else selected
                # An unsafe approver name does not fail approval and does not replace
                # a marker that was already unambiguous.
                if not attempt.get("attached"):
                    if candidate.get("marker_status") != "resolved":
                        selected = None
        elif not isinstance(selected, dict):
            selected = None
        return self._approval_document(
            candidate,
            selected=selected if isinstance(selected, dict) else None,
            part_used=part_used,
            common_name=common_name,
            note=note,
            marker_attempt=attempt,
        )

    def set_marker(
        self,
        approval: dict[str, Any],
        marker_name: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(approval, dict) or approval.get("status") != "approved":
            raise ResearchError("set_marker needs an approved research document", code="not_approved")
        name = (marker_name or "").strip()
        if not name:
            raise ResearchError("marker_name is required", code="marker_unverified")
        taxonomy = approval.get("taxonomy") or {}
        resolved = self.resolve_named_marker(name, _as_int(taxonomy.get("tax_id")), strict=True)
        if not resolved.get("attached"):
            reason = resolved.get("reason") or "name did not resolve to one species-linked PubChem record"
            raise ResearchError(
                f"{name!r} was not attached: {reason}. "
                "Use a specific name that PubChem links to this species.",
                code="marker_unverified",
            )
        marker = resolved["marker"]
        updated = dict(approval)
        ingredient = dict(updated.get("ingredient") or {})
        ingredient["marker_status"] = "resolved"
        ingredient["markers"] = [
            {
                "marker_name": marker["name"],
                "rationale": _marker_rationale(marker, updated.get("imppat"), note),
                "efflux_substrate": False,
                "efflux_transporter": None,
                "acid_labile": False,
                "override_requires_citation": False,
                "evidence_note": None,
            }
        ]
        updated["ingredient"] = ingredient
        updated["marker_status"] = "resolved"
        updated["properties"] = {marker["name"]: _registry_properties(marker)}
        updated["selected_marker"] = marker
        audit = list(updated.get("marker_audit") or [])
        audit.append(
            {
                "at": _now(),
                "action": "set_marker",
                "marker_name": marker["name"],
                "pubchem_cid": (marker.get("pubchem") or {}).get("cid"),
                "note": (note or "").strip() or None,
                "organism_link": marker.get("organism_link"),
            }
        )
        updated["marker_audit"] = audit
        return updated

    def taxonomy_record(self, query: str) -> dict[str, Any]:
        try:
            found = self._search("taxonomy", query, retmax=5)
        except EutilsError as exc:
            raise ResearchError(
                f"NCBI Taxonomy search failed: {exc}",
                code="identity_unresolved",
            ) from exc
        if not found.get("ids"):
            raise ResearchError(
                f"NCBI Taxonomy has no record for {query!r}",
                code="identity_unresolved",
            )
        record = self._taxonomy_by_id(str(found["ids"][0]), query_label=query)
        if not record or not record.get("scientific_name"):
            raise ResearchError(
                f"NCBI Taxonomy has no usable record for {query!r}",
                code="identity_unresolved",
            )
        record["source"] = "NCBI Taxonomy"
        record["url"] = f"https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id={record['tax_id']}"
        record.setdefault("retrieved_at", found.get("retrieved_at"))
        return record

    def literature_record(self, scientific_name: str, *, max_pmids: int) -> dict[str, Any]:
        term = (
            f'"{scientific_name}"[Title/Abstract] AND '
            "(pharmacology[Title/Abstract] OR mechanism[Title/Abstract] OR pathway[Title/Abstract] "
            "OR constituents[Title/Abstract] OR phytochemistry[Title/Abstract])"
        )
        if max_pmids == 0:
            return _literature_block(term, [], 0, None)
        try:
            found = self._search("pubmed", term, retmax=max_pmids, sort="relevance")
            summary = self.eutils.summary("pubmed", list(found.get("ids") or [])[:max_pmids])
        except (EutilsError, AttributeError) as exc:
            return _literature_block(term, [], 0, None, status="unavailable", error=str(exc))
        abstracts = self._abstracts(list(found.get("ids") or [])[:max_pmids])
        articles = []
        for row in summary.get("records") or []:
            article = pubmed_from_summary(row)
            if not article.get("pmid") or not article.get("title"):
                continue
            article["url"] = f"https://pubmed.ncbi.nlm.nih.gov/{article['pmid']}/"
            abstract = abstracts.get(str(article["pmid"]))
            if abstract:
                article["abstract"] = abstract
            articles.append(article)
        return _literature_block(
            term,
            articles,
            int(found.get("count") or 0),
            summary.get("retrieved_at") or found.get("retrieved_at"),
            sort="relevance",
        )

    def marker_candidates(
        self,
        scientific_name: str,
        constituents: list[dict[str, str]],
        *,
        tax_id: int | None,
        max_markers: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        markers: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        def add(marker: dict[str, Any] | None) -> None:
            if not marker:
                return
            key = str((marker.get("pubchem") or {}).get("cid") or marker.get("query_name") or marker.get("name"))
            if key in seen_keys:
                return
            seen_keys.add(key)
            markers.append(marker)

        for item in constituents:
            if item.get("kind") == "class":
                # Bare class words such as "ternatin" are resolved so ambiguity is recorded,
                # and they are never auto-selected.
                resolved = self.resolve_named_marker(item["name"], tax_id, strict=False, allow_auto=False)
                add(_candidate_from_resolution(item["name"], resolved, source="literature_name"))
                continue
            if len([row for row in markers if row.get("source") == "literature_name"]) >= _MAX_NAME_LOOKUPS:
                break
            resolved = self.resolve_named_marker(item["name"], tax_id, strict=False, allow_auto=True)
            add(_candidate_from_resolution(item["name"], resolved, source="literature_name"))

        species_search = self._species_compounds(scientific_name, tax_id=tax_id, max_markers=max_markers)
        for marker in species_search.get("markers") or []:
            add(marker)
        return markers, {
            "quoted": species_search.get("quoted"),
            "unquoted": species_search.get("unquoted"),
        }

    def resolve_named_marker(
        self,
        name: str,
        tax_id: int | None,
        *,
        strict: bool,
        allow_auto: bool = False,
    ) -> dict[str, Any]:
        cleaned = " ".join(name.split())
        if not cleaned or not hasattr(self.pubchem, "cids_by_name"):
            return {"attached": False, "reason": "name lookup is unavailable", "cids": [], "marker": None}
        try:
            cids, retrieved_at = self.pubchem.cids_by_name(cleaned)
        except PubChemLookupError as exc:
            return {"attached": False, "reason": str(exc), "cids": [], "marker": None}
        cids = list(cids)[:_MAX_CID_LINKS]
        links = {cid: self._taxonomy_link(cid) for cid in cids}
        judgment = judge_name_cids(cids, links, tax_id)
        judgment["retrieved_at"] = retrieved_at
        if not judgment.get("attach"):
            return {
                "attached": False,
                "reason": judgment.get("reason"),
                "cids": cids,
                "judgment": judgment,
                "marker": None,
            }
        if strict and judgment.get("organism_link") != "linked":
            return {
                "attached": False,
                "reason": "PubChem taxonomy did not link this record to the target species",
                "cids": cids,
                "judgment": judgment,
                "marker": None,
            }
        cid = int(judgment["cid"])
        marker = self._marker_for_cid(
            cid,
            query_name=cleaned,
            organism_link=str(judgment.get("organism_link") or "unavailable"),
            source="name",
            auto_selectable=bool(allow_auto and judgment.get("attach")),
            resolution=str(judgment.get("status") or "unambiguous"),
            candidate_cids=cids,
        )
        if marker is None or not marker.get("usable_for_approval"):
            return {
                "attached": False,
                "reason": "PubChem did not return the required descriptors",
                "cids": cids,
                "judgment": judgment,
                "marker": marker,
            }
        if not allow_auto and not strict:
            # Recorded for evidence, not attached unless the caller asked to attach
            # (approve passes allow_auto False and checks attached itself).
            pass
        marker["auto_selectable"] = bool(allow_auto and marker.get("usable_for_approval"))
        return {"attached": True, "reason": None, "cids": cids, "judgment": judgment, "marker": marker}

    def linked_records(self, db: str, scientific_name: str, parser, url_prefix: str) -> dict[str, Any]:
        term = f"{scientific_name}[Organism]"
        try:
            found = self._search(db, term, retmax=3)
            summary = self.eutils.summary(db, list(found.get("ids") or [])[:3])
        except (EutilsError, AttributeError) as exc:
            return {
                "status": "unavailable",
                "source": f"NCBI {db}",
                "query": term,
                "records": [],
                "retrieved_at": None,
                "error": str(exc),
            }
        records = []
        for row in summary.get("records") or []:
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
            "retrieved_at": summary.get("retrieved_at") or found.get("retrieved_at"),
            "error": None,
            "note": "Records linked to the organism in NCBI. Not pharmacological targets.",
        }

    def web_search(self, query: str, *, allowed_urls: set[str] | None = None) -> dict[str, Any]:
        endpoint = (self.web_search_endpoint or "").strip()
        if not endpoint:
            return {
                "status": "unavailable",
                "query": query,
                "results": [],
                "error": "HERBENZO_WEB_SEARCH_ENDPOINT is not set",
            }
        # The endpoint is pluggable. A missing or failing endpoint does not stop research.
        return {
            "status": "unavailable",
            "query": query,
            "results": [],
            "error": "web search transport is not configured on this client",
            "endpoint": endpoint,
        }

    def _species_compounds(self, scientific_name: str, *, tax_id: int | None, max_markers: int) -> dict[str, Any]:
        quoted_term = f'"{scientific_name}"'
        try:
            quoted = self._search("pccompound", quoted_term, retmax=max_markers)
        except EutilsError as exc:
            quoted = {"count": 0, "ids": [], "term": quoted_term, "error": str(exc), "retrieved_at": None}
        try:
            unquoted = self._search("pccompound", scientific_name, retmax=max_markers)
        except EutilsError as exc:
            unquoted = {
                "count": 0,
                "ids": [],
                "term": scientific_name,
                "error": str(exc),
                "retrieved_at": None,
            }
        ordered: list[int] = []
        for bucket in (quoted.get("ids") or [], unquoted.get("ids") or []):
            for raw in bucket:
                try:
                    cid = int(raw)
                except (TypeError, ValueError):
                    continue
                if cid not in ordered:
                    ordered.append(cid)
        multiple = len(ordered) != 1
        markers: list[dict[str, Any]] = []
        for cid in ordered[:max_markers]:
            link = self._taxonomy_link(cid)
            judgment = judge_name_cids([cid], {cid: link}, tax_id)
            title_probe = None
            marker = self._marker_for_cid(
                cid,
                query_name=scientific_name,
                organism_link=str(judgment.get("organism_link") or "unavailable"),
                source="pccompound",
                auto_selectable=False,
                resolution="candidate",
                candidate_cids=ordered,
            )
            if marker is None:
                continue
            title = str(marker.get("title") or "")
            unii = bool(_UNII_TITLE.fullmatch(title.replace(" ", "")))
            # A species search that returns several records, or a bare UNII title,
            # stays a candidate. One ordinary title can be auto-selected when the
            # organism check allows it.
            selectable = (
                not multiple
                and not unii
                and bool(marker.get("usable_for_approval"))
                and judgment.get("organism_link") in {"linked", "unavailable"}
            )
            marker["auto_selectable"] = selectable
            marker["preferred_name"] = marker.get("name")
            if unii:
                synonym = _preferred_synonym(marker.get("synonyms") or [], title)
                if synonym:
                    marker["preferred_name"] = synonym
                    marker["name"] = synonym
            markers.append(marker)
            del title_probe
        return {
            "quoted": {
                "term": quoted_term,
                "count": int(quoted.get("count") or 0),
                "ids": list(quoted.get("ids") or []),
                "retrieved_at": quoted.get("retrieved_at"),
            },
            "unquoted": {
                "term": scientific_name,
                "count": int(unquoted.get("count") or 0),
                "ids": list(unquoted.get("ids") or []),
                "retrieved_at": unquoted.get("retrieved_at"),
            },
            "markers": markers,
        }

    def _marker_for_cid(
        self,
        cid: int,
        *,
        query_name: str,
        organism_link: str,
        source: str,
        auto_selectable: bool,
        resolution: str,
        candidate_cids: list[int],
    ) -> dict[str, Any] | None:
        try:
            props, retrieved_at = self.pubchem.properties(cid)
        except (PubChemLookupError, AttributeError, KeyError):
            return None
        try:
            synonyms, _syn_at = self.pubchem.synonyms(cid)
        except (PubChemLookupError, AttributeError):
            synonyms = []
        try:
            assays, _assay_at = self.pubchem.bioassays(cid)
        except (PubChemLookupError, AttributeError):
            assays = {
                "status": "unavailable",
                "total": None,
                "active": None,
                "source": "PubChem PUG-REST",
                "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                "retrieved_at": None,
                "error": "bioassay summary failed",
            }
        chemical = {"classyfire": {"status": "unavailable"}, "npclassifier": {"status": "unavailable"}}
        if self.chemclass is not None:
            try:
                chemical = self.chemclass.classify(
                    cid=cid,
                    inchikey=props.get("inchi_key"),
                    smiles=props.get("canonical_smiles"),
                )
            except Exception:
                chemical = {"classyfire": {"status": "unavailable"}, "npclassifier": {"status": "unavailable"}}
        title = props.get("title") or _preferred_synonym(synonyms, "") or f"CID {cid}"
        pubchem = {
            "cid": props.get("cid") or cid,
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
        usable = pubchem.get("cid") is not None and all(pubchem.get(key) is not None for key in PHYSCHEM_READY)
        return {
            "name": title,
            "title": title,
            "query_name": query_name,
            "synonyms": [item for item in synonyms if str(item).casefold() != str(title).casefold()][:8],
            "pubchem": pubchem,
            "usable_for_approval": usable,
            "bioassays": assays,
            "chemical_taxonomy": chemical,
            "organism_link": organism_link,
            "resolution": resolution,
            "auto_selectable": bool(auto_selectable and usable),
            "source": source,
            "candidate_cids": candidate_cids,
            "llm_rationale": None,
        }

    def _taxonomy_link(self, cid: int) -> dict[str, Any]:
        method = getattr(self.pubchem, "taxonomy_links", None)
        if method is None:
            return {"status": "unavailable", "tax_ids": [], "source": "PubChem PUG-View Taxonomy"}
        try:
            return method(cid)
        except Exception:
            return {"status": "unavailable", "tax_ids": [], "source": "PubChem PUG-View Taxonomy"}

    def _taxonomy_by_id(self, tax_id: str, *, query_label: str) -> dict[str, Any] | None:
        record = None
        retrieved_at = None
        try:
            raw, retrieved_at = self._fetch_text("taxonomy", [tax_id], retmode="xml")
            record = taxonomy_from_xml(raw)
        except (EutilsError, AssertionError, TypeError):
            record = None
        if not record or not record.get("scientific_name"):
            try:
                summary = self.eutils.summary("taxonomy", [tax_id])
            except (EutilsError, AttributeError):
                return None
            rows = summary.get("records") or []
            record = taxonomy_from_summary(rows[0]) if rows else None
            retrieved_at = summary.get("retrieved_at") or retrieved_at
        if not record:
            return None
        record["retrieved_at"] = retrieved_at
        if record.get("tax_id") is None:
            try:
                record["tax_id"] = int(tax_id)
            except ValueError:
                return None
        record.setdefault("query", query_label)
        return record

    def _abstracts(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        try:
            raw, _retrieved = self._fetch_text("pubmed", ids, retmode="text", rettype="abstract")
        except (EutilsError, AssertionError, TypeError):
            return {}
        return _abstracts_by_pmid(raw)

    def _search(self, db: str, term: str, *, retmax: int = 5, sort: str | None = None) -> dict[str, Any]:
        search = self.eutils.search
        try:
            if sort:
                return search(db, term, retmax=retmax, sort=sort)
            return search(db, term, retmax=retmax)
        except TypeError:
            return search(db, term, retmax=retmax)

    def _fetch_text(self, db: str, ids: list[str], *, retmode: str, rettype: str | None = None) -> tuple[str, str]:
        fetch = self.eutils.fetch_text
        try:
            if rettype:
                return fetch(db, ids, retmode=retmode, rettype=rettype)
            return fetch(db, ids, retmode=retmode)
        except TypeError:
            return fetch(db, ids, retmode=retmode)

    def _require_identity(self, candidate: dict[str, Any]) -> None:
        taxonomy = candidate.get("taxonomy") if isinstance(candidate.get("taxonomy"), dict) else {}
        rank = str(taxonomy.get("rank") or "").strip().casefold()
        tax_id = taxonomy.get("tax_id")
        name = taxonomy.get("scientific_name")
        if tax_id is None or not name or rank != "species":
            label = name or candidate.get("query") or "this name"
            raise ResearchError(
                f"{label} did not resolve to a species-rank NCBI Taxonomy record",
                code="identity_unresolved",
            )

    def _require_evidence(self, candidate: dict[str, Any]) -> None:
        literature = candidate.get("literature") if isinstance(candidate.get("literature"), dict) else {}
        if literature.get("status") != "ok":
            raise ResearchError(
                "PubMed literature for this species is unavailable, so the evidence gate is not met",
                code="insufficient_evidence",
            )
        found = stored_pubmed_count(literature)
        if found < self.min_pubmed_refs:
            raise ResearchError(
                f"stored PubMed references ({found}) are below HERBENZO_MIN_PUBMED_REFS ({self.min_pubmed_refs})",
                code="insufficient_evidence",
            )

    def _approval_document(
        self,
        candidate: dict[str, Any],
        *,
        selected: dict[str, Any] | None,
        part_used: str | None,
        common_name: str | None,
        note: str | None,
        marker_attempt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        taxonomy = candidate.get("taxonomy") or {}
        imppat = candidate.get("imppat") if isinstance(candidate.get("imppat"), dict) else {}
        copied = approved_context(imppat)
        if copied:
            imppat = {**imppat, **copied}
        ingredient_id = str(candidate.get("ingredient_id") or f"tax-{taxonomy.get('tax_id')}")
        botanical = str(taxonomy.get("scientific_name") or "").strip()
        common = (common_name or "").strip() or _first_text(taxonomy.get("common_names")) or candidate.get("query") or botanical
        part = (part_used or "").strip() or (candidate.get("part_used") or "") or _single_imppat_part(imppat) or "unspecified"
        sanskrit = _sanskrit_name(imppat)
        synonyms = _approval_synonyms(candidate, common, sanskrit, imppat)
        markers: list[dict[str, Any]] = []
        properties: dict[str, Any] = {}
        status = "pending"
        if selected and selected.get("usable_for_approval"):
            status = "resolved"
            rationale = _marker_rationale(selected, imppat, note)
            markers.append(
                {
                    "marker_name": selected["name"],
                    "rationale": rationale,
                    "efflux_substrate": False,
                    "efflux_transporter": None,
                    "acid_labile": False,
                    "override_requires_citation": False,
                    "evidence_note": None,
                }
            )
            properties[selected["name"]] = _registry_properties(selected)
        now = _now()
        literature = candidate.get("literature") or {}
        return {
            "status": "approved",
            "approved_at": now,
            "query": candidate.get("query"),
            "ingredient_id": ingredient_id,
            "ingredient": {
                "ingredient_id": ingredient_id,
                "botanical_name": botanical,
                "common_name": common,
                "sanskrit_name": sanskrit,
                "synonyms": synonyms,
                "part_used": part,
                "marker_status": status,
                "markers": markers,
            },
            "taxonomy": {
                "tax_id": taxonomy.get("tax_id"),
                "scientific_name": botanical,
                "rank": taxonomy.get("rank"),
                "url": taxonomy.get("url"),
                "retrieved_at": taxonomy.get("retrieved_at"),
                "source": taxonomy.get("source") or "NCBI Taxonomy",
            },
            "literature": {
                "status": literature.get("status"),
                "query": literature.get("query"),
                "sort": literature.get("sort"),
                "total": literature.get("total"),
                "articles": [
                    {
                        "pmid": article.get("pmid"),
                        "title": article.get("title"),
                        "year": article.get("year"),
                        "url": article.get("url"),
                    }
                    for article in (literature.get("articles") or [])
                    if isinstance(article, dict)
                ],
                "retrieved_at": literature.get("retrieved_at"),
            },
            "marker_status": status,
            "selected_marker": selected,
            "properties": properties,
            "imppat": imppat,
            "imppat_copied": bool(sanskrit) or imppat.get("status") == "matched",
            "marker_attempt": marker_attempt,
            "marker_audit": [],
            "note": (note or "").strip() or None,
            "research_path": candidate.get("research_path"),
            "retrieved_at": candidate.get("retrieved_at"),
        }

    def _advisory_narrative(self, taxonomy: dict, literature: dict, markers: list[dict]) -> dict[str, Any]:
        settings = get_settings()
        verdict = validate_llm_settings(
            api_key=getattr(self.llm, "api_key", None) or settings.llm_api_key,
            base_url=getattr(self.llm, "base_url", None) or settings.llm_base_url,
            model=getattr(self.llm, "model", None) or settings.llm_model,
        )
        if verdict["status"] != "ok" or not getattr(self.llm, "available", False):
            return {
                "status": "unavailable",
                "narrative": None,
                "ignored_claims": [],
                "numerics_ignored": False,
                "message": verdict.get("message"),
            }
        context = {
            "scientific_name": taxonomy.get("scientific_name"),
            "marker_names": [marker.get("name") for marker in markers],
            "pubmed_titles": [article.get("title") for article in literature.get("articles") or []],
        }
        try:
            result = self.llm.justify(context)
        except Exception as exc:
            return {
                "status": "unavailable",
                "narrative": None,
                "ignored_claims": [],
                "numerics_ignored": False,
                "message": str(exc),
            }
        cleaned, stripped = strip_llm_numerics(result)
        if isinstance(cleaned, dict):
            result = cleaned
        narrative = result.get("narrative") if isinstance(result, dict) else None
        allowed_pmids = {
            str(article.get("pmid"))
            for article in (literature.get("articles") or [])
            if isinstance(article, dict) and article.get("pmid")
        }
        allowed_cids = {
            str((marker.get("pubchem") or {}).get("cid"))
            for marker in markers
            if (marker.get("pubchem") or {}).get("cid") is not None
        }
        allowed_urls = {
            str(article.get("url"))
            for article in (literature.get("articles") or [])
            if isinstance(article, dict) and article.get("url")
        }
        narrative, ignored = strip_unverified_citations(
            narrative if isinstance(narrative, str) else None,
            allowed_pmids=allowed_pmids,
            allowed_cids=allowed_cids,
            allowed_urls=allowed_urls,
        )
        if isinstance(result, dict):
            result["narrative"] = narrative
            result["numerics_ignored"] = bool(result.get("numerics_ignored") or stripped)
            result["ignored_claims"] = ignored
        return result if isinstance(result, dict) else {"status": "unavailable", "narrative": None}


def build_research_service() -> ResearchService:
    settings = get_settings()
    return ResearchService(
        eutils=_live_eutils(settings),
        pubchem=_live_pubchem(),
        chemclass=ChemicalTaxonomyClient(cache_enabled=False),
        llm=LlmClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        ),
        imppat=ImppatLookup(settings.imppat_dir),
        min_pubmed_refs=settings.min_pubmed_refs,
        web_search_endpoint=settings.web_search_endpoint,
    )


def judge_name_cids(
    cids: list[int],
    links: dict[int, dict[str, Any]],
    target_tax_id: int | None,
) -> dict[str, Any]:
    """Decide whether a name lookup is safe to attach.

    Several CIDs are ambiguous unless exactly one of them is linked to the
    target species. A single CID whose taxonomy links name other organisms is
    not attached. A missing taxonomy heading is flagged and may still be used
    outside the strict adjudication path.
    """
    if not cids:
        return {
            "status": "not_found",
            "attach": False,
            "cid": None,
            "organism_link": None,
            "reason": "PubChem returned no CID for this name",
            "cids": [],
        }
    if len(cids) > 1:
        linked = [
            cid
            for cid in cids
            if _link_contains(links.get(cid), target_tax_id)
        ]
        if len(linked) == 1:
            return {
                "status": "unambiguous",
                "attach": True,
                "cid": linked[0],
                "organism_link": "linked",
                "reason": None,
                "cids": list(cids),
            }
        return {
            "status": "ambiguous",
            "attach": False,
            "cid": None,
            "organism_link": "ambiguous",
            "reason": "name matched more than one PubChem record and not exactly one species link",
            "cids": list(cids),
        }
    cid = cids[0]
    link = links.get(cid) or {"status": "unavailable", "tax_ids": []}
    if link.get("status") != "ok":
        return {
            "status": "unambiguous",
            "attach": True,
            "cid": cid,
            "organism_link": "unavailable",
            "reason": None,
            "cids": [cid],
        }
    tax_ids = [int(item) for item in (link.get("tax_ids") or []) if str(item).strip().isdigit() or isinstance(item, int)]
    if target_tax_id is not None and int(target_tax_id) in tax_ids:
        return {
            "status": "unambiguous",
            "attach": True,
            "cid": cid,
            "organism_link": "linked",
            "reason": None,
            "cids": [cid],
        }
    if tax_ids:
        return {
            "status": "not_linked",
            "attach": False,
            "cid": cid,
            "organism_link": "excluded",
            "reason": "PubChem taxonomy links this record to other organisms",
            "cids": [cid],
        }
    return {
        "status": "unambiguous",
        "attach": False,
        "cid": cid,
        "organism_link": "none_reported",
        "reason": "PubChem taxonomy listed no organism for this record",
        "cids": [cid],
    }


def _link_contains(link: dict[str, Any] | None, target_tax_id: int | None) -> bool:
    if not link or link.get("status") != "ok" or target_tax_id is None:
        return False
    tax_ids = []
    for item in link.get("tax_ids") or []:
        try:
            tax_ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return int(target_tax_id) in tax_ids


def _auto_selected(markers: list[dict[str, Any]]) -> dict[str, Any] | None:
    chosen = [marker for marker in markers if marker.get("auto_selectable") and marker.get("usable_for_approval")]
    cids = []
    for marker in chosen:
        cid = (marker.get("pubchem") or {}).get("cid")
        if cid is not None and cid not in cids:
            cids.append(cid)
    if len(cids) != 1:
        return None
    return next(marker for marker in chosen if (marker.get("pubchem") or {}).get("cid") == cids[0])


def _candidate_from_resolution(name: str, resolved: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    marker = resolved.get("marker")
    if isinstance(marker, dict):
        copied = dict(marker)
        copied["source"] = source
        copied["query_name"] = name
        if source == "literature_name" and not copied.get("auto_selectable"):
            copied["auto_selectable"] = False
        return copied
    judgment = resolved.get("judgment") or {}
    if not resolved.get("cids") and judgment.get("status") != "ambiguous":
        return None
    return {
        "name": name,
        "query_name": name,
        "source": source,
        "resolution": judgment.get("status") or "not_found",
        "organism_link": judgment.get("organism_link"),
        "auto_selectable": False,
        "usable_for_approval": False,
        "candidate_cids": list(resolved.get("cids") or []),
        "pubchem": None,
        "reason": resolved.get("reason"),
    }


def _registry_properties(marker: dict[str, Any]) -> dict[str, Any]:
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


def _marker_rationale(marker: dict[str, Any], imppat: dict[str, Any] | None, note: str | None) -> str:
    pubchem = marker.get("pubchem") or {}
    parts = [
        f"Marker {marker.get('name')} from PubChem CID {pubchem.get('cid')}.",
        "Descriptors were read from PubChem. Missing XLogP does not block a marker.",
    ]
    if marker.get("organism_link") == "unavailable":
        parts.append("PubChem taxonomy heading was unavailable for this CID.")
    elif marker.get("organism_link") == "linked":
        parts.append("PubChem taxonomy links this record to the resolved species.")
    if isinstance(imppat, dict) and imppat.get("status") == "matched":
        parts.append(
            "Ayurvedic names and parts from IMPPAT 3.0 "
            "(CC BY-NC-ND 4.0; advisory context, not an approval gate)."
        )
    if note and note.strip():
        parts.append(note.strip())
    return " ".join(parts)


def _approval_synonyms(
    candidate: dict[str, Any],
    common_name: str,
    sanskrit: str | None,
    imppat: dict[str, Any],
) -> list[str]:
    taxonomy = candidate.get("taxonomy") or {}
    values: list[str] = []
    for item in list(taxonomy.get("synonyms") or []) + list(taxonomy.get("common_names") or []):
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
    query = candidate.get("query")
    if isinstance(query, str) and query.strip():
        values.append(query.strip())
    if imppat.get("status") == "matched":
        for item in list(imppat.get("synonyms") or []) + list(imppat.get("common_names") or []) + list(imppat.get("sanskrit_names") or []):
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
    skip = {common_name.casefold(), str(taxonomy.get("scientific_name") or "").casefold()}
    if sanskrit:
        skip.add(sanskrit.casefold())
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        key = item.casefold()
        if key in skip or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:24]


def _sanskrit_name(imppat: dict[str, Any]) -> str | None:
    if imppat.get("status") != "matched":
        return None
    names = [item.strip() for item in imppat.get("sanskrit_names") or [] if isinstance(item, str) and item.strip()]
    return names[0] if names else None


def _single_imppat_part(imppat: dict[str, Any]) -> str | None:
    if imppat.get("status") != "matched":
        return None
    parts = []
    for row in imppat.get("plant_parts") or []:
        if not isinstance(row, dict):
            continue
        label = row.get("standardized") or row.get("original")
        if isinstance(label, str) and label.strip():
            parts.append(label.strip())
    unique = []
    for item in parts:
        if item.casefold() not in {existing.casefold() for existing in unique}:
            unique.append(item)
    if len(unique) == 1:
        return unique[0]
    return None


def _literature_texts(literature: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for article in literature.get("articles") or []:
        if not isinstance(article, dict):
            continue
        if article.get("title"):
            texts.append(str(article["title"]))
        if article.get("abstract"):
            texts.append(str(article["abstract"]))
    return texts


def _literature_block(term, articles, total, retrieved_at, status="ok", error=None, sort="relevance") -> dict[str, Any]:
    return {
        "status": status,
        "query": term,
        "sort": sort,
        "total": total,
        "articles": articles,
        "source": "PubMed E-utilities",
        "retrieved_at": retrieved_at,
        "error": error,
    }


def _abstracts_by_pmid(raw: str) -> dict[str, str]:
    found: dict[str, str] = {}
    if not raw:
        return found
    chunks = re.split(r"\n\s*\n", raw)
    current: list[str] = []
    pmid: str | None = None
    for chunk in chunks:
        match = re.search(r"\bPMID:\s*(\d{5,9})\b", chunk)
        if match:
            if pmid and current:
                found[pmid] = " ".join(current).strip()
            pmid = match.group(1)
            current = [chunk]
        elif pmid:
            current.append(chunk)
    if pmid and current:
        found[pmid] = " ".join(current).strip()
    return found


def _preferred_synonym(synonyms: list[str], title: str) -> str | None:
    for name in synonyms:
        text = " ".join(str(name).split())
        if not text or text.casefold() == title.casefold():
            continue
        if _UNII_TITLE.fullmatch(text.replace(" ", "")):
            continue
        if text.upper() == text and " " not in text:
            continue
        return text
    return None


def _first_text(values: Any) -> str | None:
    if not isinstance(values, list):
        return None
    for item in values:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _live_eutils(settings) -> EutilsClient:
    return EutilsClient(
        api_key=settings.ncbi_api_key,
        email=settings.ncbi_email,
        tool=settings.ncbi_tool,
        min_interval_s=settings.ncbi_min_interval_s,
        cache_enabled=False,
    )


def _live_pubchem() -> PubChemLookup:
    return PubChemLookup(cache_enabled=False, min_interval_s=0.25)

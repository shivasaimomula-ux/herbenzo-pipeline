"""Gemini function-calling front door for ingredient research.

The model may plan tool calls. The candidate is built only from tool results.
Numeric descriptors come from PubChem tool payloads. Invented PMIDs, CIDs, and
URLs in the model's closing text are dropped. If Gemini is misconfigured,
errors, or times out, the deterministic NCBI/PubChem path runs instead.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from herbenzo.clients.ayush_portal import AYUSH_PORTAL_TOOL
from herbenzo.config import get_settings
from herbenzo.services.llm_config import validate_llm_settings
from herbenzo.services.research import ResearchService, apply_name_match, _now
from herbenzo.services.research_extract import strip_unverified_citations
from herbenzo.services.enrich_parse import strip_llm_numerics

__all__ = ["ResearchFrontDoor", "TOOL_DEFINITIONS"]

Completer = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]


def _tool(name: str, description: str, properties: dict[str, str]) -> dict[str, Any]:
    schema_props = {}
    for key, kind in properties.items():
        if kind == "array":
            schema_props[key] = {"type": "array", "items": {"type": "string"}}
        else:
            schema_props[key] = {"type": kind}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": schema_props,
                "required": list(properties),
            },
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool("taxonomy_search", "Search NCBI Taxonomy for a scientific or common name.", {"query": "string"}),
    _tool("pubmed_search", "Search PubMed. Results are relevance-sorted by the service.", {"term": "string"}),
    _tool("pubmed_summary", "Fetch PubMed summaries for PMIDs returned by pubmed_search.", {"pmids": "array"}),
    _tool("pubchem_name", "Resolve one compound name to PubChem CIDs. Do not assume the first CID.", {"name": "string"}),
    _tool("pubchem_properties", "Fetch PubChem physicochemical properties for one CID.", {"cid": "integer"}),
    _tool("pubchem_taxonomy", "Fetch PubChem taxonomy links for one CID.", {"cid": "integer"}),
    _tool("species_compounds", "Search pccompound for a species, quoted and unquoted.", {"scientific_name": "string"}),
    _tool("imppat_lookup", "Advisory IMPPAT context for a scientific name.", {"scientific_name": "string"}),
    _tool("web_search", "Search public web pages for market or monograph presence.", {"query": "string"}),
    _tool("web_fetch", "Fetch one URL that web_search already returned in this session.", {"url": "string"}),
    AYUSH_PORTAL_TOOL,
]


class ResearchFrontDoor:
    def __init__(
        self,
        service: ResearchService | None = None,
        *,
        completer: Completer | None = None,
        max_steps: int | None = None,
        timeout_s: float | None = None,
    ) -> None:
        settings = get_settings()
        self.service = service or ResearchService()
        self.completer = completer
        self.max_steps = settings.research_max_steps if max_steps is None else int(max_steps)
        self.timeout_s = settings.research_timeout_s if timeout_s is None else float(timeout_s)
        self.rounds = 0

    def research(
        self,
        query: str,
        *,
        part_used: str | None = None,
        max_markers: int = 8,
        max_pmids: int = 5,
        name_sources: list[str] | None = None,
        name_query: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        llm = self.service.llm
        verdict = validate_llm_settings(
            api_key=getattr(llm, "api_key", None),
            base_url=getattr(llm, "base_url", None) or settings.llm_base_url,
            model=getattr(llm, "model", None) or settings.llm_model,
        )
        kwargs = {
            "part_used": part_used,
            "max_markers": max_markers,
            "max_pmids": max_pmids,
            "name_sources": name_sources,
            "name_query": name_query,
        }
        if verdict["status"] != "ok":
            doc = self.service.research(query, **kwargs)
            doc["research_path"] = "deterministic" if verdict["status"] == "unconfigured" else "deterministic_fallback"
            doc["llm_config"] = verdict
            return doc
        try:
            doc = self._gemini(query, part_used=part_used, max_markers=max_markers, max_pmids=max_pmids)
        except Exception as exc:
            doc = self.service.research(query, **kwargs)
            doc["research_path"] = "deterministic_fallback"
            doc["llm_error"] = str(exc)
            doc["llm_config"] = verdict
            return doc
        return apply_name_match(doc, name_sources=name_sources, name_query=name_query)

    def _gemini(self, query: str, *, part_used: str | None, max_markers: int, max_pmids: int) -> dict[str, Any]:
        self.rounds = 0
        deadline = time.monotonic() + self.timeout_s
        session = _ToolSession(self.service, max_markers=max_markers, max_pmids=max_pmids)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Research one botanical ingredient by calling tools. "
                    "Do not invent PMIDs, CIDs, taxonomy ids, or URLs. "
                    "Do not state physicochemical numbers; PubChem tools supply those. "
                    "Prefer specific constituent names over a bare class name. "
                    "Call ayush_portal_search for the scientific name when that tool is listed. "
                    "Ayush hits are bibliographic only; do not invent ARP ids, abstracts, or emails."
                ),
            },
            {"role": "user", "content": query},
        ]
        final_text = ""
        truncated = False
        while self.rounds < self.max_steps:
            if time.monotonic() > deadline:
                raise TimeoutError("research tool loop timed out")
            message = self._complete(messages)
            self.rounds += 1
            tool_calls = message.get("tool_calls") or []
            content = message.get("content") or ""
            if isinstance(content, str):
                final_text = content
            if not tool_calls:
                break
            messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
            for call in tool_calls:
                name, arguments, call_id = _parse_call(call)
                result = session.execute(name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result, default=str),
                    }
                )
        else:
            truncated = True
        if self.rounds >= self.max_steps and not final_text:
            truncated = True
        if not session.taxonomy:
            raise RuntimeError("Gemini finished without a taxonomy tool result")
        doc = session.assemble(
            query=query,
            part_used=part_used,
            narrative=final_text,
            rounds=self.rounds,
            truncated=truncated,
            min_pubmed_refs=self.service.min_pubmed_refs,
        )
        if "ayush_portal" not in doc:
            scientific = str((session.taxonomy or {}).get("scientific_name") or query)
            block = self.service.ayush_literature(scientific)
            if block is not None:
                doc["ayush_portal"] = block
        return doc

    def _complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if self.completer is not None:
            return self.completer(messages, TOOL_DEFINITIONS)
        return _openai_complete(self.service.llm, messages, TOOL_DEFINITIONS)


class _ToolSession:
    def __init__(self, service: ResearchService, *, max_markers: int, max_pmids: int) -> None:
        self.service = service
        self.max_markers = max_markers
        self.max_pmids = max_pmids
        self.taxonomy: dict[str, Any] | None = None
        self.literature: dict[str, Any] | None = None
        self.imppat: dict[str, Any] | None = None
        self.markers: list[dict[str, Any]] = []
        self.species_search: dict[str, Any] | None = None
        self.web_results: list[dict[str, Any]] = []
        self.ayush: dict[str, Any] | None = None
        self.allowed_urls: set[str] = set()
        self.pmids: set[str] = set()
        self.cids: set[str] = set()
        self.log: list[dict[str, Any]] = []

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "taxonomy_search":
            record = self.service.taxonomy_record(str(arguments.get("query") or ""))
            self.taxonomy = record
            result = record
        elif name == "pubmed_search":
            scientific = (self.taxonomy or {}).get("scientific_name") or arguments.get("term")
            self.literature = self.service.literature_record(str(scientific), max_pmids=self.max_pmids)
            for article in self.literature.get("articles") or []:
                if article.get("pmid"):
                    self.pmids.add(str(article["pmid"]))
                if article.get("url"):
                    self.allowed_urls.add(str(article["url"]))
            result = self.literature
        elif name == "pubmed_summary":
            result = self.literature or {"articles": [], "note": "call pubmed_search first"}
        elif name == "pubchem_name":
            tax_id = (self.taxonomy or {}).get("tax_id")
            resolved = self.service.resolve_named_marker(
                str(arguments.get("name") or ""),
                int(tax_id) if str(tax_id).isdigit() or isinstance(tax_id, int) else None,
                strict=False,
                allow_auto=True,
            )
            marker = resolved.get("marker")
            if isinstance(marker, dict):
                self._remember_marker(marker)
            for cid in resolved.get("cids") or []:
                self.cids.add(str(cid))
            result = {
                "attached_candidate": bool(marker and marker.get("auto_selectable")),
                "cids": resolved.get("cids") or [],
                "reason": resolved.get("reason"),
                "judgment": resolved.get("judgment"),
                "pubchem": (marker or {}).get("pubchem") if isinstance(marker, dict) else None,
            }
        elif name == "pubchem_properties":
            cid = int(arguments.get("cid"))
            self.cids.add(str(cid))
            props, retrieved_at = self.service.pubchem.properties(cid)
            result = {"properties": props, "retrieved_at": retrieved_at}
        elif name == "pubchem_taxonomy":
            cid = int(arguments.get("cid"))
            result = self.service._taxonomy_link(cid)
        elif name == "species_compounds":
            scientific = str(arguments.get("scientific_name") or (self.taxonomy or {}).get("scientific_name") or "")
            tax_raw = (self.taxonomy or {}).get("tax_id")
            tax_id = int(tax_raw) if isinstance(tax_raw, int) or str(tax_raw).isdigit() else None
            packed = self.service._species_compounds(scientific, tax_id=tax_id, max_markers=self.max_markers)
            self.species_search = {"quoted": packed.get("quoted"), "unquoted": packed.get("unquoted")}
            for marker in packed.get("markers") or []:
                self._remember_marker(marker)
            result = self.species_search
        elif name == "imppat_lookup":
            scientific = str(arguments.get("scientific_name") or (self.taxonomy or {}).get("scientific_name") or "")
            synonyms = list((self.taxonomy or {}).get("synonyms") or [])
            self.imppat = self.service.imppat.lookup(scientific, synonyms)
            result = {"status": self.imppat.get("status"), "advisory": True}
        elif name == "web_search":
            payload = _web_search(self.service, str(arguments.get("query") or ""))
            for row in payload.get("results") or []:
                url = row.get("url")
                if isinstance(url, str):
                    self.allowed_urls.add(url)
            self.web_results.extend(payload.get("results") or [])
            result = payload
        elif name == "web_fetch":
            url = str(arguments.get("url") or "")
            if url not in self.allowed_urls:
                result = {"status": "rejected", "error": "url was not returned by web_search in this session"}
            else:
                result = {"status": "ok", "url": url}
        elif name == "ayush_portal_search":
            scientific = str(arguments.get("query") or (self.taxonomy or {}).get("scientific_name") or "")
            limit = arguments.get("limit")
            full = self.service.ayush_literature(
                scientific,
                system=str(arguments.get("system") or "any"),
                category=str(arguments.get("category") or "any"),
                limit=limit if isinstance(limit, int) else None,
            )
            if full is None:
                result = {
                    "status": "disabled",
                    "reason": "HERBENZO_AYUSH_PORTAL_ENABLED is false",
                    "hits": [],
                }
            else:
                self.ayush = full
                for record in list(full.get("records") or []) + list(full.get("hits") or []):
                    if isinstance(record, dict) and record.get("record_url"):
                        self.allowed_urls.add(str(record["record_url"]))
                status = full.get("status")
                if status != "ok":
                    result = {
                        "status": status or "unavailable",
                        "reason": full.get("reason") or "unavailable",
                        "hits": [],
                    }
                else:
                    result = {"status": "ok", "hits": list(full.get("hits") or [])}
        else:
            result = {"status": "ignored", "error": f"unknown tool {name}"}
        self.log.append({"tool": name, "arguments": arguments})
        return result

    def _remember_marker(self, marker: dict[str, Any]) -> None:
        cid = (marker.get("pubchem") or {}).get("cid")
        if cid is not None:
            self.cids.add(str(cid))
            if any((row.get("pubchem") or {}).get("cid") == cid for row in self.markers):
                return
        self.markers.append(marker)

    def assemble(
        self,
        *,
        query: str,
        part_used: str | None,
        narrative: str | None,
        rounds: int,
        truncated: bool,
        min_pubmed_refs: int,
    ) -> dict[str, Any]:
        from herbenzo.services.research import _auto_selected, stored_pubmed_count
        from herbenzo.services.research_extract import constituent_names, pathway_mentions

        taxonomy = dict(self.taxonomy or {})
        literature = self.literature or {
            "status": "unavailable",
            "articles": [],
            "total": 0,
            "query": None,
            "sort": "relevance",
        }
        texts = []
        for article in literature.get("articles") or []:
            if isinstance(article, dict):
                if article.get("title"):
                    texts.append(str(article["title"]))
                if article.get("abstract"):
                    texts.append(str(article["abstract"]))
        cleaned, numerics_ignored = strip_llm_numerics({"narrative": narrative or ""})
        narrative_text = cleaned.get("narrative") if isinstance(cleaned, dict) else None
        narrative_text, ignored = strip_unverified_citations(
            narrative_text if isinstance(narrative_text, str) else None,
            allowed_pmids=set(self.pmids),
            allowed_cids=set(self.cids),
            allowed_urls=set(self.allowed_urls),
        )
        selected = _auto_selected(self.markers)
        tax_id = taxonomy.get("tax_id")
        now = _now()
        doc = {
            "schema_version": "research/1",
            "status": "pending",
            "query": query,
            "part_used": (part_used or "").strip() or None,
            "ingredient_id": f"tax-{tax_id}",
            "created_at": now,
            "retrieved_at": now,
            "research_path": "gemini",
            "numeric_policy": "pubchem_only",
            "llm_numerics_applied": False,
            "taxonomy": taxonomy,
            "imppat": self.imppat or {"status": "unavailable", "advisory": True},
            "literature": literature,
            "constituents": constituent_names(*texts),
            "pathways": pathway_mentions(*texts),
            "genes": {"status": "not_requested", "records": []},
            "proteins": {"status": "not_requested", "records": []},
            "species_search": self.species_search,
            "markers": self.markers,
            "selected_marker": selected,
            "marker_status": "resolved" if selected else "pending",
            "justification": {
                "status": "ok" if narrative_text else "unavailable",
                "narrative": narrative_text,
                "numerics_ignored": numerics_ignored,
                "ignored_claims": ignored,
            },
            "ignored_claims": ignored,
            "evidence": {
                "pubmed_refs": stored_pubmed_count(literature),
                "min_pubmed_refs": min_pubmed_refs,
                "meets_threshold": stored_pubmed_count(literature) >= min_pubmed_refs
                and literature.get("status") == "ok",
            },
            "web": {"status": "ok" if self.web_results else "unavailable", "results": self.web_results},
            "tool_rounds": rounds,
            "truncated": truncated,
            "tool_log": self.log,
        }
        if isinstance(self.ayush, dict):
            doc["ayush_portal"] = self.ayush
        return doc


def _parse_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    name = str((function or {}).get("name") or call.get("name") or "")
    raw = (function or {}).get("arguments") if isinstance(function, dict) else call.get("arguments")
    if isinstance(raw, str):
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            arguments = {}
    elif isinstance(raw, dict):
        arguments = raw
    else:
        arguments = {}
    call_id = str(call.get("id") or name)
    return name, arguments, call_id


def _openai_complete(llm, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    url = f"{llm.base_url}/chat/completions"
    body = json.dumps(
        {"model": llm.model, "temperature": 0, "messages": messages, "tools": tools}
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {llm.api_key}",
        "Content-Type": "application/json",
        "User-Agent": "herbenzo-pipeline/1.0",
    }
    transport = getattr(llm, "transport", None)
    timeout_s = float(getattr(llm, "timeout_s", 45.0))
    if transport is not None:
        status, raw = transport(url, body, headers, timeout_s)
    else:
        status, raw = _post(url, body, headers, timeout_s)
    if status >= 400:
        raise RuntimeError(f"LLM HTTP {status}")
    payload = json.loads(raw.decode("utf-8"))
    return payload["choices"][0]["message"]


def _post(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        data = exc.read() if exc.fp is not None else b""
        return int(exc.code), data


def _web_search(service: ResearchService, query: str) -> dict[str, Any]:
    endpoint = (service.web_search_endpoint or "").strip()
    if not endpoint:
        return {
            "status": "unavailable",
            "query": query,
            "results": [],
            "error": "HERBENZO_WEB_SEARCH_ENDPOINT is not set",
        }
    url = endpoint
    if "?" in endpoint:
        url = f"{endpoint}&q={urllib.parse.quote(query)}"
    else:
        url = f"{endpoint}?q={urllib.parse.quote(query)}"
    transport = getattr(service, "web_transport", None)
    try:
        if transport is not None:
            status, raw = transport(url)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "herbenzo-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, raw = int(resp.status), resp.read()
    except Exception as exc:
        return {"status": "unavailable", "query": query, "results": [], "error": str(exc)}
    if status >= 400:
        return {"status": "unavailable", "query": query, "results": [], "error": f"HTTP {status}"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "unavailable", "query": query, "results": [], "error": "web search was not JSON"}
    rows = payload.get("results") if isinstance(payload, dict) else None
    results = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("url"), str):
                results.append({"title": row.get("title"), "url": row["url"]})
    return {"status": "ok", "query": query, "results": results, "error": None}

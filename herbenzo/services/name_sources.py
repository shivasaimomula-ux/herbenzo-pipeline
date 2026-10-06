"""Live common-name lookups. Nothing from these calls is cached.

GBIF vernacular names and Wikidata taxon names are candidates only. The
research service still has to match each binomial to an NCBI species before
it can be selected. A configured LLM may add more binomials; those are held
to the same NCBI check.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

__all__ = [
    "NameSourceError",
    "NameSources",
    "QuietNameSources",
    "SOURCE_GBIF",
    "SOURCE_GEMINI",
    "SOURCE_NCBI_COMMON",
    "SOURCE_NCBI_SCIENTIFIC",
    "SOURCE_NCBI_SYNONYM",
    "SOURCE_WIKIDATA",
    "clean_binomial",
    "phrase_match",
]

SOURCE_NCBI_SCIENTIFIC = "NCBI scientific name"
SOURCE_NCBI_COMMON = "NCBI common name"
SOURCE_NCBI_SYNONYM = "NCBI synonym"
SOURCE_GBIF = "GBIF vernacular"
SOURCE_WIKIDATA = "Wikidata"
SOURCE_GEMINI = "Gemini web research"

_WD_LANGS = ("en", "hi", "sa", "te")
_GBIF_SEARCH = "https://api.gbif.org/v1/species/search"
_WD_API = "https://www.wikidata.org/w/api.php"
_SPARQL = "https://query.wikidata.org/sparql"

Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class NameSourceError(RuntimeError):
    """One common-name host failed. Other hosts can still be used."""


class QuietNameSources:
    """No network. Used when a caller did not ask for live common-name hosts."""

    def collect(self, query: str) -> dict[str, Any]:
        return {"candidates": [], "notes": []}


class NameSources:
    def __init__(
        self,
        *,
        llm: Any | None = None,
        transport: Transport | None = None,
        timeout_s: float = 8.0,
        sparql_timeout_s: float = 6.0,
        min_interval_s: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str = "herbenzo-pipeline/1.0 (common-name lookup)",
    ) -> None:
        self.llm = llm
        self.transport = transport or _urllib_get
        self.timeout_s = float(timeout_s)
        self.sparql_timeout_s = float(sparql_timeout_s)
        self.min_interval_s = float(min_interval_s)
        self._sleep = sleep
        self.user_agent = user_agent
        self._lock = threading.Lock()
        self._last_call = 0.0

    def collect(self, query: str) -> dict[str, Any]:
        """Return binomial candidates and degradation notes. Never raises."""
        text = " ".join((query or "").split())
        if len(text) < 2:
            return {"candidates": [], "notes": []}
        jobs = {
            "GBIF": self._gbif,
            "Wikidata": self._wikidata,
            "Gemini": self._gemini,
        }
        candidates: list[dict[str, Any]] = []
        notes: list[str] = []
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(fn, text) for name, fn in jobs.items()}
            for name, future in futures.items():
                try:
                    rows, note = future.result()
                except Exception as exc:
                    notes.append(
                        f"{name} is unavailable ({_short(exc)}); other sources are still listed."
                    )
                    continue
                candidates.extend(rows or [])
                if note:
                    notes.append(note)
        return {"candidates": candidates, "notes": notes}

    def _gbif(self, query: str) -> tuple[list[dict[str, Any]], str | None]:
        params = {
            "q": query,
            "qField": "VERNACULAR",
            "rank": "SPECIES",
            "status": "ACCEPTED",
            "limit": 100,
        }
        url = f"{_GBIF_SEARCH}?{urllib.parse.urlencode(params)}"
        try:
            payload = self._get_json(url, timeout=self.timeout_s)
        except Exception as exc:
            return [], (
                f"GBIF vernacular names are unavailable ({_short(exc)}); "
                "other sources are still listed."
            )
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in (payload.get("results") or []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or item.get("synonym") is True:
                continue
            rank = str(item.get("rank") or "SPECIES").upper()
            if rank != "SPECIES":
                continue
            verns = [
                str(vern.get("vernacularName"))
                for vern in (item.get("vernacularNames") or [])
                if isinstance(vern, dict) and vern.get("vernacularName")
            ]
            if not any(phrase_match(vern, query) for vern in verns):
                continue
            binomial = clean_binomial(item.get("canonicalName") or item.get("scientificName") or item.get("species"))
            if not binomial or binomial.casefold() in seen:
                continue
            seen.add(binomial.casefold())
            rows.append({"binomial": binomial, "sources": [SOURCE_GBIF], "ncbi_tax_id": None})
        return rows, None

    def _wikidata(self, query: str) -> tuple[list[dict[str, Any]], str | None]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            entities = pool.submit(self._wd_entities, query)
            sparql = pool.submit(self._wd_sparql, query)
            entity_rows, entity_note = entities.result()
            sparql_rows, sparql_note = sparql.result()
        if entity_note and sparql_note:
            return [], "Wikidata is unavailable; other sources are still listed."
        note = entity_note or sparql_note
        return list(entity_rows) + list(sparql_rows), note

    def _wd_entities(self, query: str) -> tuple[list[dict[str, Any]], str | None]:
        ids: list[str] = []
        try:
            for lang in _WD_LANGS:
                params = {
                    "action": "wbsearchentities",
                    "search": query,
                    "language": lang,
                    "type": "item",
                    "format": "json",
                    "limit": 8,
                }
                payload = self._get_json(f"{_WD_API}?{urllib.parse.urlencode(params)}", timeout=self.timeout_s)
                for hit in (payload.get("search") or []) if isinstance(payload, dict) else []:
                    qid = hit.get("id") if isinstance(hit, dict) else None
                    if isinstance(qid, str) and qid.startswith("Q") and qid not in ids:
                        ids.append(qid)
        except Exception as exc:
            return [], f"Wikidata is unavailable ({_short(exc)}); other sources are still listed."
        if not ids:
            return [], None
        rows: list[dict[str, Any]] = []
        try:
            params = {
                "action": "wbgetentities",
                "ids": "|".join(ids[:50]),
                "props": "labels|aliases|claims",
                "languages": "|".join(_WD_LANGS),
                "format": "json",
            }
            payload = self._get_json(f"{_WD_API}?{urllib.parse.urlencode(params)}", timeout=self.timeout_s)
        except Exception as exc:
            return [], f"Wikidata is unavailable ({_short(exc)}); other sources are still listed."
        entities = payload.get("entities") if isinstance(payload, dict) else None
        for entity in (entities or {}).values():
            if isinstance(entity, dict):
                row = _wikidata_row(entity, query)
                if row:
                    rows.append(row)
        return rows, None

    def _wd_sparql(self, query: str) -> tuple[list[dict[str, Any]], str | None]:
        if len(query) < 4:
            return [], None
        sparql = _sparql(query)
        url = f"{_SPARQL}?{urllib.parse.urlencode({'query': sparql, 'format': 'json'})}"
        try:
            payload = self._get_json(url, timeout=self.sparql_timeout_s)
        except Exception as exc:
            return [], (
                f"Wikidata SPARQL timed out or failed ({_short(exc)}); "
                "other Wikidata matches are still listed."
            )
        rows: list[dict[str, Any]] = []
        bindings = ((payload.get("results") or {}).get("bindings") or []) if isinstance(payload, dict) else []
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            name = _sparql_value(binding.get("name"))
            if not name or not phrase_match(name, query):
                continue
            binomial = clean_binomial(_sparql_value(binding.get("sci")))
            if not binomial:
                continue
            rows.append(
                {
                    "binomial": binomial,
                    "sources": [SOURCE_WIKIDATA],
                    "ncbi_tax_id": _as_int(_sparql_value(binding.get("ncbi"))),
                }
            )
        return rows, None

    def _gemini(self, query: str) -> tuple[list[dict[str, Any]], str | None]:
        hints = getattr(self.llm, "binomial_hints", None)
        if hints is None:
            return [], None
        try:
            names = hints(query)
        except Exception as exc:
            return [], (
                f"Gemini web research is unavailable ({_short(exc)}); "
                "other sources are still listed."
            )
        if not names:
            return [], None
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in names:
            binomial = clean_binomial(str(name))
            if not binomial or binomial.casefold() in seen:
                continue
            seen.add(binomial.casefold())
            rows.append({"binomial": binomial, "sources": [SOURCE_GEMINI], "ncbi_tax_id": None})
        return rows, None

    def _get_json(self, url: str, *, timeout: float) -> Any:
        self._throttle()
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/sparql-results+json, application/json",
        }
        try:
            status, raw = self.transport(url, headers, timeout)
        except urllib.error.URLError as exc:
            raise NameSourceError(str(getattr(exc, "reason", exc))) from exc
        if status >= 400 or status == 0:
            raise NameSourceError(f"HTTP {status}")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NameSourceError("response was not JSON") from exc

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()


def clean_binomial(name: str | None) -> str | None:
    """Genus plus species epithet, without an author citation or rank tag."""
    if not name:
        return None
    tokens = re.findall(r"[A-Za-z][A-Za-z-]*", str(name).replace("×", " "))
    if len(tokens) < 2:
        return None
    genus, epithet = tokens[0], tokens[1]
    if not (genus[:1].isupper() and epithet[:1].islower()):
        return None
    if epithet in {"var", "subsp", "ssp", "f"}:
        return None
    return f"{genus} {epithet}"


def phrase_match(text: str, query: str) -> bool:
    """True when the query is the whole vernacular phrase, not a loose substring."""
    left = _fold_phrase(text)
    right = _fold_phrase(query)
    if len(right) < 2 or not left:
        return False
    return f" {right} " in f" {left} "


def _fold_phrase(text: str) -> str:
    lowered = str(text).casefold().replace("-", " ")
    lowered = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return " ".join(lowered.split())


def _wikidata_row(entity: dict[str, Any], query: str) -> dict[str, Any] | None:
    binomials = [item for item in (clean_binomial(value) for value in _claim_strings(entity, "P225")) if item]
    if not binomials:
        return None
    names = _entity_names(entity)
    if not any(phrase_match(name, query) for name in names):
        return None
    ncbi_ids = [_as_int(value) for value in _claim_strings(entity, "P685")]
    ncbi = next((item for item in ncbi_ids if item), None)
    return {"binomial": binomials[0], "sources": [SOURCE_WIKIDATA], "ncbi_tax_id": ncbi}


def _entity_names(entity: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for block in ("labels", "aliases"):
        for lang, value in (entity.get(block) or {}).items():
            if lang not in _WD_LANGS:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict) and item.get("value"):
                    names.append(str(item["value"]))
    for claim in (entity.get("claims") or {}).get("P1843") or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(value, dict) and value.get("language") in _WD_LANGS and value.get("text"):
            names.append(str(value["text"]))
    return names


def _claim_strings(entity: dict[str, Any], pid: str) -> list[str]:
    found: list[str] = []
    for claim in (entity.get("claims") or {}).get(pid) or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, dict) and value.get("text"):
            found.append(str(value["text"]).strip())
    return found


def _sparql(query: str) -> str:
    safe = json.dumps(_fold_phrase(query).replace('"', "")[:80])
    langs = ", ".join(f'"{lang}"' for lang in _WD_LANGS)
    return (
        "SELECT ?sci ?ncbi ?name WHERE {\n"
        "  { ?item wdt:P1843 ?name . }\n"
        "  UNION { ?item rdfs:label ?name . }\n"
        "  UNION { ?item skos:altLabel ?name . }\n"
        f"  FILTER(LANG(?name) IN ({langs}))\n"
        f"  FILTER(CONTAINS(LCASE(STR(?name)), {safe}))\n"
        "  ?item wdt:P225 ?sci .\n"
        "  OPTIONAL { ?item wdt:P685 ?ncbi . }\n"
        "} LIMIT 20"
    )


def _sparql_value(node: Any) -> str | None:
    if isinstance(node, dict) and node.get("value"):
        return str(node["value"])
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:180]


def _urllib_get(url: str, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        return int(exc.code), raw

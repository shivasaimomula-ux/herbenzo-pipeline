"""NCBI E-utilities PubMed client.

Replaces the hosted PubMed connector used during development. Provides the two
operations the pipeline needs: search by query, and fetch structured records
(title, abstract, journal, year, publication types, DOI, retraction status).

Configuration by environment variable — never hard-code either value:

* ``NCBI_EMAIL``    — contact address. NCBI asks automated clients to identify
                      themselves; requests work without it but are deprioritised.
* ``NCBI_API_KEY``  — optional. Raises the rate limit from 3 to 10 requests/second.
* ``HERBENZO_PUBMED_CACHE_DIR`` / ``HERBENZO_PUBMED_CACHE_TTL_S`` — shared
  PMID disk cache (Task T16 / Finding #14). Same directory as A / adjudication.

Every response is cached to disk (TTL-aware), so re-running the pipeline costs
no network calls while entries remain fresh.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from herbenzo_pubmed_cache import PmidDiskCache

__all__ = ["PubMedClient", "PubMedError", "Article"]

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

#: Transient HTTP conditions worth retrying with backoff.
_RETRY_CODES = {429, 500, 502, 503, 504}

#: Publication types that map to a higher evidence tier, in ascending order.
_TIER_BY_PUBTYPE = {
    "Meta-Analysis": "meta_analysis",
    "Systematic Review": "meta_analysis",
    "Randomized Controlled Trial": "human_rct",
    "Controlled Clinical Trial": "human_rct",
    "Clinical Trial": "human_observational",
    "Observational Study": "human_observational",
    "Case Reports": "human_observational",
}


class PubMedError(RuntimeError):
    """E-utilities request failed."""


class Article(dict):
    """A PubMed record. Dict-shaped so it serialises straight to the cache."""

    @property
    def pmid(self) -> str:
        return self["pmid"]

    @property
    def is_retracted(self) -> bool:
        return self.get("retracted", False)

    @property
    def evidence_tier(self) -> str:
        """Tier derived from publication type — computed, never model-asserted."""
        for ptype, tier in _TIER_BY_PUBTYPE.items():
            if ptype in self.get("publication_types", []):
                return tier
        return "unclassified"


class PubMedClient:
    def __init__(
        self,
        cache_dir: str | pathlib.Path | None = None,
        email: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        user_agent: str = "herbenzo-pipeline/1.0",
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
        ttl_seconds: int | None = None,
        pmid_cache: PmidDiskCache | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        if pmid_cache is not None:
            self.cache = pmid_cache
        else:
            # Preserve historical default relative path when env unset.
            root = cache_dir if cache_dir is not None else "cache/pubmed"
            self.cache = PmidDiskCache(cache_dir=root, ttl_seconds=ttl_seconds)
        self.cache_dir = pathlib.Path(self.cache.cache_dir)
        self.email = email or os.environ.get("NCBI_EMAIL")
        self.api_key = api_key or os.environ.get("NCBI_API_KEY")
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        # NCBI: 3 req/s without a key, 10 with one.
        self.min_interval_s = 0.11 if self.api_key else 0.34
        self._lock = threading.Lock()
        self._last_call = 0.0

    @property
    def cache_stats(self) -> dict[str, int]:
        return self.cache.stats.as_dict()

    # -- transport ----------------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last_call
            if delta < self.min_interval_s:
                time.sleep(self.min_interval_s - delta)
            self._last_call = time.monotonic()

    def _params(self, **kw) -> dict:
        p = {"db": "pubmed", "tool": "herbenzo-pipeline", **kw}
        if self.email:
            p["email"] = self.email
        if self.api_key:
            p["api_key"] = self.api_key
        return p

    def _call(self, endpoint: str, params: dict) -> bytes:
        url = f"{_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        # NCBI returns 429 when the per-second limit is exceeded and 5xx under
        # load; both are transient and documented. Back off and retry.
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRY_CODES:
                    raise PubMedError(
                        f"E-utilities HTTP {exc.code} on {endpoint}"
                    ) from exc
                last = exc
            except urllib.error.URLError as exc:
                last = exc
            time.sleep(self.backoff_base_s * (2 ** attempt))
        raise PubMedError(
            f"E-utilities unavailable on {endpoint} after "
            f"{self.max_retries} attempts: {last}"
        ) from last

    # -- public API ---------------------------------------------------------

    def search(self, query: str, max_results: int = 20) -> dict:
        """ESearch. Returns ``{query, total, pmids}``.

        A total of 0 is a real, reportable result — the pipeline treats an empty
        search as evidence of absence to be declared, not a failure to retry.
        """
        cached = self.cache.get_search(query, max_results)
        if cached is not None:
            return cached

        raw = self._call("esearch.fcgi", self._params(
            term=query, retmax=max_results, retmode="json", sort="relevance"))
        res = json.loads(raw).get("esearchresult", {})
        out = {
            "query": query,
            "total": int(res.get("count", 0)),
            "pmids": list(res.get("idlist", [])),
        }
        self.cache.put_search(query, max_results, out)
        return out

    def fetch(self, pmids: list[str]) -> dict[str, Article]:
        """EFetch full records for a list of PMIDs, batched and cached per PMID."""
        pmids = [str(p) for p in pmids]
        found: dict[str, Article] = {}
        missing: list[str] = []
        for p in pmids:
            hit = self.cache.get_pmid(p)
            if hit is not None:
                found[p] = Article(hit)
            else:
                missing.append(p)

        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            raw = self._call("efetch.fcgi", self._params(
                id=",".join(batch), retmode="xml"))
            for art in _parse_articles(raw):
                found[art["pmid"]] = art
                self.cache.put_pmid(art["pmid"], art)
        return found


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def _text(node, path: str, default: str = "") -> str:
    el = node.find(path)
    return "".join(el.itertext()).strip() if el is not None else default


def _parse_articles(raw: bytes) -> list[Article]:
    root = ET.fromstring(raw)
    out: list[Article] = []
    for pa in root.findall(".//PubmedArticle"):
        cit = pa.find("MedlineCitation")
        if cit is None:
            continue
        pmid = _text(cit, "PMID")
        art = cit.find("Article")
        if art is None:
            continue

        abstract = " ".join(
            "".join(seg.itertext()).strip()
            for seg in art.findall(".//Abstract/AbstractText")
        ).strip()

        ptypes = [
            "".join(pt.itertext()).strip()
            for pt in art.findall("PublicationTypeList/PublicationType")
        ]

        doi = ""
        for aid in pa.findall(".//ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip()

        year = _text(art, "Journal/JournalIssue/PubDate/Year") or \
            _text(art, "Journal/JournalIssue/PubDate/MedlineDate")[:4]

        # A retraction is a hard block downstream, so capture it at ingest.
        retracted = any("Retracted Publication" in p for p in ptypes) or \
            pa.find(".//CommentsCorrections[@RefType='RetractionIn']") is not None

        out.append(Article({
            "pmid": pmid,
            "title": _text(art, "ArticleTitle"),
            "abstract": abstract,
            "journal": _text(art, "Journal/Title"),
            "year": year,
            "doi": doi,
            "publication_types": ptypes,
            "retracted": retracted,
            "mesh_terms": [
                _text(mh, "DescriptorName")
                for mh in cit.findall("MeshHeadingList/MeshHeading")
            ],
        }))
    return out

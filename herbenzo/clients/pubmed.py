"""NCBI E-utilities PubMed client.

Replaces the hosted PubMed connector used during development. Provides the two
operations the pipeline needs: search by query, and fetch structured records
(title, abstract, journal, year, publication types, DOI, retraction status).

Configuration by environment variable — never hard-code either value:

* ``NCBI_EMAIL``    — contact address. NCBI asks automated clients to identify
                      themselves; requests work without it but are deprioritised.
* ``NCBI_API_KEY``  — optional. Raises the rate limit from 3 to 10 requests/second.

Every response is cached to disk, so re-running the pipeline costs no network calls.
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
        cache_dir: str | pathlib.Path = "cache/pubmed",
        email: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        user_agent: str = "herbenzo-pipeline/1.0",
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
    ) -> None:
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.cache_dir = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.email = email or os.environ.get("NCBI_EMAIL")
        self.api_key = api_key or os.environ.get("NCBI_API_KEY")
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        # NCBI: 3 req/s without a key, 10 with one.
        self.min_interval_s = 0.11 if self.api_key else 0.34
        self._lock = threading.Lock()
        self._last_call = 0.0

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
        key = self.cache_dir / f"search_{_slug(query)}_{max_results}.json"
        if key.exists():
            return json.loads(key.read_text())

        raw = self._call("esearch.fcgi", self._params(
            term=query, retmax=max_results, retmode="json", sort="relevance"))
        res = json.loads(raw).get("esearchresult", {})
        out = {
            "query": query,
            "total": int(res.get("count", 0)),
            "pmids": list(res.get("idlist", [])),
        }
        key.write_text(json.dumps(out, indent=1))
        return out

    def fetch(self, pmids: list[str]) -> dict[str, Article]:
        """EFetch full records for a list of PMIDs, batched and cached per PMID."""
        pmids = [str(p) for p in pmids]
        found: dict[str, Article] = {}
        missing: list[str] = []
        for p in pmids:
            f = self.cache_dir / f"pmid_{p}.json"
            if f.exists():
                found[p] = Article(json.loads(f.read_text()))
            else:
                missing.append(p)

        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            raw = self._call("efetch.fcgi", self._params(
                id=",".join(batch), retmode="xml"))
            for art in _parse_articles(raw):
                found[art["pmid"]] = art
                (self.cache_dir / f"pmid_{art['pmid']}.json").write_text(
                    json.dumps(art, indent=1))
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


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower())[:90]

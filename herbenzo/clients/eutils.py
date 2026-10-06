"""NCBI E-utilities client for enrichment (taxonomy, PubChem CIDs, PubMed, gene, protein).

Rate limit follows NCBI's published policy: 3 requests/second without an API key,
10 requests/second when ``NCBI_API_KEY`` is set. ``NCBI_EMAIL`` and the tool name
are sent when present. The client works with no key.
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from herbenzo.clients.httpjson import CachedJsonClient, HttpError

__all__ = ["EutilsClient", "EutilsError"]

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_UNSET = object()


class EutilsError(RuntimeError):
    """E-utilities request failed or returned an unusable payload."""


class EutilsClient:
    def __init__(
        self,
        *,
        api_key: str | None | object = _UNSET,
        email: str | None | object = _UNSET,
        tool: str | None | object = _UNSET,
        cache_dir: str | Path | None = None,
        transport: Callable | None = None,
        min_interval_s: float | None = None,
        sleep: Callable[[float], None] | None = None,
        user_agent: str = "herbenzo-pipeline/1.0",
        cache_enabled: bool = True,
    ) -> None:
        self.api_key = _from_env("NCBI_API_KEY", api_key)
        self.email = _from_env("NCBI_EMAIL", email)
        resolved_tool = tool if tool is not _UNSET else os.environ.get("NCBI_TOOL")
        self.tool = (str(resolved_tool).strip() if resolved_tool else "") or "herbenzo-pipeline"
        if min_interval_s is None:
            # 10 req/s with a key, 3 req/s without.
            min_interval_s = 0.1 if self.api_key else (1.0 / 3.0)
        self.min_interval_s = float(min_interval_s)
        kwargs: dict[str, Any] = {
            "cache_dir": cache_dir or "cache/enrichment/eutils",
            "transport": transport,
            "min_interval_s": self.min_interval_s,
            "user_agent": user_agent,
            "cache_enabled": cache_enabled,
        }
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.http = CachedJsonClient(**kwargs)

    def search(self, db: str, term: str, *, retmax: int = 5, sort: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"db": db, "term": term, "retmax": retmax, "retmode": "json"}
        if sort:
            params["sort"] = sort
        url = self._url("esearch.fcgi", **params)
        key = _hash_key("esearch", db, term, str(retmax), sort or "")
        payload, retrieved_at = self._json(url, key)
        result = payload.get("esearchresult") or {}
        ids = [str(item) for item in result.get("idlist") or []]
        try:
            count = int(result.get("count") or 0)
        except (TypeError, ValueError):
            count = len(ids)
        return {"db": db, "term": term, "count": count, "ids": ids, "retrieved_at": retrieved_at}

    def summary(self, db: str, ids: list[str]) -> dict[str, Any]:
        clean = [str(item) for item in ids if str(item).strip()]
        if not clean:
            return {"db": db, "records": [], "retrieved_at": None}
        url = self._url("esummary.fcgi", db=db, id=",".join(clean), retmode="json")
        key = _hash_key("esummary", db, ",".join(clean))
        payload, retrieved_at = self._json(url, key)
        return {"db": db, "records": _summary_records(payload), "retrieved_at": retrieved_at}

    def fetch_text(
        self,
        db: str,
        ids: list[str],
        *,
        retmode: str = "xml",
        rettype: str | None = None,
    ) -> tuple[str, str]:
        clean = [str(item) for item in ids if str(item).strip()]
        if not clean:
            raise EutilsError(f"no ids to fetch from {db}")
        params: dict[str, Any] = {"db": db, "id": ",".join(clean), "retmode": retmode}
        if rettype:
            params["rettype"] = rettype
        url = self._url("efetch.fcgi", **params)
        key = _hash_key("efetch", db, ",".join(clean), retmode, rettype or "")
        raw, retrieved_at = self.http.get_bytes(url, cache_key=key)
        return raw.decode("utf-8", errors="replace"), retrieved_at

    def _json(self, url: str, cache_key: str) -> tuple[dict, str]:
        try:
            return self.http.get_json(url, cache_key=cache_key)
        except HttpError as exc:
            raise EutilsError(str(exc)) from exc

    def _url(self, endpoint: str, **params: Any) -> str:
        query: dict[str, Any] = {"tool": self.tool, **params}
        if self.email:
            query["email"] = self.email
        if self.api_key:
            query["api_key"] = self.api_key
        return f"{_BASE}/{endpoint}?{urllib.parse.urlencode(query)}"


def _from_env(name: str, value: str | None | object) -> str | None:
    if value is _UNSET:
        raw = os.environ.get(name)
    else:
        raw = None if value is None else str(value)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _hash_key(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{parts[0]}_{digest}"


def _summary_records(payload: dict) -> list[dict]:
    result = payload.get("result") or {}
    uids = [str(item) for item in result.get("uids") or []]
    records: list[dict] = []
    for uid in uids:
        row = result.get(uid)
        if isinstance(row, dict):
            records.append(row)
    if records:
        return records
    # Some payloads are a flat list under "result" without uids.
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    return []

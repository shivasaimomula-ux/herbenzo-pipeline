"""TTL-aware PMID (and search) disk cache shared across Herbenzo stages.

Envelope format (v1)::

    {
      "schema": "herbenzo.pmid_cache/v1",
      "cached_at": "<ISO-8601 UTC>",
      "ttl_seconds": 2592000,
      "payload": { ... article or search result ... }
    }

Legacy Stage B files that store a bare article dict (no envelope) are still
readable: freshness falls back to the file mtime.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping, Optional

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "ENVELOPE_SCHEMA",
    "CacheLookup",
    "CacheStats",
    "PmidDiskCache",
    "default_cache_dir",
    "resolve_ttl_seconds",
]

ENVELOPE_SCHEMA = "herbenzo.pmid_cache/v1"

#: Default freshness window — 30 days. PubMed records rarely change; TTL
#: still forces periodic refresh so retractions / MeSH updates land.
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60

_ENV_DIR = "HERBENZO_PUBMED_CACHE_DIR"
_ENV_TTL = "HERBENZO_PUBMED_CACHE_TTL_S"


def default_cache_dir() -> pathlib.Path:
    """Resolve the shared cache directory.

    Prefer ``HERBENZO_PUBMED_CACHE_DIR``. Otherwise use the process cwd's
    ``cache/pubmed`` (Stage B historical default) so offline fixtures keep
    working without env changes.
    """
    raw = os.environ.get(_ENV_DIR)
    if raw:
        return pathlib.Path(raw).expanduser().resolve()
    return (pathlib.Path.cwd() / "cache" / "pubmed").resolve()


def resolve_ttl_seconds(ttl_seconds: int | None = None) -> int:
    if ttl_seconds is not None:
        return int(ttl_seconds)
    raw = os.environ.get(_ENV_TTL)
    if raw is not None and str(raw).strip() != "":
        return int(raw)
    return DEFAULT_TTL_SECONDS


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_iso(stamp: str) -> _dt.datetime | None:
    try:
        dt = _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt.astimezone(_dt.UTC)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    expired: int = 0
    writes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "expired": self.expired,
            "writes": self.writes,
        }


@dataclass(frozen=True)
class CacheLookup:
    """Result of a cache probe (before any network fetch)."""

    hit: bool
    stale: bool
    payload: Optional[dict[str, Any]]
    cached_at: Optional[str] = None
    path: Optional[pathlib.Path] = None

    @property
    def fresh(self) -> bool:
        return self.hit and not self.stale and self.payload is not None


@dataclass
class PmidDiskCache:
    """Read-through disk cache keyed by PMID (and optional search slug)."""

    cache_dir: pathlib.Path | str | None = None
    ttl_seconds: int | None = None
    stats: CacheStats = field(default_factory=CacheStats)

    def __post_init__(self) -> None:
        root = (
            pathlib.Path(self.cache_dir)
            if self.cache_dir is not None
            else default_cache_dir()
        )
        self.cache_dir = root.expanduser().resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = resolve_ttl_seconds(self.ttl_seconds)
        self._lock = threading.Lock()

    # -- paths --------------------------------------------------------------

    def pmid_path(self, pmid: str) -> pathlib.Path:
        return self.cache_dir / f"pmid_{str(pmid)}.json"

    def search_path(self, slug: str, max_results: int) -> pathlib.Path:
        safe = "".join(c if c.isalnum() else "_" for c in slug.lower())[:90]
        return self.cache_dir / f"search_{safe}_{int(max_results)}.json"

    # -- PMID API -----------------------------------------------------------

    def lookup_pmid(self, pmid: str) -> CacheLookup:
        """Probe cache without side effects beyond stats."""
        path = self.pmid_path(pmid)
        return self._lookup_path(path)

    def get_pmid(self, pmid: str) -> Optional[dict[str, Any]]:
        """Return fresh payload or ``None`` on miss/expired."""
        lookup = self.lookup_pmid(pmid)
        with self._lock:
            if lookup.fresh:
                self.stats.hits += 1
                return dict(lookup.payload or {})
            if lookup.hit and lookup.stale:
                self.stats.expired += 1
            else:
                self.stats.misses += 1
        return None

    def put_pmid(self, pmid: str, article: Mapping[str, Any]) -> pathlib.Path:
        payload = dict(article)
        payload.setdefault("pmid", str(pmid))
        path = self.pmid_path(pmid)
        self._write_envelope(path, payload)
        with self._lock:
            self.stats.writes += 1
        return path

    # -- search API (same envelope) ----------------------------------------

    def get_search(self, query: str, max_results: int) -> Optional[dict[str, Any]]:
        path = self.search_path(query, max_results)
        lookup = self._lookup_path(path)
        with self._lock:
            if lookup.fresh:
                self.stats.hits += 1
                return dict(lookup.payload or {})
            if lookup.hit and lookup.stale:
                self.stats.expired += 1
            else:
                self.stats.misses += 1
        return None

    def put_search(
        self, query: str, max_results: int, result: Mapping[str, Any]
    ) -> pathlib.Path:
        path = self.search_path(query, max_results)
        self._write_envelope(path, dict(result))
        with self._lock:
            self.stats.writes += 1
        return path

    # -- internals ----------------------------------------------------------

    def _lookup_path(self, path: pathlib.Path) -> CacheLookup:
        if not path.exists():
            return CacheLookup(hit=False, stale=False, payload=None, path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CacheLookup(hit=False, stale=False, payload=None, path=path)

        cached_at: str | None
        payload: dict[str, Any]
        ttl = int(self.ttl_seconds)

        if isinstance(raw, dict) and raw.get("schema") == ENVELOPE_SCHEMA:
            payload = dict(raw.get("payload") or {})
            cached_at = raw.get("cached_at")
            if raw.get("ttl_seconds") is not None:
                try:
                    ttl = int(raw["ttl_seconds"])
                except (TypeError, ValueError):
                    ttl = int(self.ttl_seconds)
        elif isinstance(raw, dict):
            # Legacy bare article / search dict from Stage B.
            payload = dict(raw)
            mtime = path.stat().st_mtime
            cached_at = (
                _dt.datetime.fromtimestamp(mtime, tz=_dt.UTC)
                .isoformat(timespec="seconds")
            )
        else:
            return CacheLookup(hit=False, stale=False, payload=None, path=path)

        stale = self._is_stale(cached_at, ttl, path)
        return CacheLookup(
            hit=True,
            stale=stale,
            payload=payload,
            cached_at=cached_at,
            path=path,
        )

    def _is_stale(
        self,
        cached_at: str | None,
        ttl: int,
        path: pathlib.Path,
    ) -> bool:
        if ttl <= 0:
            return False  # ttl 0 / negative => never expire (ops override)
        when = _parse_iso(cached_at) if cached_at else None
        if when is None:
            when = _dt.datetime.fromtimestamp(path.stat().st_mtime, tz=_dt.UTC)
        age = (_now() - when).total_seconds()
        return age > float(ttl)

    def _write_envelope(self, path: pathlib.Path, payload: MutableMapping[str, Any]) -> None:
        envelope = {
            "schema": ENVELOPE_SCHEMA,
            "cached_at": _now_iso(),
            "ttl_seconds": int(self.ttl_seconds),
            "payload": dict(payload),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(envelope, indent=1), encoding="utf-8")
        tmp.replace(path)

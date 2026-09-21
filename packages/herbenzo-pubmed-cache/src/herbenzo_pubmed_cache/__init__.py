"""Shared PubMed/PMID disk cache (Audit Finding #14 / Task T16).

Canonical on-disk layout aligns with Stage B's evidence layer
(``cache/pubmed/pmid_<id>.json``). Stages A / B / C / adjudication should
point ``HERBENZO_PUBMED_CACHE_DIR`` at the same directory so NCBI EFetch
spend is not duplicated.
"""

from __future__ import annotations

from herbenzo_pubmed_cache.disk import (
    DEFAULT_TTL_SECONDS,
    ENVELOPE_SCHEMA,
    CacheLookup,
    CacheStats,
    PmidDiskCache,
    default_cache_dir,
    resolve_ttl_seconds,
)

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "ENVELOPE_SCHEMA",
    "CacheLookup",
    "CacheStats",
    "PmidDiskCache",
    "default_cache_dir",
    "resolve_ttl_seconds",
]

__version__ = "0.1.0"

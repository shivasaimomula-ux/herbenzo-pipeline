"""Hit / miss / TTL tests for the shared PMID disk cache."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from herbenzo_pubmed_cache import (
    ENVELOPE_SCHEMA,
    PmidDiskCache,
)


ARTICLE = {
    "pmid": "12345678",
    "title": "Example",
    "abstract": "An abstract.",
    "journal": "J Test",
    "year": "2024",
    "doi": "",
    "publication_types": ["Journal Article"],
    "mesh_terms": [],
    "retracted": False,
}


def test_miss_then_hit(tmp_path: Path) -> None:
    cache = PmidDiskCache(cache_dir=tmp_path, ttl_seconds=3600)
    assert cache.get_pmid("12345678") is None
    assert cache.stats.misses == 1
    assert cache.stats.hits == 0

    cache.put_pmid("12345678", ARTICLE)
    assert cache.stats.writes == 1

    got = cache.get_pmid("12345678")
    assert got is not None
    assert got["pmid"] == "12345678"
    assert got["title"] == "Example"
    assert cache.stats.hits == 1

    path = cache.pmid_path("12345678")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["schema"] == ENVELOPE_SCHEMA
    assert "cached_at" in envelope
    assert envelope["ttl_seconds"] == 3600
    assert envelope["payload"]["pmid"] == "12345678"


def test_expired_counts_as_miss_for_get(tmp_path: Path) -> None:
    cache = PmidDiskCache(cache_dir=tmp_path, ttl_seconds=60)
    cache.put_pmid("12345678", ARTICLE)
    path = cache.pmid_path("12345678")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(
        timespec="seconds"
    )
    envelope["cached_at"] = old
    envelope["ttl_seconds"] = 60
    path.write_text(json.dumps(envelope), encoding="utf-8")

    assert cache.get_pmid("12345678") is None
    assert cache.stats.expired == 1
    assert cache.stats.hits == 0

    lookup = cache.lookup_pmid("12345678")
    assert lookup.hit is True
    assert lookup.stale is True
    assert lookup.payload is not None


def test_legacy_bare_article_still_hits(tmp_path: Path) -> None:
    path = tmp_path / "pmid_999.json"
    path.write_text(json.dumps(ARTICLE), encoding="utf-8")
    cache = PmidDiskCache(cache_dir=tmp_path, ttl_seconds=86400)
    got = cache.get_pmid("999")
    assert got is not None
    assert got["pmid"] == "12345678"  # payload pmid preserved from bare file
    assert cache.stats.hits == 1


def test_search_hit_miss(tmp_path: Path) -> None:
    cache = PmidDiskCache(cache_dir=tmp_path, ttl_seconds=3600)
    assert cache.get_search("withania somnifera", 5) is None
    assert cache.stats.misses == 1

    result = {"query": "withania somnifera", "total": 1, "pmids": ["1"]}
    cache.put_search("withania somnifera", 5, result)
    got = cache.get_search("withania somnifera", 5)
    assert got == result
    assert cache.stats.hits == 1


def test_env_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERBENZO_PUBMED_CACHE_DIR", str(tmp_path / "shared"))
    monkeypatch.setenv("HERBENZO_PUBMED_CACHE_TTL_S", "120")
    cache = PmidDiskCache()
    assert cache.cache_dir == (tmp_path / "shared").resolve()
    assert cache.ttl_seconds == 120
    cache.put_pmid("1", {"pmid": "1", "title": "t"})
    assert (tmp_path / "shared" / "pmid_1.json").exists()


def test_ttl_zero_never_expires(tmp_path: Path) -> None:
    cache = PmidDiskCache(cache_dir=tmp_path, ttl_seconds=0)
    cache.put_pmid("1", {"pmid": "1"})
    path = cache.pmid_path("1")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["cached_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert cache.get_pmid("1") is not None
    assert cache.stats.hits == 1

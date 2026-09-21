"""Shared PMID cache hit/miss wiring for Stage B PubMedClient (Task T16)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from herbenzo.clients.pubmed import Article, PubMedClient
from herbenzo_pubmed_cache import PmidDiskCache


ARTICLE = Article({
    "pmid": "12345678",
    "title": "Cached title",
    "abstract": "Cached abstract",
    "journal": "J",
    "year": "2024",
    "doi": "",
    "publication_types": ["Journal Article"],
    "mesh_terms": [],
    "retracted": False,
})


def test_fetch_hit_skips_network(tmp_path: Path) -> None:
    disk = PmidDiskCache(cache_dir=tmp_path, ttl_seconds=3600)
    disk.put_pmid("12345678", ARTICLE)
    client = PubMedClient(pmid_cache=disk)
    client._call = MagicMock(side_effect=AssertionError("network should not run"))

    found = client.fetch(["12345678"])
    assert "12345678" in found
    assert found["12345678"]["title"] == "Cached title"
    assert client.cache_stats["hits"] >= 1
    client._call.assert_not_called()


def test_fetch_miss_writes_cache(tmp_path: Path) -> None:
    disk = PmidDiskCache(cache_dir=tmp_path, ttl_seconds=3600)
    client = PubMedClient(pmid_cache=disk)

    xml = b"""<?xml version="1.0"?>
    <PubmedArticleSet><PubmedArticle><MedlineCitation>
    <PMID Version="1">87654321</PMID>
    <Article>
      <ArticleTitle>Live title</ArticleTitle>
      <Abstract><AbstractText>Live abstract</AbstractText></Abstract>
      <Journal><Title>J Live</Title>
        <JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue>
      </Journal>
      <PublicationTypeList>
        <PublicationType>Journal Article</PublicationType>
      </PublicationTypeList>
    </Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>"""

    client._call = MagicMock(return_value=xml)
    found = client.fetch(["87654321"])
    assert found["87654321"]["title"] == "Live title"
    assert disk.get_pmid("87654321") is not None
    assert client.cache_stats["misses"] >= 1
    assert client.cache_stats["writes"] >= 1

    # Second fetch is a pure hit.
    client._call.reset_mock()
    client._call.side_effect = AssertionError("should be cached")
    again = client.fetch(["87654321"])
    assert again["87654321"]["title"] == "Live title"
    client._call.assert_not_called()


def test_search_hit_miss(tmp_path: Path) -> None:
    disk = PmidDiskCache(cache_dir=tmp_path, ttl_seconds=3600)
    client = PubMedClient(pmid_cache=disk)
    client._call = MagicMock(
        return_value=b'{"esearchresult":{"count":"1","idlist":["1"]}}'
    )
    first = client.search("withania", max_results=5)
    assert first["pmids"] == ["1"]
    client._call.assert_called_once()

    client._call.reset_mock()
    client._call.side_effect = AssertionError("cached search")
    second = client.search("withania", max_results=5)
    assert second == first
    client._call.assert_not_called()

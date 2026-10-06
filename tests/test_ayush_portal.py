"""Ayush Research Portal client. Recorded fixtures only, except the opt-in live test."""

from __future__ import annotations

import inspect
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from herbenzo.api import app
from herbenzo.clients.ayush_portal import (
    AYUSH_PORTAL_FUNCTION,
    AyushPortalClient,
    clean_doi,
    clean_pmid,
    compact_hit,
    parse_record_html,
    parse_search_payload,
    strip_title_prefix,
)
from herbenzo.config import get_settings
from herbenzo.services.ayush_portal import (
    SOURCE,
    AyushPortalService,
    AyushReviewStore,
    ayush_portal_search,
    ayush_portal_search_from_arguments,
    ayush_portal_tool_schema,
)
from herbenzo.services.candidate_store import CandidateStore
from herbenzo.services.enrichment import EnrichmentService
from tests.test_enrichment import FakeChem, FakeEutils, FakePubChem, RankingLlm

FIX = Path(__file__).resolve().parent / "fixtures" / "ayush_portal"
_WHEN = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
_PROVENANCE = (
    "source",
    "arp_id",
    "record_url",
    "query",
    "endpoint",
    "retrieved_at",
    "pmid",
    "doi",
    "cross_check_source",
    "license_basis",
    "permission_ref",
    "attribution",
)
_AYUSH_ENV = (
    "HERBENZO_AYUSH_PORTAL_ENABLED",
    "HERBENZO_AYUSH_PORTAL_BASE_URL",
    "HERBENZO_AYUSH_PORTAL_MIN_INTERVAL_S",
    "HERBENZO_AYUSH_PORTAL_TIMEOUT_S",
    "HERBENZO_AYUSH_PORTAL_MAX_RESULTS",
    "HERBENZO_AYUSH_PORTAL_USER_AGENT",
    "HERBENZO_AYUSH_PORTAL_LICENSE_BASIS",
    "HERBENZO_AYUSH_PORTAL_PERMISSION_REF",
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += float(seconds)


class Scripted:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url: str, headers: dict, timeout: float):
        self.calls.append(url)
        self.headers.append(headers)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class EchoPubmed:
    def __init__(self) -> None:
        self.pmids: list[str] = []

    def summary(self, db: str, ids: list[str]) -> dict:
        assert db == "pubmed"
        pmid = str(ids[0])
        self.pmids.append(pmid)
        return {"records": [{"uid": pmid, "title": "verified"}], "retrieved_at": _WHEN.isoformat()}


def _bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _client(transport, **kwargs) -> AyushPortalClient:
    clock = kwargs.pop("clock", None) or Clock()
    return AyushPortalClient(
        enabled=kwargs.pop("enabled", True),
        transport=transport,
        clock=clock,
        sleep=kwargs.pop("sleep", clock.sleep),
        now=kwargs.pop("now", lambda: _WHEN),
        min_interval_s=kwargs.pop("min_interval_s", 0),
        timeout_s=kwargs.pop("timeout_s", 5),
        max_results=kwargs.pop("max_results", 10),
        user_agent=kwargs.pop("user_agent", "herbenzo-test/ayush"),
        max_retries=kwargs.pop("max_retries", 2),
        backoff_base_s=kwargs.pop("backoff_base_s", 0.5),
        failure_threshold=kwargs.pop("failure_threshold", 3),
        cooldown_s=kwargs.pop("cooldown_s", 60),
        throttle_state=kwargs.pop("throttle_state", None),
        throttle_lock=kwargs.pop("throttle_lock", None),
        **kwargs,
    )


def _service(transport, tmp_path: Path, **kwargs) -> tuple[AyushPortalService, Scripted | object, EchoPubmed]:
    scripted = transport if not isinstance(transport, list) else Scripted(transport)
    if isinstance(transport, list):
        scripted = Scripted(transport)
    pubmed = kwargs.pop("pubmed", None) or EchoPubmed()
    client = _client(scripted, **kwargs)
    service = AyushPortalService(
        client,
        pubmed=pubmed,
        reviews=AyushReviewStore(tmp_path),
        license_basis="verbal_authorization",
        permission_ref="CCRAS Deputy Director Srikanth, Delhi, 2026-10-06; research use permitted for Herbenzo Ayurvedic and Herbal Pvt Ltd",
        now=lambda: _WHEN,
        doi_resolver=kwargs.get("doi_resolver"),
    )
    return service, scripted, pubmed


def _search_ok() -> tuple[int, bytes]:
    return 200, _bytes("search_sample.json")


def _by_id(records: list[dict], arp_id: str) -> dict:
    return next(row for row in records if row["arp_id"] == arp_id)


def test_json_search_parses_placeholders_and_identifiers():
    payload = json.loads(_bytes("search_sample.json"))
    hits, problem = parse_search_payload(payload, base_url="https://arp.ayush.gov.in")
    assert problem is None
    assert len(hits) == 6
    aloe = _by_id(hits, "ARP_AYU030906")
    assert aloe["title"].startswith("EFFECT OF RECONSTITUTED")
    assert not aloe["title"].startswith("258750")
    assert aloe["pmid"] == "38479038"
    assert aloe["doi"] == "10.1016/j.jaim.2024.100887"
    assert aloe["system"] == "ayurveda"
    assert aloe["category"] == "clinical"
    assert aloe["evidence_grade"] == "C"
    assert aloe["arp_internal_id"] == 31408
    assert aloe["record_url"] == "https://arp.ayush.gov.in/View_Res_Landing_Url?rp6=31408"
    assert aloe["journal"] == "Journal of Ayurveda and Integrative Medicine"
    pmid_only = _by_id(hits, "ARP_AYU030905")
    assert pmid_only["pmid"] == "22557632"
    assert pmid_only["doi"] is None
    doi_only = _by_id(hits, "ARP_AYU030899")
    assert doi_only["pmid"] is None
    assert doi_only["doi"] == "10.4103/jras.jras_300_23"
    ashwagandha = _by_id(hits, "ARP_SID003819")
    assert ashwagandha["pmid"] is None
    assert ashwagandha["journal"] is None
    assert ashwagandha["system"] == "siddha"
    assert ashwagandha["category"] == "preclinical"
    assert ashwagandha["doi"].startswith("10.13040/")
    neither = _by_id(hits, "ARP_AYU000001")
    assert neither["pmid"] is None and neither["doi"] is None
    invalid = _by_id(hits, "ARP_AYU000002")
    assert invalid["pmid"] is None and invalid["doi"] is None and invalid["journal"] is None
    for hit in hits:
        assert "abstract" not in hit
        assert "@" not in json.dumps(hit)


def test_placeholder_and_identifier_helpers():
    assert clean_pmid("NA") is None
    assert clean_pmid("NI") is None
    assert clean_pmid("No") is None
    assert clean_pmid("123") is None
    assert clean_pmid("1234567890") is None
    assert clean_pmid("38479038") == "38479038"
    assert clean_doi("No") is None
    assert clean_doi("NA") is None
    assert clean_doi("not-a-doi") is None
    assert clean_doi("10.1016/j.jaim.2024.100887") == "10.1016/j.jaim.2024.100887"
    assert clean_doi("https://doi.org/10.4103/jras.jras_300_23") == "10.4103/jras.jras_300_23"
    assert strip_title_prefix("258750(Ay)-EFFECT OF ALOE", "258750(Ay)") == "EFFECT OF ALOE"


def test_record_page_strips_email_and_drops_the_abstract():
    html = (FIX / "record_31408.html").read_text(encoding="utf-8")
    parsed = parse_record_html(html, base_url="https://arp.ayush.gov.in", internal_id=31408)
    assert parsed is not None
    blob = json.dumps(parsed)
    assert "@" not in blob
    assert "author@example.com" not in blob
    assert "hidden@example.com" not in blob
    assert "ABSTRACT_TOKEN_SHOULD_NOT_PERSIST" not in blob
    assert "AFFILIATION_TOKEN_SHOULD_NOT_PERSIST" not in blob
    assert "CORRESPONDING_TOKEN_SHOULD_NOT_PERSIST" not in blob
    assert "abstract" not in parsed
    assert parsed["authors"] == "1. A. Researcher, 2. B. Colleague"
    assert parsed["title"] == "EFFECT OF RECONSTITUTED ALOE VERA EXTRACT"
    assert parsed["year"] == "2024"
    assert parsed["volume"] == "15"
    assert parsed["issue"] == "2"
    assert parsed["pages"] is None
    assert parsed["publisher_url"] == "https://www.example.org/article/aloe"
    assert parsed["evidence_grade"] == "C"
    assert parsed["arp_id"] == "ARP_AYU030906"


def test_citations_pubmed_doi_and_reviewer_accept(tmp_path: Path):
    transport = Scripted([_search_ok(), _search_ok()])
    service, _scripted, pubmed = _service(transport, tmp_path, min_interval_s=0, max_retries=0)
    result = service.search("Aloe", system="ayurveda", category="clinical")
    assert result["status"] == "ok"
    assert transport.calls and "getFilter_Search_data_home1" in transport.calls[0]
    assert "orderColunm=1" in transport.calls[0]
    assert "system_id=1" in transport.calls[0]
    assert "category_id=1" in transport.calls[0]
    assert "View_Res_Landing_Url" not in transport.calls[0]
    assert transport.headers[0]["User-Agent"] == "herbenzo-test/ayush"
    records = result["records"]
    both = _by_id(records, "ARP_AYU030906")
    pmid_only = _by_id(records, "ARP_AYU030905")
    doi_only = _by_id(records, "ARP_AYU030899")
    neither = _by_id(records, "ARP_AYU000001")
    assert both["citation"]["source"] == "PubMed"
    assert both["citation"]["url"] == "https://pubmed.ncbi.nlm.nih.gov/38479038/"
    assert both["cross_check_source"] == "pubmed"
    assert both["doi"] == "10.1016/j.jaim.2024.100887"
    assert both["review_status"] == "not_required"
    assert pmid_only["citation"]["source"] == "PubMed"
    assert pmid_only["cross_check_source"] == "pubmed"
    assert doi_only["citation"] == {
        "source": "DOI",
        "doi": "10.4103/jras.jras_300_23",
        "url": "https://doi.org/10.4103/jras.jras_300_23",
    }
    assert doi_only["cross_check_source"] == "doi"
    assert doi_only["review_status"] == "not_required"
    assert neither["confidence"] == "low"
    assert neither["review_status"] == "needs_review"
    assert neither["cross_check_source"] == "none"
    assert set(pubmed.pmids) == {"38479038", "22557632"}
    for record in records:
        for key in _PROVENANCE:
            assert key in record
        for key in (
            "source",
            "arp_id",
            "record_url",
            "query",
            "endpoint",
            "retrieved_at",
            "cross_check_source",
            "license_basis",
            "permission_ref",
            "attribution",
        ):
            assert record.get(key) not in (None, ""), key
        assert record["source"] == SOURCE
        assert record["endpoint"] == "getFilter_Search_data_home1"
        assert record["query"] == "Aloe"
        assert record["retrieved_at"] == _WHEN.isoformat()
        assert record["license_basis"] == "verbal_authorization"
        assert "Srikanth" in record["permission_ref"]
        assert record["attribution"] == (
            "Source: Ayush Research Portal, Ministry of Ayush, Government of India"
            f" — {record['record_url']} (ARP ID {record['arp_id']}), retrieved 2026-10-06"
        )
        assert "abstract" not in record
        assert "@" not in json.dumps(record)
    compact = result["hits"]
    assert set(compact[0]) == {
        "arp_id",
        "title",
        "journal",
        "pmid",
        "doi",
        "category",
        "system",
        "evidence_grade",
        "record_url",
    }
    assert "abstract" not in compact[0]
    assert "attribution" not in compact[0]
    accepted = service.accept(neither["arp_id"], note="journal checked", record=neither)
    assert accepted["review_status"] == "accepted"
    assert accepted["confidence"] == "accepted"
    assert accepted["review_note"] == "journal checked"
    assert "abstract" not in accepted
    again = service.search("Aloe")
    assert _by_id(again["records"], "ARP_AYU000001")["review_status"] == "accepted"
    assert _by_id(again["records"], "ARP_AYU000001")["confidence"] == "accepted"


def test_crossref_resolver_is_optional(tmp_path: Path):
    rows = json.loads(_bytes("search_sample.json"))
    only = [row for row in rows if row["arp_id"] == "ARP_AYU030899"]
    transport = Scripted([(200, json.dumps(only).encode())])
    service, _scripted, pubmed = _service(transport, tmp_path, max_retries=0)
    service.doi_resolver = lambda doi: doi.startswith("10.")
    result = service.search("ray")
    record = result["records"][0]
    assert record["cross_check_source"] == "crossref"
    assert pubmed.pmids == []


def test_record_page_through_the_service_has_no_email(tmp_path: Path):
    transport = Scripted([(200, _bytes("record_31408.html"))])
    service, scripted, _pubmed = _service(transport, tmp_path, max_retries=0)
    result = service.record(31408, query="Aloe")
    assert result["status"] == "ok"
    assert "View_Res_Landing_Url?rp6=31408" in scripted.calls[0]
    record = result["record"]
    blob = json.dumps(record)
    assert "@" not in blob
    assert "ABSTRACT_TOKEN_SHOULD_NOT_PERSIST" not in blob
    assert "author@example.com" not in blob
    assert record["endpoint"] == "View_Res_Landing_Url"
    assert record["attribution"].startswith("Source: Ayush Research Portal")
    assert record["publisher_url"] == "https://www.example.org/article/aloe"
    assert "abstract" not in record


def test_throttle_spaces_requests_by_at_least_two_seconds():
    clock = Clock()
    state: dict = {"last": None}
    transport = Scripted([_search_ok(), _search_ok(), _search_ok()])
    client = _client(transport, clock=clock, min_interval_s=2.0, max_retries=0, throttle_state=state)
    other = _client(
        transport,
        clock=clock,
        min_interval_s=2.0,
        max_retries=0,
        throttle_state=state,
        throttle_lock=client.throttle._lock,
    )
    assert client.search("one")["status"] == "ok"
    assert other.search("two")["status"] == "ok"
    assert client.search("three")["status"] == "ok"
    assert clock.sleeps == [2.0, 2.0]
    assert clock.t >= 4.0
    assert len(transport.calls) == 3


def test_retries_only_on_5xx_then_circuit_opens():
    clock = Clock()
    transport = Scripted([(500, b"no"), (500, b"no"), _search_ok()])
    client = _client(transport, clock=clock, min_interval_s=0, max_retries=2, backoff_base_s=0.5)
    assert client.search("Aloe")["status"] == "ok"
    assert clock.sleeps == [0.5, 1.0]
    assert len(transport.calls) == 3

    clock = Clock()
    transport = Scripted([(500, b"no")] * 3 + [_search_ok()])
    client = _client(
        transport,
        clock=clock,
        min_interval_s=0,
        max_retries=0,
        failure_threshold=3,
        cooldown_s=60,
    )
    for _ in range(3):
        failed = client.search("Aloe")
        assert failed["status"] == "unavailable"
        assert failed["reason"] == "http_500"
    opened = client.search("Aloe")
    assert opened["status"] == "unavailable"
    assert opened["reason"] == "circuit_open"
    assert opened["hits"] == []
    assert len(transport.calls) == 3
    clock.t += 60
    assert client.search("Aloe")["status"] == "ok"
    assert len(transport.calls) == 4


def test_timeout_is_not_retried():
    clock = Clock()
    transport = Scripted([TimeoutError("timed out"), _search_ok()])
    client = _client(transport, clock=clock, min_interval_s=0, max_retries=2)
    result = client.search("Aloe")
    assert result["status"] == "unavailable"
    assert result["reason"] == "timeout"
    assert len(transport.calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ((500, b"down"), "http_500"),
        ((200, _bytes("html_error.html")), "html_instead_of_json"),
        ((200, _bytes("not_json.txt")), "non_json"),
        ((200, _bytes("schema_drift.json")), "schema_drift"),
    ],
)
def test_unavailable_results_do_not_raise(body, reason):
    client = _client(Scripted([body, body, body]), min_interval_s=0, max_retries=0)
    result = client.search("Aloe")
    assert result["status"] == "unavailable"
    assert result["reason"] == reason
    assert result["hits"] == []


def test_empty_search_is_a_real_result():
    client = _client(Scripted([(200, _bytes("empty.json"))]), max_retries=0)
    result = client.search("missing-term")
    assert result["status"] == "ok"
    assert result["hits"] == []


def test_count_parses_a_bare_integer():
    transport = Scripted([(200, _bytes("count_ashwagandha.txt"))])
    client = _client(transport, max_retries=0)
    result = client.count("ashwagandha")
    assert result["status"] == "ok"
    assert result["count"] == 102
    assert "getFilter_Search_dataCount_home_page" in transport.calls[0]


def test_disabled_by_default_makes_no_network_call():
    def boom(url, headers, timeout):
        raise AssertionError(url)

    client = AyushPortalClient(transport=boom)
    assert client.enabled is False
    result = client.search("ashwagandha")
    assert result["status"] == "disabled"
    assert result["reason"] == "HERBENZO_AYUSH_PORTAL_ENABLED is false"
    assert result["hits"] == []
    assert client.record(31408)["status"] == "disabled"
    assert client.count("ashwagandha")["status"] == "disabled"


def test_tool_schema_and_adapter_return_compact_hits(tmp_path: Path):
    schema = ayush_portal_tool_schema()
    assert schema["name"] == "ayush_portal_search"
    assert schema["parameters"]["required"] == ["query"]
    assert schema is not AYUSH_PORTAL_FUNCTION
    assert set(schema["parameters"]["properties"]) == {"query", "system", "category", "limit"}
    transport = Scripted([_search_ok()])
    service, _scripted, _pubmed = _service(transport, tmp_path, max_retries=0)
    result = ayush_portal_search_from_arguments(
        {"query": "Aloe", "system": "ayurveda", "category": "clinical", "limit": 10},
        service=service,
    )
    assert result["status"] == "ok"
    assert result["hits"]
    assert set(result["hits"][0]) == set(compact_hit(result["hits"][0]))
    blob = json.dumps(result)
    assert "abstract" not in blob
    assert "@" not in blob
    failed = ayush_portal_search_from_arguments({"limit": 3}, service=service)
    assert failed == {"status": "unavailable", "reason": "empty_query"}


def test_settings_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch):
    for key in _AYUSH_ENV:
        monkeypatch.delenv(key, raising=False)
    settings = get_settings()
    assert settings.ayush_portal_enabled is False
    assert settings.ayush_portal_base_url == "https://arp.ayush.gov.in"
    assert settings.ayush_portal_min_interval_s == 2.0
    assert settings.ayush_portal_timeout_s == 15.0
    assert settings.ayush_portal_max_results == 10
    assert settings.ayush_portal_license_basis == "verbal_authorization"
    assert "Srikanth" in settings.ayush_portal_permission_ref
    assert "2026-10-06" in settings.ayush_portal_permission_ref
    monkeypatch.setenv("HERBENZO_AYUSH_PORTAL_ENABLED", "true")
    monkeypatch.setenv("HERBENZO_AYUSH_PORTAL_MIN_INTERVAL_S", "2.5")
    monkeypatch.setenv("HERBENZO_AYUSH_PORTAL_MAX_RESULTS", "4")
    monkeypatch.setenv("HERBENZO_AYUSH_PORTAL_LICENSE_BASIS", "verbal_authorization")
    updated = get_settings()
    assert updated.ayush_portal_enabled is True
    assert updated.ayush_portal_min_interval_s == 2.5
    assert updated.ayush_portal_max_results == 4
    monkeypatch.setenv("HERBENZO_AYUSH_PORTAL_ENABLED", "false")
    assert get_settings().ayush_portal_enabled is False


def test_client_does_not_use_the_disk_cache():
    source = inspect.getsource(AyushPortalClient)
    module_source = inspect.getsource(inspect.getmodule(AyushPortalClient))
    assert "CachedJsonClient" not in module_source
    assert "cache_dir" not in source


def test_portal_failure_does_not_block_enrichment(tmp_path: Path):
    cases = [
        lambda: Scripted([(500, b"down"), (500, b"down"), (500, b"down")]),
        lambda: Scripted([(200, _bytes("html_error.html"))]),
        lambda: Scripted([(200, _bytes("not_json.txt"))]),
        lambda: Scripted([(200, _bytes("schema_drift.json"))]),
    ]
    for build in cases:
        transport = build()
        portal = _client(transport, min_interval_s=0, max_retries=0)
        ayush = AyushPortalService(portal, reviews=AyushReviewStore(tmp_path / transport.__class__.__name__))
        service = EnrichmentService(
            eutils=FakeEutils(),
            pubchem=FakePubChem(),
            chemclass=FakeChem(),
            llm=RankingLlm(),
            store=CandidateStore(tmp_path / "candidates"),
            ayush=ayush,
        )
        doc = service.propose("Bacopa monnieri")
        assert doc["status"] == "pending"
        assert doc["literature"]["status"] == "ok"
        assert doc["literature"]["articles"][0]["pmid"] == "321"
        assert doc["ayush_portal"]["status"] == "unavailable"
        assert doc["ayush_portal"]["records"] == []
        assert doc["ayush_portal"]["reason"]


def test_disabled_enrichment_does_not_call_the_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERBENZO_AYUSH_PORTAL_ENABLED", "false")

    def boom(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    service = EnrichmentService(
        eutils=FakeEutils(),
        pubchem=FakePubChem(),
        chemclass=FakeChem(),
        llm=RankingLlm(),
        store=CandidateStore(tmp_path),
    )
    doc = service.propose("Bacopa monnieri")
    assert "ayush_portal" not in doc
    assert doc["literature"]["articles"][0]["pmid"] == "321"


def test_enabled_enrichment_stores_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERBENZO_AYUSH_PORTAL_ENABLED", "false")
    transport = Scripted([_search_ok()])
    portal = _client(transport, max_retries=0)
    ayush = AyushPortalService(
        portal,
        pubmed=EchoPubmed(),
        reviews=AyushReviewStore(tmp_path),
        now=lambda: _WHEN,
    )
    service = EnrichmentService(
        eutils=FakeEutils(),
        pubchem=FakePubChem(),
        chemclass=FakeChem(),
        llm=RankingLlm(),
        store=CandidateStore(tmp_path),
        ayush=ayush,
    )
    doc = service.propose("Bacopa monnieri")
    block = doc["ayush_portal"]
    assert block["status"] == "ok"
    assert block["source"] == SOURCE
    assert doc["literature"]["articles"][0]["pmid"] == "321"
    blob = json.dumps(block)
    assert "@" not in blob
    assert "ABSTRACT" not in blob
    for record in block["records"]:
        assert record["attribution"].startswith("Source: Ayush Research Portal")
        assert "abstract" not in record


def test_cli_and_api_accept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("HERBENZO_REGISTRY_DIR", str(tmp_path))
    for key in _AYUSH_ENV:
        monkeypatch.delenv(key, raising=False)
    from herbenzo.cli import main

    assert main(["ayush", "accept", "ARP_AYU000001", "--note", "journal checked"]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["review_status"] == "accepted"
    assert decision["license_basis"] == "verbal_authorization"
    assert "Srikanth" in decision["permission_ref"]
    stored = json.loads((tmp_path / "ayush_reviews.json").read_text(encoding="utf-8"))
    assert stored["ARP_AYU000001"]["note"] == "journal checked"
    assert main(["ayush", "accept", "not-an-id"]) == 2
    assert "invalid" in capsys.readouterr().err

    monkeypatch.setenv("HERBENZO_AYUSH_PORTAL_ENABLED", "false")

    def boom(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert main(["ayush", "search", "Triphala"]) == 0
    searched = json.loads(capsys.readouterr().out)
    assert searched["status"] == "disabled"

    client = TestClient(app)
    accepted = client.post("/enrich/ayush/ARP_AYU030906/accept", json={"note": "seen the journal"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["review_status"] == "accepted"
    assert accepted.json()["arp_id"] == "ARP_AYU030906"
    rejected = client.post("/enrich/ayush/nope/accept", json={})
    assert rejected.status_code == 422


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("HERBENZO_LIVE_TESTS") != "1", reason="set HERBENZO_LIVE_TESTS=1 to call arp.ayush.gov.in")
def test_live_search_smoke():
    client = AyushPortalClient(enabled=True, min_interval_s=2.0, max_results=1)
    result = client.search("ashwagandha", limit=1)
    assert result["status"] in {"ok", "unavailable"}
    if result["status"] == "ok":
        assert result["hits"]
        assert "abstract" not in result["hits"][0]

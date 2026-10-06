"""Live bibliographic client for the Ayush Research Portal.

The portal (https://arp.ayush.gov.in) has no published API. Search results come
from the same undocumented JSON endpoint the public search page uses. Record
pages are server-rendered HTML and each fetch increments a public visit
counter, so this client does not fetch them during search.

There is no disk cache. Pass an injectable transport with the same signature
as ``urllib_transport``. A process-wide throttle spaces requests at least
``min_interval_s`` apart (default 2 seconds). Public methods return
``{"status": "unavailable", "reason": ...}`` on failure and do not raise.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from html import unescape
from typing import Any, Callable

from herbenzo.clients.httpjson import urllib_transport

__all__ = [
    "AYUSH_PORTAL_FUNCTION",
    "AYUSH_PORTAL_TOOL",
    "DEFAULT_BASE_URL",
    "DEFAULT_USER_AGENT",
    "SEARCH_ENDPOINT",
    "COUNT_ENDPOINT",
    "RECORD_ENDPOINT",
    "AyushPortalClient",
    "CircuitBreaker",
    "IntervalThrottle",
    "compact_hit",
    "parse_record_html",
    "parse_search_payload",
    "record_url_for",
]

DEFAULT_BASE_URL = "https://arp.ayush.gov.in"
DEFAULT_USER_AGENT = "herbenzo-pipeline/1.0 (Ayush Research Portal bibliographic lookup; research use)"
SEARCH_ENDPOINT = "getFilter_Search_data_home1"
COUNT_ENDPOINT = "getFilter_Search_dataCount_home_page"
RECORD_ENDPOINT = "View_Res_Landing_Url"

# Gemini function declaration. OpenAI wrappers use AYUSH_PORTAL_TOOL.
AYUSH_PORTAL_FUNCTION: dict[str, Any] = {
    "name": "ayush_portal_search",
    "description": (
        "Search the Ministry of Ayush Research Portal (arp.ayush.gov.in) by title "
        "keywords for Ayurveda, Siddha, Unani, Homoeopathy, and Yoga research articles, "
        "including Indian journals that PubMed does not index. Returns bibliographic "
        "metadata only (no abstracts): arp_id, title, journal, pmid, doi, category, "
        "system, evidence_grade, record_url. Title-only matching: use a botanical "
        "binomial or a common/Sanskrit name, not a long sentence. Opt-in: returns "
        "status disabled unless HERBENZO_AYUSH_PORTAL_ENABLED is true. On failure "
        "the result is status unavailable; use PubMed and web search instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "1-4 title keywords, for example 'Withania somnifera' or 'Triphala'.",
            },
            "system": {
                "type": "string",
                "enum": [
                    "any",
                    "ayurveda",
                    "yoga_naturopathy",
                    "unani",
                    "siddha",
                    "homoeopathy",
                    "sowa_rigpa",
                ],
            },
            "category": {
                "type": "string",
                "enum": ["any", "clinical", "preclinical", "drug", "fundamental"],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        },
        "required": ["query"],
    },
}
AYUSH_PORTAL_TOOL: dict[str, Any] = {"type": "function", "function": AYUSH_PORTAL_FUNCTION}

COMPACT_KEYS = (
    "arp_id",
    "title",
    "journal",
    "pmid",
    "doi",
    "category",
    "system",
    "evidence_grade",
    "record_url",
)

_SYSTEM_IDS = {
    "ayurveda": 1,
    "yoga_naturopathy": 2,
    "yoga_and_naturopathy": 2,
    "unani": 3,
    "siddha": 4,
    "homoeopathy": 5,
    "homeopathy": 5,
    "sowa_rigpa": 6,
}
_CATEGORY_IDS = {
    "clinical": 1,
    "preclinical": 2,
    "drug": 3,
    "fundamental": 4,
    "fundamental_and_others": 4,
}
_SYSTEM_LABELS = {
    "AYURVEDA": "ayurveda",
    "YOGA & NATUROPATHY": "yoga_naturopathy",
    "YOGA AND NATUROPATHY": "yoga_naturopathy",
    "UNANI": "unani",
    "SIDDHA": "siddha",
    "HOMOEOPATHY": "homoeopathy",
    "HOMEOPATHY": "homoeopathy",
    "SOWA RIGPA": "sowa_rigpa",
}
_PLACEHOLDERS = {"na", "ni", "no", "n/a", "n.a.", "null"}
_PMID = re.compile(r"^\d{5,9}$")
_DOI = re.compile(r"^10\.\S+/\S+$")
_ARTID_PREFIX = re.compile(r"^\s*\d+\([^)]+\)\s*-\s*")
_EMAIL = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.I)
_GRADE = re.compile(r"GRADE\s*([ABC])\b", re.I)
_LABEL_VALUE = re.compile(
    r"<label>\s*(?P<label>.*?)\s*</label>\s*<span\b(?P<attrs>[^>]*)>(?P<body>.*?)</span>",
    re.I | re.S,
)
_ART_TITLE = re.compile(
    r"Art ID\s*:\s*(?P<artid>[^<]*)</span>\s*<span\b[^>]*value-bind[^>]*>(?P<title>.*?)</span>",
    re.I | re.S,
)
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.I)
_SKIP_LABELS = {
    "background",
    "conclusion",
    "conclusions",
    "objective",
    "objectives",
    "method",
    "methods",
    "result",
    "results",
    "abstract",
    "keyword",
    "keywords",
    "corresponding author details",
    "corresponding author",
    "author's affiliation",
    "authors affiliation",
    "author affiliation",
    "number of visits",
    "number of downloads",
    "disease",
    "body system",
    "access",
}

_PROCESS_THROTTLE_STATE: dict[str, float | None] = {"last": None}
_PROCESS_THROTTLE_LOCK = threading.Lock()
_PROCESS_BREAKER: "CircuitBreaker | None" = None
_PROCESS_BREAKER_LOCK = threading.Lock()

Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures and stays open for ``cooldown_s``."""

    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_s: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until: float | None = None

    def allow(self) -> bool:
        with self._lock:
            if self._open_until is None:
                return True
            if self._clock() >= self._open_until:
                self._open_until = None
                self._failures = 0
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_until = self._clock() + self.cooldown_s


def process_breaker() -> CircuitBreaker:
    global _PROCESS_BREAKER
    with _PROCESS_BREAKER_LOCK:
        if _PROCESS_BREAKER is None:
            _PROCESS_BREAKER = CircuitBreaker()
        return _PROCESS_BREAKER


class IntervalThrottle:
    """At most one request every ``min_interval_s``. Shared state is process-wide by default."""

    def __init__(
        self,
        min_interval_s: float,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        state: dict[str, float | None] | None = None,
        lock: threading.Lock | None = None,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._state = state if state is not None else _PROCESS_THROTTLE_STATE
        self._lock = lock if lock is not None else _PROCESS_THROTTLE_LOCK

    def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            now = self._clock()
            last = self._state.get("last")
            if last is not None:
                delay = self.min_interval_s - (now - float(last))
                if delay > 0:
                    self._sleep(delay)
                    now = self._clock()
            self._state["last"] = now


class AyushPortalClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        enabled: bool = False,
        transport: Transport | None = None,
        min_interval_s: float = 2.0,
        timeout_s: float = 15.0,
        max_results: int = 10,
        user_agent: str = DEFAULT_USER_AGENT,
        max_retries: int = 2,
        backoff_base_s: float = 0.5,
        failure_threshold: int = 3,
        cooldown_s: float = 300.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
        throttle_state: dict[str, float | None] | None = None,
        throttle_lock: threading.Lock | None = None,
        breaker: CircuitBreaker | None = None,
        share_process_breaker: bool = True,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.enabled = bool(enabled)
        self.transport = transport or urllib_transport
        self.timeout_s = float(timeout_s)
        self.max_results = max(1, min(int(max_results), 25))
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.max_retries = max(0, int(max_retries))
        self.backoff_base_s = max(0.0, float(backoff_base_s))
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._now = now or (lambda: datetime.now(UTC))
        # A fake clock must not write into the process-wide throttle timestamp.
        if clock is not None and throttle_state is None:
            throttle_state = {"last": None}
        if clock is not None and throttle_lock is None:
            throttle_lock = threading.Lock()
        self.throttle = IntervalThrottle(
            min_interval_s,
            clock=self._clock,
            sleep=self._sleep,
            state=throttle_state,
            lock=throttle_lock,
        )
        if breaker is not None:
            self.breaker = breaker
        elif share_process_breaker and clock is None:
            self.breaker = process_breaker()
        else:
            self.breaker = CircuitBreaker(
                threshold=failure_threshold,
                cooldown_s=cooldown_s,
                clock=self._clock,
            )

    def search(
        self,
        query: str,
        *,
        system: str | None = None,
        category: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search published records. Never raises. Does not fetch record pages.

        ``offset`` is the portal's ``startPage`` (a row offset, not a page number).
        """
        if not self.enabled:
            return self._stopped("disabled", "HERBENZO_AYUSH_PORTAL_ENABLED is false", SEARCH_ENDPOINT)
        text = (query or "").strip()
        if not text:
            return self._stopped("unavailable", "empty_query", SEARCH_ENDPOINT)
        if len(text) > 200:
            text = text[:200]
        try:
            system_id = _filter_id(system, _SYSTEM_IDS, empty="any")
            category_id = _filter_id(category, _CATEGORY_IDS, empty="any")
        except ValueError as exc:
            return self._stopped("unavailable", str(exc), SEARCH_ENDPOINT)
        capped = self._limit(limit)
        if capped is None:
            return self._stopped("unavailable", "invalid_limit", SEARCH_ENDPOINT)
        start = self._offset(offset)
        if start is None:
            return self._stopped("unavailable", "invalid_offset", SEARCH_ENDPOINT)
        params = {
            "startPage": start,
            "pageLength": capped,
            "Search": text,
            "orderColunm": 1,
            "orderType": "desc",
            "system_id": system_id,
            "category_id": category_id,
            "arp_id": 0,
            "app_status": 1,
        }
        url = f"{self.base_url}/{SEARCH_ENDPOINT}?{urllib.parse.urlencode(params)}"
        fetched = self._get_text(url, endpoint=SEARCH_ENDPOINT)
        if fetched["status"] != "ok":
            return fetched
        body = fetched["text"]
        if _looks_like_html(body):
            self.breaker.record_failure()
            return self._stopped("unavailable", "html_instead_of_json", SEARCH_ENDPOINT, retrieved_at=fetched["retrieved_at"])
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.breaker.record_failure()
            return self._stopped("unavailable", "non_json", SEARCH_ENDPOINT, retrieved_at=fetched["retrieved_at"])
        hits, problem = parse_search_payload(payload, base_url=self.base_url)
        if problem:
            self.breaker.record_failure()
            return self._stopped("unavailable", problem, SEARCH_ENDPOINT, retrieved_at=fetched["retrieved_at"])
        self.breaker.record_success()
        return {
            "status": "ok",
            "reason": None,
            "endpoint": SEARCH_ENDPOINT,
            "retrieved_at": fetched["retrieved_at"],
            "hits": hits[:capped],
        }

    def count(self, query: str, *, system: str | None = None, category: str | None = None) -> dict[str, Any]:
        """Published-record count. Never raises."""
        if not self.enabled:
            return self._stopped("disabled", "HERBENZO_AYUSH_PORTAL_ENABLED is false", COUNT_ENDPOINT)
        text = (query or "").strip()
        if not text:
            return self._stopped("unavailable", "empty_query", COUNT_ENDPOINT)
        try:
            system_id = _filter_id(system, _SYSTEM_IDS, empty="any")
            category_id = _filter_id(category, _CATEGORY_IDS, empty="any")
        except ValueError as exc:
            return self._stopped("unavailable", str(exc), COUNT_ENDPOINT)
        params = {
            "Search": text,
            "system_id": system_id,
            "category_id": category_id,
            "arp_id": 0,
            "app_status": 1,
        }
        url = f"{self.base_url}/{COUNT_ENDPOINT}?{urllib.parse.urlencode(params)}"
        fetched = self._get_text(url, endpoint=COUNT_ENDPOINT)
        if fetched["status"] != "ok":
            return fetched
        body = fetched["text"].strip()
        if _looks_like_html(body):
            self.breaker.record_failure()
            return self._stopped("unavailable", "html_instead_of_json", COUNT_ENDPOINT, retrieved_at=fetched["retrieved_at"])
        count = _parse_count(body)
        if count is None:
            self.breaker.record_failure()
            reason = "non_json" if not body[:1].isdigit() else "schema_drift"
            return self._stopped("unavailable", reason, COUNT_ENDPOINT, retrieved_at=fetched["retrieved_at"])
        self.breaker.record_success()
        return {
            "status": "ok",
            "reason": None,
            "endpoint": COUNT_ENDPOINT,
            "retrieved_at": fetched["retrieved_at"],
            "count": count,
        }

    def record(self, internal_id: int | str) -> dict[str, Any]:
        """Fetch one record page. Call only when year, authors, or the publisher URL are required."""
        if not self.enabled:
            return self._stopped("disabled", "HERBENZO_AYUSH_PORTAL_ENABLED is false", RECORD_ENDPOINT)
        try:
            ident = int(str(internal_id).strip())
        except (TypeError, ValueError):
            return self._stopped("unavailable", "invalid_id", RECORD_ENDPOINT)
        if ident <= 0:
            return self._stopped("unavailable", "invalid_id", RECORD_ENDPOINT)
        url = record_url_for(self.base_url, ident)
        fetched = self._get_text(url, endpoint=RECORD_ENDPOINT, accept="text/html,application/xhtml+xml")
        if fetched["status"] != "ok":
            return fetched
        parsed = parse_record_html(fetched["text"], base_url=self.base_url, internal_id=ident)
        if parsed is None:
            self.breaker.record_failure()
            return self._stopped("unavailable", "schema_drift", RECORD_ENDPOINT, retrieved_at=fetched["retrieved_at"])
        self.breaker.record_success()
        return {
            "status": "ok",
            "reason": None,
            "endpoint": RECORD_ENDPOINT,
            "retrieved_at": fetched["retrieved_at"],
            "record": parsed,
        }

    def _limit(self, limit: int | None) -> int | None:
        if limit is None:
            return self.max_results
        if isinstance(limit, bool):
            return None
        try:
            value = int(limit)
        except (TypeError, ValueError):
            return None
        if value < 1:
            return None
        return min(value, self.max_results, 25)

    def _offset(self, offset: int | None) -> int | None:
        if offset is None:
            return 0
        if isinstance(offset, bool):
            return None
        try:
            value = int(offset)
        except (TypeError, ValueError):
            return None
        if value < 0 or value > 5000:
            return None
        return value

    def _stopped(
        self,
        status: str,
        reason: str,
        endpoint: str,
        *,
        retrieved_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "endpoint": endpoint,
            "retrieved_at": retrieved_at,
            "hits": [],
        }

    def _get_text(self, url: str, *, endpoint: str, accept: str = "application/json, text/plain, */*") -> dict[str, Any]:
        headers = {"User-Agent": self.user_agent, "Accept": accept}
        attempt = 0
        retries_left = self.max_retries
        while True:
            if not self.breaker.allow():
                return self._stopped("unavailable", "circuit_open", endpoint)
            self.throttle.wait()
            try:
                status, raw = self.transport(url, headers, self.timeout_s)
            except Exception as exc:
                self.breaker.record_failure()
                reason = "timeout" if _is_timeout(exc) else "transport_error"
                return self._stopped("unavailable", reason, endpoint)
            if 500 <= int(status) <= 599:
                if retries_left <= 0:
                    self.breaker.record_failure()
                    return self._stopped("unavailable", f"http_{int(status)}", endpoint)
                self._sleep(self.backoff_base_s * (2**attempt))
                attempt += 1
                retries_left -= 1
                continue
            if int(status) >= 400:
                self.breaker.record_failure()
                return self._stopped("unavailable", f"http_{int(status)}", endpoint)
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            return {
                "status": "ok",
                "reason": None,
                "endpoint": endpoint,
                "retrieved_at": self._now().isoformat(),
                "text": text,
                "hits": [],
            }


def compact_hit(record: dict[str, Any]) -> dict[str, Any]:
    """Tool payload. Bibliographic fields only; no abstract and no provenance block."""
    return {key: record.get(key) for key in COMPACT_KEYS}


def record_url_for(base_url: str, internal_id: int) -> str:
    return f"{base_url.rstrip('/')}/{RECORD_ENDPOINT}?rp6={int(internal_id)}"


def parse_search_payload(payload: Any, *, base_url: str) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(hits, error)``. ``error`` is ``schema_drift`` when the array shape changed."""
    if not isinstance(payload, list):
        return [], "schema_drift"
    hits: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            return [], "schema_drift"
        if "arp_id" not in item or "title_manual" not in item or "id" not in item:
            return [], "schema_drift"
        hit = _hit_from_row(item, base_url)
        if hit is None:
            return [], "schema_drift"
        hits.append(hit)
    return hits, None


def parse_record_html(html: str, *, base_url: str, internal_id: int) -> dict[str, Any] | None:
    """Bibliographic fields from a record page. Abstracts and emails are dropped."""
    if not html or not str(html).strip():
        return None
    cleaned = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    fields: dict[str, dict[str, str | None]] = {}
    for match in _LABEL_VALUE.finditer(cleaned):
        label = _label_key(_strip_tags(match.group("label")))
        if not label or _skip_label(label):
            continue
        body = match.group("body")
        href_match = _HREF.search(body)
        href = unescape(href_match.group(1)).strip() if href_match else None
        fields[label] = {"text": strip_emails(_strip_tags(body)), "href": href}
    title = ""
    title_match = _ART_TITLE.search(cleaned)
    if title_match:
        title = strip_emails(_strip_tags(title_match.group("title")))
    arp_id = _clean_placeholder((fields.get("arp id") or {}).get("text"))
    if not arp_id or not title:
        return None
    publisher = _publisher_url(fields)
    doi = clean_doi((fields.get("doi") or {}).get("text"))
    pages = _clean_placeholder((fields.get("page") or fields.get("pages") or {}).get("text"))
    return _scrub_mapping(
        {
            "arp_id": arp_id,
            "arp_internal_id": int(internal_id),
            "title": strip_title_prefix(title),
            "title_original": title,
            "journal": _clean_placeholder((fields.get("journal") or {}).get("text")),
            "year": _year((fields.get("year") or {}).get("text")),
            "volume": _clean_placeholder((fields.get("volume") or {}).get("text")),
            "issue": _clean_placeholder((fields.get("issue") or {}).get("text")),
            "pages": pages,
            "authors": clean_authors((fields.get("authors") or {}).get("text")),
            "pmid": None,
            "doi": doi,
            "publisher_url": publisher,
            "system": None,
            "category": _category_label((fields.get("category") or {}).get("text")),
            "evidence_grade": _evidence_grade((fields.get("sub-category") or fields.get("sub category") or {}).get("text")),
            "record_url": record_url_for(base_url, internal_id),
        }
    )


def strip_title_prefix(title: str, artid: str | None = None) -> str:
    text = (title or "").strip()
    original = text
    if artid:
        prefix = artid.strip() + "-"
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    text = _ARTID_PREFIX.sub("", text, count=1).strip()
    return text or original


def clean_pmid(value: Any) -> str | None:
    text = _clean_placeholder(value)
    if text is None or not _PMID.fullmatch(text):
        return None
    return text


def clean_authors(value: Any) -> str | None:
    """Author line from a record page.

    The portal sometimes glues the next numbered author onto the previous
    surname (``Langade 1. Vaishali``). Insert the missing separator.
    """
    text = _clean_placeholder(value)
    if text is None:
        return None
    return re.sub(r"(?<=[A-Za-z.)])\s+(?=\d+\.\s)", ", ", text)


def normalize_http_url(value: Any) -> str | None:
    """Publisher URL. The portal sometimes renders ``https: //host/...``."""
    text = _clean_placeholder(value)
    if text is None:
        return None
    text = re.sub(r"\s+", "", text)
    if not text.lower().startswith(("http://", "https://")):
        return None
    return strip_emails(text) or None


def clean_doi(value: Any) -> str | None:
    text = _clean_placeholder(value)
    if text is None:
        return None
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, count=1, flags=re.I).strip()
    if not _DOI.fullmatch(text):
        return None
    return text


def strip_emails(value: str | None) -> str:
    if not value:
        return ""
    cleaned = _EMAIL.sub(" ", value)
    cleaned = re.sub(r"\s+([,;])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" ,;")


def _hit_from_row(item: dict[str, Any], base_url: str) -> dict[str, Any] | None:
    arp_id = _clean_placeholder(item.get("arp_id"))
    title_raw = _as_text(item.get("title_manual"))
    if not arp_id or not title_raw:
        return None
    try:
        internal_id = int(str(item.get("id")).strip())
    except (TypeError, ValueError):
        return None
    if internal_id <= 0:
        return None
    artid = _as_text(item.get("artid"))
    title = strip_title_prefix(title_raw, artid)
    return _scrub_mapping(
        {
            "arp_id": arp_id,
            "arp_internal_id": internal_id,
            "title": title,
            "title_original": title_raw.strip(),
            "journal": _clean_placeholder(item.get("journal")),
            "year": None,
            "volume": None,
            "issue": None,
            "pages": None,
            "authors": None,
            "pmid": clean_pmid(item.get("pubmed_id")),
            "doi": clean_doi(item.get("doi")),
            "publisher_url": None,
            "system": _system_label(item.get("system")),
            "category": _category_label(item.get("category")) or _category_from_id(item.get("c_id")),
            "evidence_grade": _evidence_grade(item.get("sub_category")),
            "record_url": record_url_for(base_url, internal_id),
        }
    )


def _scrub_mapping(record: dict[str, Any]) -> dict[str, Any]:
    """Drop personal-data and abstract fields if a future payload includes them."""
    for key in ("abstract", "email", "emails", "corresponding_author", "affiliation", "keywords"):
        record.pop(key, None)
    for key, value in list(record.items()):
        if isinstance(value, str):
            record[key] = strip_emails(value) or None
            if key in {"pmid", "doi", "arp_id", "record_url", "title", "title_original"} and record[key] is None:
                record[key] = strip_emails(value)
    return record


def _filter_id(value: str | None, table: dict[str, int], *, empty: str) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text or text.casefold() == empty:
        return 0
    key = text.casefold().replace("&", "and").replace("-", "_").replace(" ", "_")
    key = re.sub(r"_+", "_", key).strip("_")
    if key not in table:
        kind = "invalid_system" if table is _SYSTEM_IDS else "invalid_category"
        raise ValueError(kind)
    return table[key]


def _system_label(value: Any) -> str | None:
    text = _clean_placeholder(value)
    if text is None:
        return None
    mapped = _SYSTEM_LABELS.get(text.upper())
    if mapped:
        return mapped
    key = text.casefold().replace("&", "and").replace(" ", "_")
    return key if key in _SYSTEM_IDS else text.casefold()


def _category_label(value: Any) -> str | None:
    text = _clean_placeholder(value)
    if text is None:
        return None
    upper = text.upper()
    if "PRECLINICAL" in upper:
        return "preclinical"
    if "CLINICAL" in upper:
        return "clinical"
    if "DRUG" in upper:
        return "drug"
    if "FUNDAMENTAL" in upper:
        return "fundamental"
    return None


def _category_from_id(value: Any) -> str | None:
    return {1: "clinical", 2: "preclinical", 3: "drug", 4: "fundamental"}.get(_maybe_int(value) or -1)


def _evidence_grade(value: Any) -> str | None:
    text = _clean_placeholder(value)
    if text is None:
        return None
    match = _GRADE.search(text)
    if not match:
        return None
    return match.group(1).upper()


def _publisher_url(fields: dict[str, dict[str, str | None]]) -> str | None:
    for key, field in fields.items():
        if not key.startswith("url"):
            continue
        for candidate in (field.get("href"), field.get("text")):
            url = normalize_http_url(candidate)
            if url:
                return url
    return None


def _year(value: Any) -> str | None:
    text = _clean_placeholder(value)
    if text is None:
        return None
    match = re.search(r"\b(1[89]\d{2}|20\d{2})\b", text)
    return match.group(1) if match else None


def _label_key(label: str) -> str:
    text = unescape(label).replace("\xa0", " ")
    text = text.rstrip(":").strip().casefold()
    return re.sub(r"\s+", " ", text)


def _skip_label(label: str) -> bool:
    if label.startswith("url"):
        return False
    if label in _SKIP_LABELS:
        return True
    return label.startswith("icpc")


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _clean_placeholder(value: Any) -> str | None:
    text = _as_text(value)
    if text is None:
        return None
    text = text.strip()
    if not text or text.casefold() in _PLACEHOLDERS:
        return None
    if re.fullmatch(r"[-–—.\s]+", text):
        return None
    return text


def _as_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value).strip() or None
    text = str(value).strip()
    return text or None


def _maybe_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_count(body: str) -> int | None:
    text = body.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, int):
        return payload
    if isinstance(payload, str) and payload.strip().isdigit():
        return int(payload.strip())
    return None


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith(("<!doctype", "<html", "<head", "<body"))


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError) and "timed out" in str(exc).lower():
        return True
    return "timeout" in exc.__class__.__name__.lower()

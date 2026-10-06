"""Cached JSON/text GET with retry, used by enrichment clients."""

from __future__ import annotations

import json
import pathlib
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Callable

__all__ = ["CachedJsonClient", "HttpError", "urllib_transport"]

_RETRY_CODES = {429, 500, 502, 503, 504}

Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body


def urllib_transport(url: str, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        return int(exc.code), raw


class CachedJsonClient:
    """Disk-cached GET. Successful responses are stored with a retrieval timestamp."""

    def __init__(
        self,
        cache_dir: str | pathlib.Path = "cache/enrichment",
        *,
        transport: Transport | None = None,
        min_interval_s: float = 0.25,
        timeout_s: float = 30.0,
        user_agent: str = "herbenzo-pipeline/1.0",
        max_retries: int = 4,
        backoff_base_s: float = 0.4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_dir = pathlib.Path(cache_dir)
        self.transport = transport or urllib_transport
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call = 0.0

    def get_bytes(self, url: str, *, cache_key: str) -> tuple[bytes, str]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / f"{_safe_key(cache_key)}.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            return cached["body"].encode("utf-8"), str(cached["retrieved_at"])

        headers = {"User-Agent": self.user_agent, "Accept": "application/json, application/xml, text/xml"}
        last_status: int | None = None
        last_body = ""
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                status, raw = self.transport(url, headers, self.timeout_s)
            except urllib.error.URLError as exc:
                last_status = 0
                last_body = str(exc)
                self._sleep(self.backoff_base_s * (2**attempt))
                continue
            if status == 404:
                raise HttpError(404, url, raw.decode("utf-8", errors="replace")[:500])
            if status in _RETRY_CODES:
                last_status = status
                last_body = raw.decode("utf-8", errors="replace")[:500]
                self._sleep(self.backoff_base_s * (2**attempt))
                continue
            if status >= 400:
                raise HttpError(status, url, raw.decode("utf-8", errors="replace")[:500])
            text = raw.decode("utf-8", errors="replace")
            retrieved_at = datetime.now(UTC).isoformat()
            cache_file.write_text(
                json.dumps({"retrieved_at": retrieved_at, "body": text}, ensure_ascii=False),
                encoding="utf-8",
            )
            return raw, retrieved_at
        raise HttpError(last_status or 0, url, last_body or "request failed")

    def get_json(self, url: str, *, cache_key: str) -> tuple[dict, str]:
        raw, retrieved_at = self.get_bytes(url, cache_key=cache_key)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HttpError(200, url, "response was not JSON") from exc
        if not isinstance(payload, dict):
            raise HttpError(200, url, "JSON response was not an object")
        return payload, retrieved_at

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            delta = time.monotonic() - self._last_call
            if delta < self.min_interval_s:
                self._sleep(self.min_interval_s - delta)
            self._last_call = time.monotonic()


def _safe_key(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
    return cleaned[:120] or "request"

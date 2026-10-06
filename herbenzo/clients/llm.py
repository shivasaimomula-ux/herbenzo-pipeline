"""Optional OpenAI-compatible chat client for enrichment justification.

The model may rank marker names and write a narrative. It is not called when
``HERBENZO_LLM_API_KEY`` is unset. Physicochemical numbers in a response are
stripped by the enrichment service and are never stored as descriptors.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from herbenzo.services.enrich_parse import parse_llm_content, strip_llm_numerics

__all__ = ["LlmClient", "LlmError"]

_UNSET = object()


class LlmError(RuntimeError):
    """The configured LLM endpoint failed."""


class LlmClient:
    def __init__(
        self,
        *,
        api_key: str | None | object = _UNSET,
        base_url: str | None | object = _UNSET,
        model: str | None | object = _UNSET,
        transport: Callable[..., tuple[int, bytes]] | None = None,
        timeout_s: float = 45.0,
    ) -> None:
        self.api_key = _clean(api_key, "HERBENZO_LLM_API_KEY")
        self.base_url = (_clean(base_url, "HERBENZO_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = _clean(model, "HERBENZO_LLM_MODEL") or "gpt-4o-mini"
        self.transport = transport
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def justify(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            return {
                "status": "unavailable",
                "provider": "openai-compatible",
                "model": None,
                "narrative": None,
                "ranking": [],
                "ignored_names": [],
                "numerics_ignored": False,
                "error": "HERBENZO_LLM_API_KEY is not set",
            }
        try:
            content = self._complete(_messages(context))
            payload = parse_llm_content(content)
        except (LlmError, ValueError, json.JSONDecodeError) as exc:
            return {
                "status": "unavailable",
                "provider": "openai-compatible",
                "model": self.model,
                "narrative": None,
                "ranking": [],
                "ignored_names": [],
                "numerics_ignored": False,
                "error": str(exc),
            }
        cleaned, numerics_ignored = strip_llm_numerics(payload)
        ranking = cleaned.get("ranking") if isinstance(cleaned, dict) else None
        if not isinstance(ranking, list):
            ranking = []
        narrative = cleaned.get("narrative") if isinstance(cleaned, dict) else None
        if narrative is not None and not isinstance(narrative, str):
            narrative = None
        return {
            "status": "ok",
            "provider": "openai-compatible",
            "model": self.model,
            "narrative": narrative.strip() if isinstance(narrative, str) else None,
            "ranking": [item for item in ranking if isinstance(item, dict)],
            "ignored_names": [],
            "numerics_ignored": numerics_ignored,
            "error": None,
        }

    def _complete(self, messages: list[dict[str, str]]) -> str:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": messages,
            }
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "herbenzo-pipeline/1.0",
        }
        if self.transport is not None:
            status, raw = self.transport(url, body, headers, self.timeout_s)
        else:
            status, raw = _urllib_post(url, body, headers, self.timeout_s)
        if status >= 400:
            raise LlmError(f"LLM HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LlmError("LLM response was not JSON") from exc
        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("LLM response did not include message content") from exc


def _messages(context: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You rank candidate marker compounds for a botanical ingredient registry "
                "and write a short justification. Reply with a JSON object containing "
                "\"narrative\" (string) and \"ranking\" (array of objects with \"name\" and "
                "\"rationale\"). Use only marker names from the user message. "
                "Do not include physicochemical numbers of any kind: no molecular weight, "
                "logP, XLogP, TPSA, hydrogen-bond counts, pKa, solubility, or BCS class. "
                "Those values are retrieved from PubChem and must not be invented or repeated."
            ),
        },
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]


def _clean(value: str | None | object, env_name: str) -> str | None:
    if value is _UNSET:
        raw = os.environ.get(env_name)
    else:
        raw = None if value is None else str(value)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _urllib_post(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        return int(exc.code), raw

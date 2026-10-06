"""Validate the optional OpenAI-compatible LLM settings.

Gemini's compatible endpoint is ``https://generativelanguage.googleapis.com/v1beta/openai``.
A host with no path returns 404. ``gemini-4.0-argon`` is not a published model;
``gemini-2.5-flash`` is the example that works.
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = ["KNOWN_GEMINI_MODELS", "GEMINI_OPENAI_BASE", "validate_llm_settings"]

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_HOST = "generativelanguage.googleapis.com"

#: Models confirmed against the public Gemini model list. Not an ingredient allow-list.
KNOWN_GEMINI_MODELS = frozenset(
    {
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash-8b",
    }
)


def validate_llm_settings(
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> dict[str, str | None]:
    """Return ``status`` of ``ok``, ``unconfigured``, or ``invalid`` plus a message."""
    if not api_key:
        return {
            "status": "unconfigured",
            "base_url": base_url,
            "model": model,
            "message": (
                "HERBENZO_LLM_API_KEY is not set. Research uses the deterministic "
                "NCBI and PubChem path."
            ),
        }
    url = (base_url or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = (parsed.path or "").rstrip("/")
    chosen = (model or "").strip()
    if host == _GEMINI_HOST:
        if path in {"", "/"}:
            extra = ""
            if chosen not in KNOWN_GEMINI_MODELS:
                extra = (
                    f" Model {chosen or '(empty)'} is also unknown: "
                    "gemini-4.0-argon does not exist; gemini-2.5-flash is known to work."
                )
            return {
                "status": "invalid",
                "base_url": url,
                "model": chosen or None,
                "message": (
                    "Gemini base URL has no path and returns 404. "
                    f"Use {GEMINI_OPENAI_BASE}.{extra}"
                ),
            }
        if not path.endswith("/openai"):
            return {
                "status": "invalid",
                "base_url": url,
                "model": chosen or None,
                "message": (
                    "Gemini OpenAI-compatible calls need a path ending in /openai. "
                    f"Use {GEMINI_OPENAI_BASE}"
                ),
            }
        if chosen not in KNOWN_GEMINI_MODELS:
            return {
                "status": "invalid",
                "base_url": url,
                "model": chosen or None,
                "message": (
                    f"Model {chosen or '(empty)'} is not in the known Gemini model list. "
                    "gemini-4.0-argon does not exist. gemini-2.5-flash is known to work. "
                    "Set HERBENZO_LLM_MODEL."
                ),
            }
    if not chosen:
        return {
            "status": "invalid",
            "base_url": url,
            "model": None,
            "message": "HERBENZO_LLM_MODEL is empty.",
        }
    return {"status": "ok", "base_url": url, "model": chosen, "message": None}

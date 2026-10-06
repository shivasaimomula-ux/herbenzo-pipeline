"""Environment configuration for Stage B.

Loads a repo-root ``.env`` once, without overriding variables that are already
set in the process environment. NCBI and the optional LLM stay keyless when
those variables are absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOADED = False

__all__ = [
    "Settings",
    "get_settings",
    "load_project_env",
    "repo_root",
]


def repo_root() -> Path:
    return _REPO_ROOT


def load_project_env(path: Path | None = None) -> Path | None:
    """Load dotenv file. Existing environment variables win.

    Called with no path, this loads ``<repo>/.env`` at most once per process.
    An explicit path is loaded every time (tests use a temporary file).
    """
    global _DEFAULT_LOADED
    from dotenv import load_dotenv

    if path is None:
        if _DEFAULT_LOADED:
            return None
        _DEFAULT_LOADED = True
        env_path = _REPO_ROOT / ".env"
        if not env_path.is_file():
            return None
        load_dotenv(env_path, override=False)
        return env_path

    env_path = Path(path)
    if not env_path.is_file():
        return None
    load_dotenv(env_path, override=False)
    return env_path


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


@dataclass(frozen=True)
class Settings:
    ncbi_api_key: str | None
    ncbi_email: str | None
    ncbi_tool: str
    ncbi_min_interval_s: float
    llm_api_key: str | None
    llm_base_url: str
    llm_model: str
    llm_available: bool
    registry_dir: Path
    enrichment_cache_dir: Path
    imppat_dir: Path | None
    min_pubmed_refs: int
    research_max_steps: int
    research_timeout_s: float
    web_search_endpoint: str | None


def get_settings() -> Settings:
    """Read the current process environment. Call ``load_project_env`` first."""
    api_key = _blank_to_none(os.environ.get("NCBI_API_KEY"))
    llm_key = _blank_to_none(os.environ.get("HERBENZO_LLM_API_KEY"))
    registry = _blank_to_none(os.environ.get("HERBENZO_REGISTRY_DIR"))
    cache = _blank_to_none(os.environ.get("HERBENZO_ENRICHMENT_CACHE_DIR"))
    imppat = _blank_to_none(os.environ.get("HERBENZO_IMPPAT_DIR"))
    return Settings(
        ncbi_api_key=api_key,
        ncbi_email=_blank_to_none(os.environ.get("NCBI_EMAIL")),
        ncbi_tool=_blank_to_none(os.environ.get("NCBI_TOOL")) or "herbenzo-pipeline",
        # NCBI: 3 requests/second without a key, 10 with one.
        ncbi_min_interval_s=0.1 if api_key else (1.0 / 3.0),
        llm_api_key=llm_key,
        llm_base_url=(
            _blank_to_none(os.environ.get("HERBENZO_LLM_BASE_URL"))
            or "https://api.openai.com/v1"
        ).rstrip("/"),
        llm_model=_blank_to_none(os.environ.get("HERBENZO_LLM_MODEL")) or "gpt-4o-mini",
        llm_available=bool(llm_key),
        registry_dir=Path(registry).expanduser()
        if registry
        else _REPO_ROOT / "herbenzo" / "data" / "registry_overlay",
        enrichment_cache_dir=Path(cache).expanduser() if cache else Path("cache") / "enrichment",
        imppat_dir=_imppat_dir(imppat),
        min_pubmed_refs=_positive_int(os.environ.get("HERBENZO_MIN_PUBMED_REFS"), default=1),
        research_max_steps=_positive_int(os.environ.get("HERBENZO_RESEARCH_MAX_STEPS"), default=8),
        research_timeout_s=_positive_float(os.environ.get("HERBENZO_RESEARCH_TIMEOUT_S"), default=60.0),
        web_search_endpoint=_blank_to_none(os.environ.get("HERBENZO_WEB_SEARCH_ENDPOINT")),
    )


def _positive_int(value: str | None, *, default: int) -> int:
    text = _blank_to_none(value)
    if text is None:
        return default
    try:
        number = int(text)
    except ValueError:
        return default
    return number if number > 0 else default


def _positive_float(value: str | None, *, default: float) -> float:
    text = _blank_to_none(value)
    if text is None:
        return default
    try:
        number = float(text)
    except ValueError:
        return default
    return number if number > 0 else default


def _imppat_dir(value: str | None) -> Path | None:
    """Local IMPPAT cache. ``off`` / ``disabled`` / ``none`` turns the step off.

    A relative path is resolved from the repo root so it does not follow the
    process working directory.
    """
    if value is None:
        return _REPO_ROOT / "data" / "external" / "imppat" / "cache"
    if value.casefold() in {"off", "disabled", "none"}:
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return _REPO_ROOT / path

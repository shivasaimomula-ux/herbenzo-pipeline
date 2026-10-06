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


def _env_bool(name: str, default: bool) -> bool:
    raw = _blank_to_none(os.environ.get(name))
    if raw is None:
        return default
    return raw.casefold() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = _blank_to_none(os.environ.get(name))
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    return value


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _blank_to_none(os.environ.get(name))
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


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
    ayush_portal_enabled: bool
    ayush_portal_base_url: str
    ayush_portal_min_interval_s: float
    ayush_portal_timeout_s: float
    ayush_portal_max_results: int
    ayush_portal_user_agent: str
    ayush_portal_license_basis: str
    ayush_portal_permission_ref: str


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
        ayush_portal_enabled=_env_bool("HERBENZO_AYUSH_PORTAL_ENABLED", False),
        ayush_portal_base_url=(
            _blank_to_none(os.environ.get("HERBENZO_AYUSH_PORTAL_BASE_URL"))
            or "https://arp.ayush.gov.in"
        ).rstrip("/"),
        ayush_portal_min_interval_s=_env_float("HERBENZO_AYUSH_PORTAL_MIN_INTERVAL_S", 2.0, minimum=0.0),
        ayush_portal_timeout_s=_env_float("HERBENZO_AYUSH_PORTAL_TIMEOUT_S", 15.0, minimum=1.0),
        ayush_portal_max_results=_env_int("HERBENZO_AYUSH_PORTAL_MAX_RESULTS", 10, minimum=1, maximum=25),
        ayush_portal_user_agent=(
            _blank_to_none(os.environ.get("HERBENZO_AYUSH_PORTAL_USER_AGENT"))
            or "herbenzo-pipeline/1.0 (Ayush Research Portal bibliographic lookup; research use)"
        ),
        ayush_portal_license_basis=(
            _blank_to_none(os.environ.get("HERBENZO_AYUSH_PORTAL_LICENSE_BASIS"))
            or "verbal_authorization"
        ),
        ayush_portal_permission_ref=(
            _blank_to_none(os.environ.get("HERBENZO_AYUSH_PORTAL_PERMISSION_REF"))
            or (
                "CCRAS Deputy Director Srikanth, Delhi, 2026-10-06; "
                "research use permitted for Herbenzo Ayurvedic and Herbal Pvt Ltd"
            )
        ),
    )


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

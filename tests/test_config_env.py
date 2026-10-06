"""Repo-root .env loading. Process environment wins; missing keys stay keyless."""

from __future__ import annotations

from pathlib import Path

import pytest

from herbenzo.clients.eutils import EutilsClient
from herbenzo.clients.llm import LlmClient
from herbenzo.config import get_settings, load_project_env

_KEYS = (
    "NCBI_API_KEY",
    "NCBI_EMAIL",
    "NCBI_TOOL",
    "HERBENZO_LLM_API_KEY",
    "HERBENZO_LLM_BASE_URL",
    "HERBENZO_LLM_MODEL",
)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_settings_read_a_temp_dotenv_file(isolated_env, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NCBI_API_KEY=from-file",
                "NCBI_EMAIL=dev@example.com",
                "HERBENZO_LLM_API_KEY=llm-file",
                "HERBENZO_LLM_BASE_URL=https://llm.example/v1",
                "HERBENZO_LLM_MODEL=test-model",
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert load_project_env(env_file) == env_file
    settings = get_settings()
    assert settings.ncbi_api_key == "from-file"
    assert settings.ncbi_email == "dev@example.com"
    assert settings.ncbi_min_interval_s == pytest.approx(0.1)
    assert settings.llm_api_key == "llm-file"
    assert settings.llm_base_url == "https://llm.example/v1"
    assert settings.llm_model == "test-model"
    assert settings.llm_available is True


def test_process_environment_overrides_dotenv(isolated_env, tmp_path: Path):
    isolated_env.setenv("NCBI_API_KEY", "from-env")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "NCBI_API_KEY=from-file\nHERBENZO_LLM_API_KEY=from-file\n",
        encoding="utf-8",
    )
    load_project_env(env_file)
    settings = get_settings()
    assert settings.ncbi_api_key == "from-env"
    assert settings.llm_api_key == "from-file"


def test_missing_keys_degrade_to_keyless_ncbi_and_unavailable_llm(isolated_env, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("# no secrets\n", encoding="utf-8")
    load_project_env(env_file)
    settings = get_settings()
    assert settings.ncbi_api_key is None
    assert settings.ncbi_email is None
    assert settings.ncbi_min_interval_s == pytest.approx(1 / 3)
    assert settings.llm_available is False
    llm = LlmClient(api_key=settings.llm_api_key, base_url=settings.llm_base_url, model=settings.llm_model)
    skipped = llm.justify({"scientific_name": "Bacopa monnieri", "markers": ["Bacoside A"]})
    assert skipped["status"] == "unavailable"
    assert skipped["narrative"] is None
    assert "HERBENZO_LLM_API_KEY" in skipped["error"]
    eutils = EutilsClient(api_key=settings.ncbi_api_key, email=settings.ncbi_email, tool=settings.ncbi_tool)
    assert eutils.api_key is None
    assert eutils.min_interval_s == pytest.approx(1 / 3)

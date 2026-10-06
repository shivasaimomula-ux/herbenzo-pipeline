"""License notice for scripts/fetch_imppat.py. No network."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fetch_imppat.py"


def _load():
    spec = importlib.util.spec_from_file_location("fetch_imppat", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    status = 200

    def __init__(self) -> None:
        self._payload = b"plant_id\tname\n"

    def read(self, _n: int = -1) -> bytes:
        payload, self._payload = self._payload, b""
        return payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _install_fake_download(monkeypatch: pytest.MonkeyPatch, module):
    calls: list[str] = []

    def _fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        assert timeout > 0
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)
    return calls


def test_download_prints_notice_without_the_old_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    calls = _install_fake_download(monkeypatch, module)
    code = module.main([
        "--cache-dir",
        str(tmp_path),
        "--only",
        "Plant_Information_IMPPAT.tsv",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "Attribution-NonCommercial-NoDerivatives" in out
    assert "https://creativecommons.org/licenses/by-nc-nd/4.0/" in out or (
        "creativecommons.org/licenses/by-nc-nd/4.0" in out
    )
    assert "Refusing to download" not in out
    assert calls == [module.BASE_URL + "Plant_Information_IMPPAT.tsv"]
    written = tmp_path / "Plant_Information_IMPPAT.tsv"
    assert written.read_bytes() == b"plant_id\tname\n"


def test_old_license_flag_is_a_noop_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    calls = _install_fake_download(monkeypatch, module)
    code = module.main([
        "--accept-noncommercial-license",
        "--cache-dir",
        str(tmp_path),
        "--only",
        "Plant_Information_IMPPAT.tsv",
    ])
    assert code == 0
    err = capsys.readouterr().err
    assert "Refusing to download" not in err
    assert calls == [module.BASE_URL + "Plant_Information_IMPPAT.tsv"]
    assert (tmp_path / "Plant_Information_IMPPAT.tsv").is_file()

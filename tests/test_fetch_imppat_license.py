"""License gate for scripts/fetch_imppat.py. No network."""

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


def test_refuses_download_without_license_flag(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    module = _load()

    def _boom(*_args, **_kwargs):
        raise AssertionError("network must not run without --accept-noncommercial-license")

    monkeypatch.setattr(module.urllib.request, "urlopen", _boom)

    assert module.main([]) == 2
    err = capsys.readouterr().err
    assert "Refusing to download" in err
    assert "Attribution-NonCommercial-NoDerivatives" in err


def test_license_flag_reaches_downloader_without_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load()
    calls: list[str] = []

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

    def _fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        assert timeout > 0
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)

    code = module.main([
        "--accept-noncommercial-license",
        "--cache-dir",
        str(tmp_path),
        "--only",
        "Plant_Information_IMPPAT.tsv",
    ])
    assert code == 0
    assert calls == [module.BASE_URL + "Plant_Information_IMPPAT.tsv"]
    written = tmp_path / "Plant_Information_IMPPAT.tsv"
    assert written.read_bytes() == b"plant_id\tname\n"
    assert not (tmp_path / "Plant_Information_IMPPAT.tsv.partial").exists()

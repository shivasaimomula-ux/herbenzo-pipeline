"""File-backed enrichment candidates. Nothing here is part of the registry until approval."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from herbenzo.services.overlay import registry_dir

__all__ = ["CandidateStore"]

_CANDIDATE_ID = re.compile(r"^c[a-f0-9]{16}$")
_LOCK = threading.Lock()


class CandidateStore:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory).expanduser() if directory is not None else registry_dir()

    def candidates_dir(self) -> Path:
        return self.directory / "candidates"

    def save(self, doc: dict) -> None:
        candidate_id = str(doc.get("candidate_id") or "")
        path = self._path(candidate_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(doc, indent=2) + "\n"
        with _LOCK:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)

    def get(self, candidate_id: str) -> dict | None:
        path = self._path(candidate_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("candidate_id") != candidate_id:
            return None
        return data

    def list(self, status: str | None = None) -> list[dict]:
        directory = self.candidates_dir()
        if not directory.is_dir():
            return []
        docs: list[dict] = []
        for path in directory.glob("c*.json"):
            if not _CANDIDATE_ID.fullmatch(path.stem):
                continue
            doc = self.get(path.stem)
            if doc is None:
                continue
            if status and doc.get("status") != status:
                continue
            docs.append(doc)
        docs.sort(key=lambda doc: str(doc.get("updated_at") or doc.get("created_at") or ""), reverse=True)
        return docs

    def _path(self, candidate_id: str) -> Path:
        if not _CANDIDATE_ID.fullmatch(candidate_id or ""):
            raise ValueError("candidate id is invalid")
        directory = self.candidates_dir().resolve()
        path = (directory / f"{candidate_id}.json").resolve()
        if path.parent != directory:
            raise ValueError("candidate id is invalid")
        return path

"""Approved registry overlay. Pending enrichment candidates are not stored here."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from herbenzo.config import get_settings

__all__ = [
    "allocate_ingredient_id",
    "approved_dir",
    "load_approved_documents",
    "registry_dir",
    "save_approved_document",
]

_ID = re.compile(r"^HB-[A-Z0-9]{3,16}$")
_LOCK = threading.Lock()


def registry_dir() -> Path:
    return get_settings().registry_dir


def approved_dir() -> Path:
    return registry_dir() / "approved"


def load_approved_documents() -> list[dict]:
    directory = approved_dir()
    if not directory.is_dir():
        return []
    docs: list[dict] = []
    for path in sorted(directory.glob("HB-*.json")):
        if not _ID.fullmatch(path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ingredient = data.get("ingredient")
        if not isinstance(ingredient, dict):
            continue
        if ingredient.get("ingredient_id") != path.stem:
            continue
        docs.append(data)
    return docs


def save_approved_document(doc: dict) -> None:
    ingredient = doc.get("ingredient") or {}
    ingredient_id = str(ingredient.get("ingredient_id") or "")
    if not _ID.fullmatch(ingredient_id):
        raise ValueError(f"invalid ingredient id {ingredient_id!r}")
    directory = approved_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / f"{ingredient_id}.json").resolve()
    if path.parent != directory.resolve():
        raise ValueError("invalid ingredient id")
    payload = json.dumps(doc, indent=2) + "\n"
    with _LOCK:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)


def allocate_ingredient_id(scientific_name: str, taken: set[str]) -> str:
    genus = (scientific_name or "NEW").split()[0]
    letters = "".join(ch for ch in genus.upper() if ch.isalpha())
    if len(letters) < 4:
        letters = (letters + "XXXX")[:4]
    else:
        letters = letters[:4]
    base = f"HB-{letters}"
    if base not in taken:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}{suffix}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(f"could not allocate an ingredient id for {scientific_name!r}")

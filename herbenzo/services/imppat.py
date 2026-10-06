"""Optional IMPPAT 3.0 context after NCBI Taxonomy has resolved a binomial.

This step does not block. A missing cache, a parse failure, no hit, or several
hits never raises and never blocks candidate approval. A single match may be
copied onto the approved overlay row with source and citation.

Column names were checked on 6 October 2026 from the first rows of the batch
files at ``https://cb.imsc.res.in/imppat/images/Batch_Download/`` (the download
page itself is behind Cloudflare). The loader still accepts American
``standardized`` spellings and ignores unknown columns.

Observed headers:

* ``Plant_Information_IMPPAT.tsv`` — ``Plant_identifier``,
  ``Indian_Medicinal_plant``, ``Synonymous names``, ``Kingdom``, ``Family``,
  ``Group``, ``Common_name``, ``System_of_Medicine``. No Sanskrit column.
* ``IMPPAT_SingleHerbalFormulations.tsv`` — API formulation and ingredient
  names, ``Plant part in API_original``, ``Plant_name_standardised``,
  ``Plant_part_standardised``.
* ``IMPPAT_PolyHerbalFormulations.tsv`` — AFI formulation and ingredient
  names, ``Plant part in AFI_original``, ``Plant_name_standardised``,
  ``Plant_part_standardised``.
* ``IMPPAT_Phytochemical_Plant_Association.tsv`` (optional) —
  ``Plant_identifier``, ``Indian_Medicinal_plant``, ``Plant_part``,
  ``IMPPAT_Phytochemical_identifier``.

Sanskrit/IAST names are taken from the formulation columns that carry the
original API drug name or the original AFI ingredient name. The plant table
supplies family, common names, and Latin synonyms.
"""

from __future__ import annotations

import csv
import io
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["CITATIONS", "ImppatLookup", "SOURCE", "approved_context", "sole_standardized_part"]

SOURCE = "IMPPAT 3.0"
LICENSE_NAME = "CC BY-NC-ND 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-nd/4.0/"

# Homepage citation block, checked 6 October 2026. IMPPAT 3.0 was listed as submitted.
CITATIONS: tuple[dict[str, str], ...] = (
    {
        "authors": (
            "Karthikeyan Mohanraj, Bagavathy Shanmugam Karthikeyan, R.P. Vivek-Ananth, "
            "R.P. Bharath Chand, S.R. Aparna, P. Mangalapandi, and Areejit Samal"
        ),
        "title": (
            "IMPPAT: A curated database of Indian Medicinal Plants, Phytochemistry And Therapeutics"
        ),
        "venue": "Scientific Reports 8:4329 (2018)",
        "url": "https://www.nature.com/articles/s41598-018-22631-z",
    },
    {
        "authors": "R. P. Vivek-Ananth, Karthikeyan Mohanraj, Ajaya Kumar Sahoo, and Areejit Samal",
        "title": "IMPPAT 2.0: An Enhanced and Expanded Phytochemical Atlas of Indian Medicinal Plants",
        "venue": "ACS Omega 8:8827–8845 (2023)",
        "url": "https://pubs.acs.org/doi/10.1021/acsomega.3c00156",
    },
    {
        "authors": (
            "Shanmuga Priya Baskaran, Ajaya Kumar Sahoo, Priyotosh Sil, Rahul Tiwari, "
            "Nikhil Chivukula, Sabrina Elsa Eapen, Geetha Ranganathan, Preeti Semwal, "
            "and Areejit Samal"
        ),
        "title": "IMPPAT 3.0: An updated FAIR database of phytochemicals and formulations of Indian Medicinal plants",
        "venue": "submitted (2026)",
        "url": "https://cb.imsc.res.in/imppat",
    },
)

PLANT_FILE = "Plant_Information_IMPPAT.tsv"
SINGLE_FILE = "IMPPAT_SingleHerbalFormulations.tsv"
POLY_FILE = "IMPPAT_PolyHerbalFormulations.tsv"
PHYTO_FILE = "IMPPAT_Phytochemical_Plant_Association.tsv"
RECOGNIZED_FILES = (PLANT_FILE, SINGLE_FILE, POLY_FILE, PHYTO_FILE)

_FORMULATION_LIMIT = 40
_PHYTO_LIMIT = 30

_INDEX_CACHE: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
_INDEX_LOCK = threading.Lock()


def _norm_header(value: str) -> str:
    text = value.strip().lower().replace("standardized", "standardised")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _norm_name(value: str) -> str:
    text = value.replace("\u00a0", " ").strip().casefold().replace("×", "x")
    return re.sub(r"\s+", " ", text)


def _split_pipe(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[|;]", value) if part.strip()]


def _split_comma(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = value.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _file_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def _pick(headers: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        if key in headers:
            return headers[key]
    return None


def _pick_contains(headers: dict[str, str], *needles: str, exclude: set[str] | None = None) -> str | None:
    skipped = exclude or set()
    for key, original in headers.items():
        if key in skipped:
            continue
        if all(needle in key for needle in needles):
            return original
    return None


def _cell(row: dict[str, str], column: str | None) -> str:
    if not column:
        return ""
    return (row.get(column) or "").strip()


def _read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    """Return ``(headers, data rows, error)``. ``error`` is empty on success."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [], [], f"{path.name}: {exc.__class__.__name__}"
    if not raw.strip():
        return [], [], f"{path.name}: empty file"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return [], [], f"{path.name}: unsupported encoding"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        return [], [], f"{path.name}: not valid UTF-8"
    if "\x00" in text:
        return [], [], f"{path.name}: not a TSV"
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        fieldnames = [name for name in (reader.fieldnames or []) if name and name.strip()]
        if len(fieldnames) < 2:
            return [], [], f"{path.name}: missing a tab-separated header"
        rows: list[dict[str, str]] = []
        for row in reader:
            if not isinstance(row, dict):
                continue
            cleaned = {key: (value or "").strip() for key, value in row.items() if key}
            if any(cleaned.values()):
                rows.append(cleaned)
    except csv.Error as exc:
        return [], [], f"{path.name}: {exc}"
    return fieldnames, rows, ""


def _blank_result(status: str, error: str | None, *, files: list[dict[str, str]] | None = None) -> dict[str, Any]:
    present = files or []
    timestamps = [item["retrieved_at"] for item in present if item.get("retrieved_at")]
    return {
        "status": status,
        "advisory": True,
        "source": SOURCE,
        "files": present,
        "retrieved_at": max(timestamps) if timestamps else None,
        "error": error,
        "warnings": [],
        "query_names": [],
        "plants": [],
        "sanskrit_names": [],
        "synonyms": [],
        "common_names": [],
        "family": None,
        "families": [],
        "plant_parts": [],
        "formulations": [],
        "formulation_count": 0,
        "formulations_truncated": False,
        "phytochemicals": [],
        "phytochemical_count": 0,
        "phytochemicals_truncated": False,
    }


def approved_context(block: dict[str, Any] | None) -> dict[str, Any] | None:
    """Fields safe to copy onto an approved overlay row.

    Only a single unambiguous match is copied. ``no_match``, ``ambiguous``,
    and ``unavailable`` return ``None`` so approval does not invent names.
    """
    if not isinstance(block, dict) or block.get("status") != "matched":
        return None
    return {
        "source": SOURCE,
        "license": LICENSE_NAME,
        "license_url": LICENSE_URL,
        "citations": [dict(item) for item in CITATIONS],
        "status": "matched",
        "files": list(block.get("files") or []),
        "retrieved_at": block.get("retrieved_at"),
        "sanskrit_names": list(block.get("sanskrit_names") or []),
        "synonyms": list(block.get("synonyms") or []),
        "common_names": list(block.get("common_names") or []),
        "plant_parts": list(block.get("plant_parts") or []),
        "formulations": list(block.get("formulations") or []),
        "family": block.get("family"),
    }


def sole_standardized_part(block: dict[str, Any] | None) -> str | None:
    """One standardised plant part, when the match names exactly one."""
    context = approved_context(block)
    if context is None:
        return None
    found: list[str] = []
    seen: set[str] = set()
    for row in context["plant_parts"]:
        if not isinstance(row, dict):
            continue
        text = row.get("standardized") or row.get("original") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        key = text.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(text.strip())
    if len(found) == 1:
        return found[0]
    return None


class ImppatLookup:
    """Read local IMPPAT TSVs and attach Ayurvedic context for one binomial."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = None if directory is None else Path(directory).expanduser()

    def lookup(self, scientific_name: str, synonyms: list[str] | None = None) -> dict[str, Any]:
        try:
            return self._lookup(scientific_name, list(synonyms or []))
        except Exception as exc:
            return _blank_result("unavailable", f"IMPPAT lookup failed: {exc.__class__.__name__}")

    def _lookup(self, scientific_name: str, synonyms: list[str]) -> dict[str, Any]:
        directory = self.directory
        if directory is None:
            return _blank_result("unavailable", "IMPPAT lookup is disabled")
        if not directory.exists():
            return _blank_result("unavailable", f"IMPPAT directory is missing: {directory}")
        if not directory.is_dir():
            return _blank_result("unavailable", f"IMPPAT path is not a directory: {directory}")

        present = [directory / name for name in RECOGNIZED_FILES if (directory / name).is_file()]
        if not present:
            return _blank_result("unavailable", f"IMPPAT directory has no recognized TSVs: {directory}")

        try:
            tables = self._tables(directory, present)
        except OSError as exc:
            return _blank_result("unavailable", f"IMPPAT directory could not be read: {exc.__class__.__name__}")
        if tables["readable"] == 0 or tables["data_rows"] == 0:
            result = _blank_result("unavailable", "IMPPAT files were empty or unreadable", files=tables["files"])
            result["warnings"] = tables["warnings"]
            if tables["warnings"]:
                result["error"] = "; ".join(tables["warnings"])
            elif tables["data_rows"] == 0:
                result["error"] = "IMPPAT tables have headers but no data rows"
            return result

        query = {_norm_name(scientific_name)} if _norm_name(scientific_name) else set()
        for synonym in synonyms:
            key = _norm_name(str(synonym))
            if key:
                query.add(key)
        query.discard("")

        plants = [row for row in tables["plants"] if row["names"] & query]
        expanded = set(query)
        for plant in plants:
            expanded |= plant["names"]
        plant_ids = {row["plant_identifier"] for row in plants if row["plant_identifier"]}
        formulations = [
            row for row in tables["formulations"] if row["match_names"] & (expanded if plants else query)
        ]
        phytochemicals = [
            row
            for row in tables["phytochemicals"]
            if (row["plant_identifier"] and row["plant_identifier"] in plant_ids)
            or (row["plant_name"] and row["plant_name"] in expanded)
        ]

        identities = {row["identity"] for row in plants}
        form_names = {row["plant_name"] for row in formulations if row["plant_name"]}
        if plants or formulations:
            if len(identities) > 1 or (not identities and len(form_names) > 1):
                status = "ambiguous"
            else:
                status = "matched"
        else:
            status = "no_match"

        sanskrit: list[str] = []
        parts: list[dict[str, str]] = []
        seen_parts: set[tuple[str, str, str]] = set()
        public_forms: list[dict[str, Any]] = []
        for row in formulations:
            sanskrit.extend(row["sanskrit_names"])
            part_key = (row["plant_part_original"].casefold(), row["plant_part_standardised"].casefold(), row["file"])
            if any(part_key[:2]) and part_key not in seen_parts:
                seen_parts.add(part_key)
                parts.append(
                    {
                        "original": row["plant_part_original"] or None,
                        "standardized": row["plant_part_standardised"] or None,
                        "formulation_id": row["formulation_id"] or None,
                        "file": row["file"],
                    }
                )
            public_forms.append(
                {
                    "formulation_id": row["formulation_id"] or None,
                    "formulation_name": row["formulation_name"] or None,
                    "kind": row["kind"],
                    "ingredient_name_original": row["ingredient_name_original"] or None,
                    "ingredient_name_standardised": row["ingredient_name_standardised"] or None,
                    "plant_name_standardised": row["plant_name_standardised"] or None,
                    "plant_part_original": row["plant_part_original"] or None,
                    "plant_part_standardised": row["plant_part_standardised"] or None,
                    "therapeutic_uses": row["therapeutic_uses"],
                    "references": row["references"] or None,
                    "file": row["file"],
                }
            )

        families = _unique([row["family"] for row in plants if row["family"]])
        result = _blank_result(status, None, files=tables["files"])
        result["warnings"] = tables["warnings"]
        result["query_names"] = sorted(query)
        result["plants"] = [_public_plant(row) for row in plants]
        result["sanskrit_names"] = _unique(sanskrit)
        result["synonyms"] = _unique([name for row in plants for name in row["synonyms"]])
        result["common_names"] = _unique([name for row in plants for name in row["common_names"]])
        result["family"] = families[0] if len(families) == 1 else None
        result["families"] = families
        result["plant_parts"] = parts
        result["formulation_count"] = len(public_forms)
        result["formulations_truncated"] = len(public_forms) > _FORMULATION_LIMIT
        result["formulations"] = public_forms[:_FORMULATION_LIMIT]
        result["phytochemical_count"] = len(phytochemicals)
        result["phytochemicals_truncated"] = len(phytochemicals) > _PHYTO_LIMIT
        result["phytochemicals"] = [
            {
                "phytochemical_identifier": row["phytochemical_identifier"] or None,
                "plant_part": row["plant_part"] or None,
                "plant_identifier": row["plant_identifier"] or None,
                "reference_identifier": row["reference_identifier"] or None,
                "file": row["file"],
            }
            for row in phytochemicals[:_PHYTO_LIMIT]
        ]
        return result

    def _tables(self, directory: Path, present: list[Path]) -> dict[str, Any]:
        signature = tuple(
            (path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in present
        )
        cache_key = str(directory.resolve())
        with _INDEX_LOCK:
            cached = _INDEX_CACHE.get(cache_key)
            if cached is not None and cached[0] == signature:
                return cached[1]
        built = _build_tables(present)
        with _INDEX_LOCK:
            _INDEX_CACHE[cache_key] = (signature, built)
        return built


def _public_plant(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "plant_identifier": row["plant_identifier"] or None,
        "scientific_name": row["scientific_name"] or None,
        "synonyms": row["synonyms"],
        "family": row["family"] or None,
        "common_names": row["common_names"],
        "kingdom": row["kingdom"] or None,
        "group": row["group"] or None,
        "systems_of_medicine": row["systems_of_medicine"],
        "file": PLANT_FILE,
    }


def _schema_problem(filename: str, fieldnames: list[str]) -> str | None:
    headers = {_norm_header(name): name for name in fieldnames}
    if filename == PLANT_FILE:
        if _pick(headers, "indian_medicinal_plant", "plant_name", "scientific_name") is None:
            return f"{filename}: missing Indian_Medicinal_plant column"
    elif filename in {SINGLE_FILE, POLY_FILE}:
        if _pick(headers, "plant_name_standardised", "indian_medicinal_plant", "plant_name") is None and _pick(
            headers, "ingredient_name_standardised"
        ) is None:
            return f"{filename}: missing Plant_name_standardised column"
    elif filename == PHYTO_FILE:
        if _pick(headers, "imppat_phytochemical_identifier", "phytochemical_identifier") is None and _pick(
            headers, "indian_medicinal_plant", "plant_name"
        ) is None:
            return f"{filename}: missing phytochemical columns"
    return None


def _build_tables(present: list[Path]) -> dict[str, Any]:
    plants: list[dict[str, Any]] = []
    formulations: list[dict[str, Any]] = []
    phytochemicals: list[dict[str, Any]] = []
    files: list[dict[str, str]] = []
    warnings: list[str] = []
    readable = 0
    data_rows = 0
    for path in present:
        fieldnames, rows, problem = _read_tsv(path)
        if problem:
            warnings.append(problem)
            continue
        schema = _schema_problem(path.name, fieldnames)
        if schema:
            warnings.append(schema)
            continue
        readable += 1
        data_rows += len(rows)
        files.append({"name": path.name, "retrieved_at": _file_timestamp(path)})
        if path.name == PLANT_FILE:
            plants.extend(_plant_rows(fieldnames, rows))
        elif path.name == SINGLE_FILE:
            formulations.extend(
                _formulation_rows(fieldnames, rows, kind="single_herbal", filename=path.name)
            )
        elif path.name == POLY_FILE:
            formulations.extend(
                _formulation_rows(fieldnames, rows, kind="polyherbal", filename=path.name)
            )
        elif path.name == PHYTO_FILE:
            phytochemicals.extend(_phyto_rows(fieldnames, rows, filename=path.name))
    return {
        "plants": plants,
        "formulations": formulations,
        "phytochemicals": phytochemicals,
        "files": files,
        "warnings": warnings,
        "readable": readable,
        "data_rows": data_rows,
    }


def _header_index(fieldnames: list[str]) -> dict[str, str]:
    return {_norm_header(key): key for key in fieldnames if key}


def _plant_rows(fieldnames: list[str], rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    headers = _header_index(fieldnames)
    name_col = _pick(headers, "indian_medicinal_plant", "plant_name", "scientific_name")
    if name_col is None:
        return []
    synonym_col = _pick(headers, "synonymous_names", "synonymous_name", "synonyms")
    out: list[dict[str, Any]] = []
    for row in rows:
        scientific = _cell(row, name_col)
        synonyms = _split_pipe(_cell(row, synonym_col))
        names = {_norm_name(scientific)}
        names.update(_norm_name(item) for item in synonyms)
        names.discard("")
        if not names:
            continue
        common_raw = _cell(row, _pick(headers, "common_name", "common_names"))
        common_names = _split_pipe(common_raw) if "|" in common_raw else ([common_raw] if common_raw else [])
        out.append(
            {
                "identity": _cell(row, _pick(headers, "plant_identifier")) or _norm_name(scientific),
                "plant_identifier": _cell(row, _pick(headers, "plant_identifier")),
                "scientific_name": scientific,
                "synonyms": _unique(synonyms),
                "names": names,
                "family": _cell(row, _pick(headers, "family")),
                "common_names": _unique(common_names),
                "kingdom": _cell(row, _pick(headers, "kingdom")),
                "group": _cell(row, _pick(headers, "group")),
                "systems_of_medicine": _split_comma(_cell(row, _pick(headers, "system_of_medicine", "systems_of_medicine"))),
            }
        )
    return out


def _formulation_rows(
    fieldnames: list[str], rows: list[dict[str, str]], *, kind: str, filename: str
) -> list[dict[str, Any]]:
    headers = _header_index(fieldnames)
    plant_col = _pick(headers, "plant_name_standardised", "indian_medicinal_plant", "plant_name")
    ingredient_std = _pick(headers, "ingredient_name_standardised")
    if plant_col is None and ingredient_std is None:
        return []
    ingredient_original = _pick_contains(headers, "ingredient_name", "original")
    part_original = _pick_contains(headers, "plant_part", "original")
    part_std = _pick(headers, "plant_part_standardised")
    formulation_name = _pick_contains(headers, "formulation_name", "original") or _pick_contains(
        headers, "formulation_name"
    )
    uses_std = _pick(headers, "therapeutic_uses_standardised")
    uses_original = None
    for key, original in headers.items():
        if key.startswith("therapeutic_uses") and key != "therapeutic_uses_standardised":
            uses_original = original
            break
    out: list[dict[str, Any]] = []
    for row in rows:
        plant_name = _cell(row, plant_col)
        ingredient_name = _cell(row, ingredient_std)
        match_names = {_norm_name(plant_name), _norm_name(ingredient_name)}
        match_names.discard("")
        if not match_names:
            continue
        original_ingredient = _cell(row, ingredient_original)
        original_formulation = _cell(row, formulation_name)
        sanskrit = _sanskrit_names(kind, original_ingredient, original_formulation, plant_name)
        uses = _split_pipe(_cell(row, uses_std) or _cell(row, uses_original))
        out.append(
            {
                "kind": kind,
                "file": filename,
                "formulation_id": _cell(row, _pick(headers, "formulation_identifier")),
                "formulation_name": original_formulation,
                "ingredient_name_original": original_ingredient,
                "ingredient_name_standardised": ingredient_name,
                "plant_name_standardised": plant_name,
                "plant_name": _norm_name(plant_name) or _norm_name(ingredient_name),
                "match_names": match_names,
                "plant_part_original": _cell(row, part_original),
                "plant_part_standardised": _cell(row, part_std),
                "therapeutic_uses": uses,
                "references": _cell(row, _pick(headers, "references", "reference")),
                "sanskrit_names": sanskrit,
            }
        )
    return out


def _sanskrit_names(kind: str, ingredient: str, formulation: str, latin: str) -> list[str]:
    """API drug titles and AFI ingredient titles are the Sanskrit/IAST fields.

    The plant table has no Sanskrit column. Latin binomials are left out.
    """
    chosen: list[str] = []
    if kind == "polyherbal" and ingredient:
        chosen.append(ingredient)
    if kind == "single_herbal" and formulation:
        chosen.append(formulation)
    for value in (ingredient, formulation):
        if value and any(ord(char) > 127 for char in value):
            chosen.append(value)
    latin_key = _norm_name(latin)
    return _unique([value for value in chosen if _norm_name(value) and _norm_name(value) != latin_key])


def _phyto_rows(fieldnames: list[str], rows: list[dict[str, str]], *, filename: str) -> list[dict[str, str]]:
    headers = _header_index(fieldnames)
    ident = _pick(headers, "imppat_phytochemical_identifier", "phytochemical_identifier")
    plant = _pick(headers, "indian_medicinal_plant", "plant_name")
    if ident is None and plant is None:
        return []
    part_col = _pick(headers, "plant_part") or _pick(headers, "plant_part_standardised")
    out: list[dict[str, str]] = []
    for row in rows:
        out.append(
            {
                "plant_identifier": _cell(row, _pick(headers, "plant_identifier")),
                "plant_name": _norm_name(_cell(row, plant)),
                "plant_part": _cell(row, part_col),
                "phytochemical_identifier": _cell(row, ident),
                "reference_identifier": _cell(row, _pick(headers, "reference_identifier", "reference")),
                "file": filename,
            }
        )
    return out

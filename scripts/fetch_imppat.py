#!/usr/bin/env python3
"""Download selected IMPPAT 3.0 TSVs into a local gitignored cache.

Prints the license and attribution notice, then downloads. The batch files
are not committed. ``--accept-noncommercial-license`` is accepted and ignored
so older commands still run.

Usage:
    python scripts/fetch_imppat.py
    python scripts/fetch_imppat.py --cache-dir ~/.cache/herbenzo/imppat
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTICE_PATH = REPO_ROOT / "data" / "external" / "imppat" / "LICENSE_NOTICE.md"
DEFAULT_CACHE = REPO_ROOT / "data" / "external" / "imppat" / "cache"
BASE_URL = "https://cb.imsc.res.in/imppat/images/Batch_Download/"
USER_AGENT = (
    "herbenzo-imppat-fetch/0.1 "
    "(local cache; +https://cb.imsc.res.in/imppat/)"
)

# Taxonomy / formulation / plant-phytochemical tables named for local evaluation.
# Structures, targets, and bioactivity dumps are intentionally omitted.
CHOSEN_FILES = (
    "Plant_Information_IMPPAT.tsv",
    "IMPPAT_SingleHerbalFormulations.tsv",
    "IMPPAT_PolyHerbalFormulations.tsv",
    "IMPPAT_Phytochemical_Plant_Association.tsv",
)

FALLBACK_NOTICE = """\
IMPPAT is licensed under Creative Commons Attribution-NonCommercial-NoDerivatives
4.0 International License (https://creativecommons.org/licenses/by-nc-nd/4.0/).
Attribute IMPPAT and cite the three IMPPAT papers. Do not commit or redistribute
the batch files. See data/external/imppat/LICENSE_NOTICE.md.
"""


def license_notice_text() -> str:
    if NOTICE_PATH.is_file():
        return NOTICE_PATH.read_text(encoding="utf-8").rstrip() + "\n"
    return FALLBACK_NOTICE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download selected IMPPAT 3.0 TSVs into a local cache. "
            "Prints the license notice and does not commit the files."
        )
    )
    parser.add_argument(
        "--accept-noncommercial-license",
        action="store_true",
        help="Accepted for compatibility. Does not change the download.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"Directory for the TSV cache (default: {DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="FILENAME",
        help="Download one chosen filename. Repeat to select several. Default: all chosen TSVs.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Per-file socket timeout in seconds (default: 180).",
    )
    return parser


def selected_files(only: list[str] | None) -> tuple[str, ...]:
    if not only:
        return CHOSEN_FILES
    unknown = [name for name in only if name not in CHOSEN_FILES]
    if unknown:
        known = ", ".join(CHOSEN_FILES)
        raise ValueError(f"Unknown IMPPAT file(s): {', '.join(unknown)}. Choose from: {known}")
    return tuple(only)


def _looks_like_html(chunk: bytes) -> bool:
    head = chunk.lstrip()[:64].lower()
    return head.startswith((b"<!doctype", b"<html", b"<head", b"<body"))


def download_file(name: str, dest: Path, timeout: float) -> int:
    """Stream one batch TSV to ``dest``. Returns the number of bytes written."""
    url = BASE_URL + name
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/tab-separated-values, text/plain, */*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RuntimeError(f"{url} returned HTTP {status}")
            total = 0
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    if total == 0 and _looks_like_html(chunk):
                        raise RuntimeError(f"{url} returned HTML instead of a TSV")
                    handle.write(chunk)
                    total += len(chunk)
    except urllib.error.URLError as exc:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    if total == 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{url} returned an empty body")
    partial.replace(dest)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    notice = license_notice_text()

    try:
        names = selected_files(args.only)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    print(notice)
    print(
        "License and attribution notice. "
        "Files are written only to the local cache and are not committed. "
        "Do not redistribute the batch files.",
    )
    cache_dir = args.cache_dir.expanduser()
    for name in names:
        dest = cache_dir / name
        try:
            nbytes = download_file(name, dest, timeout=args.timeout)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"wrote {nbytes} bytes to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

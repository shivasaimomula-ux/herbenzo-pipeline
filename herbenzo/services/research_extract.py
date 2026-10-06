"""Pull constituent names, pathways, and unverified citations out of text.

The extractor is a heuristic over titles and abstracts. It does not assign
PubChem identifiers. Callers resolve names separately and drop anything a
model invented.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "constituent_names",
    "pathway_mentions",
    "strip_unverified_citations",
]

_STOP = frozenset(
    {
        "protein",
        "proteins",
        "information",
        "administration",
        "mechanism",
        "pathway",
        "pharmacology",
        "phytochemistry",
        "constituent",
        "constituents",
        "compound",
        "compounds",
        "extract",
        "extracts",
        "activity",
        "activities",
        "treatment",
        "review",
        "human",
        "animal",
        "plant",
        "plants",
        "species",
        "flower",
        "root",
        "study",
        "studies",
        "effect",
        "effects",
        "acid",
        "glucoside",
    }
)

# Specific anthocyanin names before the bare class word.
_SPECIFIC = re.compile(r"\b(ternatin\s+[A-D]\d)\b", re.IGNORECASE)
_CLASS = re.compile(r"\b(ternatins?)\b", re.IGNORECASE)
_GLYCOSIDE = re.compile(
    r"\b([A-Za-z][A-Za-z\-]*(?:idin|etin|in|ol|one|oside)\s+\d+-[A-Za-z0-9\-]*glucoside)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"\b([A-Za-z][A-Za-z\-]{3,}(?:in|ine|ol|one|oside|idin|etin))\b")
_PATH = re.compile(
    r"\b((?:NF-κB|NF-kB|iNOS|COX-2|acetylcholine|cholinergic|TNFR1)"
    r"(?:\s+(?:pathway|signaling|signalling))?)\b",
    re.IGNORECASE,
)
_PMID = re.compile(r"\b(?:pmid[:\s]*)?(\d{5,9})\b", re.IGNORECASE)
_CID = re.compile(r"\bCID[:\s]*(\d{2,10})\b", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s)>\"]+")


def constituent_names(*texts: str) -> list[dict[str, str]]:
    """Return unique names, most specific first. No identifiers are invented."""
    blob = "\n".join(text for text in texts if text)
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, kind: str) -> None:
        cleaned = " ".join(name.split())
        if not cleaned:
            return
        key = cleaned.casefold()
        if key in _STOP or key in seen:
            return
        if len(cleaned) < 4:
            return
        seen.add(key)
        ordered.append((cleaned, kind))

    for match in _SPECIFIC.finditer(blob):
        add(match.group(1), "specific")
    for match in _GLYCOSIDE.finditer(blob):
        add(match.group(1), "glycoside")
    for match in _WORD.finditer(blob):
        add(match.group(1), "name")
    for match in _CLASS.finditer(blob):
        token = match.group(1)
        # The plural class name is the ambiguous bare "ternatin", not a specific CID.
        add("ternatin" if token.casefold().startswith("ternatin") else token, "class")
    return [{"name": name, "kind": kind} for name, kind in ordered]


def pathway_mentions(*texts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    blob = "\n".join(text for text in texts if text)
    for match in _PATH.finditer(blob):
        label = " ".join(match.group(1).split())
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(label)
    return found


def strip_unverified_citations(
    narrative: str | None,
    *,
    allowed_pmids: set[str],
    allowed_cids: set[str],
    allowed_urls: set[str],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Drop PMIDs, CIDs, and URLs the model wrote that no tool returned."""
    if not isinstance(narrative, str) or not narrative.strip():
        return narrative if isinstance(narrative, str) else None, []
    ignored: list[dict[str, Any]] = []
    text = narrative

    def drop(pattern: re.Pattern[str], kind: str, allowed: set[str]) -> None:
        nonlocal text

        def repl(match: re.Match[str]) -> str:
            token = match.group(1) if match.lastindex else match.group(0)
            value = token.rstrip(".,;")
            if kind == "url":
                value = match.group(0).rstrip(".,;")
                ok = value in allowed
            else:
                ok = value in allowed
            if ok:
                return match.group(0)
            ignored.append({"kind": kind, "value": value, "reason": "not returned by a tool"})
            return ""

        text = pattern.sub(repl, text)

    drop(_URL, "url", allowed_urls)
    drop(_CID, "cid", allowed_cids)
    # PMIDs that are also CIDs we already judged stay if they were tool PMIDs.
    drop(_PMID, "pmid", allowed_pmids)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text or None, ignored

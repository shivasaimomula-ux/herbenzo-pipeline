"""Evidence Retrieval & Cache — the shared service every stage draws literature from.

Wraps the PubMed client with the two behaviours the architecture requires:

* **Retrieval date and source snapshot recorded per record**, so a generated
  document can carry a "literature current as of" stamp.
* **Absence is a result.** A search returning zero records is recorded and
  reported as evidence of absence, not swallowed as a failure. Several of the
  most important findings in this project were zero-record searches.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib

from herbenzo.clients.pubmed import Article, PubMedClient

__all__ = ["EvidenceStore", "SearchResult"]


class SearchResult(dict):
    @property
    def is_absent(self) -> bool:
        """True when PubMed indexes nothing for this query."""
        return self["total"] == 0


class EvidenceStore:
    def __init__(
        self,
        client: PubMedClient | None = None,
        manifest_path: str | pathlib.Path = "cache/evidence_manifest.json",
    ) -> None:
        self.client = client or PubMedClient()
        self.manifest_path = pathlib.Path(manifest_path)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._manifest: dict = (
            json.loads(self.manifest_path.read_text())
            if self.manifest_path.exists()
            else {"searches": {}, "records": {}}
        )

    # -- retrieval ----------------------------------------------------------

    def search(self, query: str, max_results: int = 20) -> SearchResult:
        res = self.client.search(query, max_results=max_results)
        stamp = _now()
        self._manifest["searches"][query] = {
            "total": res["total"],
            "pmids": res["pmids"],
            "retrieved_at": stamp,
            "absent": res["total"] == 0,
        }
        self._flush()
        return SearchResult({**res, "retrieved_at": stamp})

    def records(self, pmids: list[str]) -> dict[str, Article]:
        arts = self.client.fetch(pmids)
        stamp = _now()
        for pmid, a in arts.items():
            self._manifest["records"][pmid] = {
                "retrieved_at": stamp,
                "year": a.get("year"),
                "doi": a.get("doi"),
                "retracted": a.get("retracted", False),
                "evidence_tier": a.evidence_tier,
            }
        self._flush()
        return arts

    def record(self, pmid: str) -> Article | None:
        return self.records([pmid]).get(str(pmid))

    # -- governance ---------------------------------------------------------

    def absent_queries(self) -> list[str]:
        """Queries that returned nothing — the declarable evidence gaps."""
        return [q for q, m in self._manifest["searches"].items() if m["absent"]]

    def retracted_pmids(self) -> list[str]:
        return [p for p, m in self._manifest["records"].items() if m["retracted"]]

    def literature_current_as_of(self) -> str | None:
        stamps = [m["retrieved_at"] for m in self._manifest["records"].values()]
        return min(stamps) if stamps else None

    def _flush(self) -> None:
        self.manifest_path.write_text(json.dumps(self._manifest, indent=1))


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")

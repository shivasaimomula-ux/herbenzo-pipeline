"""PubChem PUG-REST client.

Replaces the hosted chemistry connector used during development. Same role:
resolve a marker compound name to a CID and retrieve computed descriptors.

PubChem asks that automated clients stay under 5 requests/second; this client
rate-limits itself and caches every response to disk, so a repeated pipeline run
issues no network traffic at all.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["PubChemClient", "PubChemError", "PROPERTY_FIELDS"]

_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

#: HTTP codes PubChem uses for transient load conditions — retry these.
_RETRY_CODES = {429, 500, 502, 503, 504}

PROPERTY_FIELDS = [
    "MolecularFormula", "MolecularWeight", "CanonicalSMILES", "InChIKey",
    "XLogP", "TPSA", "HBondDonorCount", "HBondAcceptorCount",
    "RotatableBondCount", "Complexity", "HeavyAtomCount",
]


class PubChemError(RuntimeError):
    """PubChem request failed or returned no usable record."""


class PubChemClient:
    def __init__(
        self,
        cache_dir: str | pathlib.Path = "cache/pubchem",
        min_interval_s: float = 0.25,
        timeout_s: float = 30.0,
        user_agent: str = "herbenzo-pipeline/1.0",
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
    ) -> None:
        self.cache_dir = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self._lock = threading.Lock()
        self._last_call = 0.0

    # -- transport ----------------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last_call
            if delta < self.min_interval_s:
                time.sleep(self.min_interval_s - delta)
            self._last_call = time.monotonic()

    def _get(self, path: str, cache_key: str) -> dict:
        cache_file = self.cache_dir / f"{cache_key}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())

        req = urllib.request.Request(
            f"{_BASE}/{path}", headers={"User-Agent": self.user_agent}
        )
        # PUGREST.ServerBusy (503) and rate limiting (429) are documented, expected
        # responses under load — back off and retry rather than failing the run.
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    payload = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise PubChemError(
                        f"PubChem has no record for {cache_key!r}"
                    ) from exc
                if exc.code not in _RETRY_CODES:
                    raise PubChemError(
                        f"PubChem HTTP {exc.code} for {cache_key!r}"
                    ) from exc
                last = exc
            except urllib.error.URLError as exc:
                last = exc
            time.sleep(self.backoff_base_s * (2 ** attempt))
        else:
            raise PubChemError(
                f"PubChem unavailable for {cache_key!r} after "
                f"{self.max_retries} attempts: {last}"
            ) from last

        cache_file.write_text(json.dumps(payload, indent=1))
        return payload

    # -- public API ---------------------------------------------------------

    def resolve_cid(self, name: str) -> int:
        """Resolve a compound name to its primary PubChem CID."""
        quoted = urllib.parse.quote(name, safe="")
        data = self._get(f"compound/name/{quoted}/cids/JSON", f"cid_{_slug(name)}")
        cids = (data.get("IdentifierList") or {}).get("CID") or []
        if not cids:
            raise PubChemError(f"no CID returned for {name!r}")
        return int(cids[0])

    def properties(self, cid: int) -> dict:
        """Computed descriptors for a CID."""
        fields = ",".join(PROPERTY_FIELDS)
        data = self._get(f"compound/cid/{cid}/property/{fields}/JSON", f"props_{cid}")
        rows = (data.get("PropertyTable") or {}).get("Properties") or []
        if not rows:
            raise PubChemError(f"no property record for CID {cid}")
        return rows[0]

    def descriptor_record(self, name: str) -> dict:
        """Resolve a marker name straight to the descriptor shape the registry stores."""
        cid = self.resolve_cid(name)
        p = self.properties(cid)
        xlogp = p.get("XLogP")
        return {
            "marker_name": name,
            "pubchem_cid": cid,
            "molecular_weight": float(p["MolecularWeight"]),
            "xlogp": None if xlogp is None else float(xlogp),
            "tpsa": float(p["TPSA"]),
            "hbd": int(p["HBondDonorCount"]),
            "hba": int(p["HBondAcceptorCount"]),
            "rotatable_bonds": int(p["RotatableBondCount"]),
            "source": f"PubChem PUG-REST CID {cid} (computed descriptors)",
        }


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower())[:80]

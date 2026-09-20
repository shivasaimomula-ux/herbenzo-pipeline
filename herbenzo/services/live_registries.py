"""Registries client backed by the cached descriptor file with a live fallback.

Order of resolution for a marker's physicochemical descriptors:

1. ``herbenzo/data/marker_properties.json`` — descriptors already retrieved.
2. PubChem PUG-REST, for any marker not yet in the file. The retrieved record is
   written back, so each marker costs one network call once, ever.

Set ``allow_network=False`` for a fully offline, deterministic run (CI, golden set).
"""

from __future__ import annotations

import json
import pathlib

from herbenzo.clients.pubchem import PubChemClient, PubChemError
from herbenzo.schemas.contracts import PhysicochemicalProfile
from herbenzo.services.registries import StaticRegistriesClient, UnknownMarker

__all__ = ["LiveRegistriesClient"]

_DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "marker_properties.json"


class LiveRegistriesClient(StaticRegistriesClient):
    def __init__(
        self,
        data_path: pathlib.Path | None = None,
        pubchem: PubChemClient | None = None,
        allow_network: bool = True,
    ) -> None:
        self.data_path = pathlib.Path(data_path or _DATA)
        super().__init__(self.data_path)
        self.pubchem = pubchem or PubChemClient()
        self.allow_network = allow_network

    def get_physicochemical_properties(self, marker_name: str) -> PhysicochemicalProfile:
        try:
            return super().get_physicochemical_properties(marker_name)
        except UnknownMarker:
            if not self.allow_network:
                raise
        record = self.pubchem.descriptor_record(marker_name)  # may raise PubChemError
        self._persist(record)
        self._props[marker_name] = record
        return super().get_physicochemical_properties(marker_name)

    def _persist(self, record: dict) -> None:
        data = json.loads(self.data_path.read_text())
        data[record["marker_name"]] = record
        self.data_path.write_text(json.dumps(data, indent=1, sort_keys=True))


__all__.append("PubChemError")

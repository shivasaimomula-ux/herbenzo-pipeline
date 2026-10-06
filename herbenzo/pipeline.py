"""Pipeline orchestrator.

Runs a FormulationSpec through Component B, gathers literature precedent for each
delivery recommendation, adjudicates every claim, and emits an auditable report.

Design rules this enforces, which are the whole point of the pipeline:

* Every claim carries a provenance ID and an adjudication verdict.
* Confidence only falls. A rejected or partial citation lowers the run's floor.
* A zero-record search is reported as a declared gap, not omitted.
* A per-run manifest records model-free reproducibility metadata: package
  version, retrieval dates, and the exact queries issued.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib

from herbenzo.components.modernizer.modernizer import ENGINE_VERSION, ModernizerEngine
from herbenzo.schemas.contracts import FormulationSpec, ModernizedSKU
from herbenzo.services.adjudication import AdjudicationService, Verdict
from herbenzo.services.evidence import EvidenceStore
from herbenzo.services.live_registries import LiveRegistriesClient

__all__ = ["Pipeline", "PIPELINE_VERSION"]

PIPELINE_VERSION = "herbenzo-pipeline/1.0.0"

#: Confidence penalty applied per unsupported delivery precedent.
_REJECT_PENALTY = 0.10
_PARTIAL_PENALTY = 0.05


class Pipeline:
    def __init__(
        self,
        registries: LiveRegistriesClient | None = None,
        evidence: EvidenceStore | None = None,
        adjudicator: AdjudicationService | None = None,
        allow_network: bool = True,
    ) -> None:
        self.registries = registries or LiveRegistriesClient(allow_network=allow_network)
        self.evidence = evidence or EvidenceStore()
        self.adjudicator = adjudicator or AdjudicationService(self.evidence)
        self.engine = ModernizerEngine(self.registries)
        self.allow_network = allow_network

    # -- main entry point ---------------------------------------------------

    def run(self, spec: FormulationSpec | dict, max_refs_per_claim: int = 4) -> dict:
        local_spec = spec if isinstance(spec, FormulationSpec) else FormulationSpec.model_validate(spec)
        sku = self.engine.modernize(local_spec)
        # Advisory only. Never an input to confidence penalties below.
        marker_gap = self.engine.classical_active_marker_gap
        started = _now()

        claims: list[dict] = []
        gaps: list[str] = []

        if sku is None:
            # Classical preparation, no registry marker on any ingredient.
            # Evidence and adjudication have nothing marker-backed to attach
            # to; the run still finishes and reports the indicator.
            confidence = round(float(local_spec.confidence), 4)
            return self._report(
                started=started,
                sku_payload=None,
                marker_gap=marker_gap,
                inherited=local_spec.confidence,
                after_modernization=confidence,
                after_adjudication=confidence,
                claims=claims,
                gaps=gaps,
            )

        # Shared-package outbound gate (Audit Finding #1 / Task T6).
        # The indicator is not part of this payload.
        from herbenzo.contract_gate import validate_outbound_modernized_sku

        validate_outbound_modernized_sku(sku)

        for ing in sku.ingredients:
            marker = ing.marker.marker_name
            delivery = ing.delivery.primary.value.replace("_", " ")

            # Claim 1 — the BCS classification itself (computed, not cited).
            claims.append(self._computed_claim(
                sku, ing,
                f"{marker} is provisionally BCS Class {ing.bcs.bcs_class.value} "
                f"({ing.bcs.solubility_call} solubility, "
                f"{ing.bcs.permeability_call} permeability).",
                basis=ing.bcs.evidence_basis, rationale=ing.bcs.rationale))

            # Claim 2 — the delivery recommendation, which needs literature.
            claim_text = (f"{delivery} improves the absorption of {marker}")
            # Search the isolated marker OR the whole botanical: delivery-system
            # work is frequently published against the extract, not the pure
            # compound, and a marker-only query misses it.
            query = (f'("{marker}" OR "{ing.botanical_name}") '
                     f"AND ({_delivery_query(ing.delivery.primary.value)})")
            if not self.allow_network:
                claims.append(self._uncited_claim(sku, ing, claim_text,
                                                  "offline run — not adjudicated"))
                continue

            search = self.evidence.search(query, max_results=max_refs_per_claim)
            if search.is_absent:
                gaps.append(query)
                claims.append(self._uncited_claim(
                    sku, ing, claim_text,
                    "No supporting literature identified. Claim downgraded: the "
                    "delivery system is recommended on classification grounds only."))
                continue

            adjs = self.adjudicator.adjudicate_many([
                dict(claim=claim_text, subject=marker,
                     subject_aliases=(ing.botanical_name,), pmid=p,
                     claim_domain="mechanism")
                for p in search["pmids"]
            ])
            for a in adjs:
                claims.append({
                    "claim_id": _claim_id(sku.sku_id, ing.ingredient_id, claim_text, a.pmid),
                    "ingredient_id": ing.ingredient_id,
                    "stage": "B:modernizer",
                    "claim": claim_text,
                    "pmid": a.pmid,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{a.pmid}/",
                    "title": a.title,
                    "verdict": a.verdict,
                    "reason_code": a.reason_code,
                    "evidence_tier": a.evidence_tier,
                    "model_system": a.model_system,
                    "note": a.note,
                })

        confidence = self._apply_penalties(sku, claims)
        return self._report(
            started=started,
            sku_payload=json.loads(sku.model_dump_json()),
            marker_gap=marker_gap,
            inherited=sku.inherited_confidence,
            after_modernization=sku.confidence,
            after_adjudication=confidence,
            claims=claims,
            gaps=gaps,
        )

    def _report(
        self,
        *,
        started: str,
        sku_payload: dict | None,
        marker_gap: dict | None,
        inherited: float,
        after_modernization: float,
        after_adjudication: float,
        claims: list[dict],
        gaps: list[str],
    ) -> dict:
        return {
            "manifest": {
                "pipeline_version": PIPELINE_VERSION,
                "engine_version": ENGINE_VERSION,
                "run_started": started,
                "run_finished": _now(),
                "offline": not self.allow_network,
                "literature_current_as_of": self.evidence.literature_current_as_of(),
                "retracted_sources_seen": self.evidence.retracted_pmids(),
            },
            "sku": sku_payload,
            "classical_active_marker_gap": marker_gap,
            "confidence": {
                "inherited_from_A": inherited,
                "after_modernization": after_modernization,
                "after_adjudication": after_adjudication,
                "rule": "may only decrease at each stage",
            },
            "claims": claims,
            "declared_gaps": gaps,
            "citation_summary": _summarise(claims),
        }

    # -- helpers ------------------------------------------------------------

    def _computed_claim(self, sku, ing, text, basis, rationale) -> dict:
        return {
            "claim_id": _claim_id(sku.sku_id, ing.ingredient_id, text, "computed"),
            "ingredient_id": ing.ingredient_id,
            "stage": "B:modernizer",
            "claim": text,
            "pmid": None,
            "verdict": "computed",
            "reason_code": basis,
            "evidence_tier": "computed_descriptors",
            "note": " ".join(rationale),
        }

    def _uncited_claim(self, sku, ing, text, note) -> dict:
        return {
            "claim_id": _claim_id(sku.sku_id, ing.ingredient_id, text, "uncited"),
            "ingredient_id": ing.ingredient_id,
            "stage": "B:modernizer",
            "claim": text,
            "pmid": None,
            "verdict": "unsupported",
            "reason_code": "no_supporting_literature",
            "evidence_tier": "none",
            "note": note,
        }

    @staticmethod
    def _apply_penalties(sku: ModernizedSKU, claims: list[dict]) -> float:
        # classical_active_marker_gap is intentionally not a penalty input.
        conf = sku.confidence
        for c in claims:
            if c["verdict"] == Verdict.REJECT:
                conf -= _REJECT_PENALTY
            elif c["verdict"] == Verdict.PARTIAL:
                conf -= _PARTIAL_PENALTY
        return round(max(0.0, min(conf, sku.confidence)), 4)


def _delivery_query(tech: str) -> str:
    return {
        "phytosome_phospholipid_complex": "phytosome OR phospholipid complex",
        "self_nanoemulsifying_drug_delivery_system": "SNEDDS OR self-nanoemulsifying",
        "nanoemulsion": "nanoemulsion",
        "liposome": "liposome OR liposomal",
        "cyclodextrin_inclusion_complex": "cyclodextrin",
        "amorphous_solid_dispersion": "solid dispersion",
        "polymeric_nanoparticle_encapsulation": "nanoparticle encapsulation",
        "permeation_enhancer_system": "permeation enhancer",
        "mucoadhesive_polymer_matrix": "mucoadhesive",
        "enteric_coated_delayed_release": "enteric coating",
        "conventional_powder_or_capsule": "bioavailability",
    }.get(tech, "bioavailability")


def _summarise(claims: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in claims:
        out[c["verdict"]] = out.get(c["verdict"], 0) + 1
    return out


def _claim_id(sku_id: str, ing_id: str, claim: str, suffix: str) -> str:
    h = hashlib.sha1(f"{sku_id}|{ing_id}|{claim}|{suffix}".encode()).hexdigest()[:10]
    return f"CLM-{h}"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")

"""Provisional BCS classification from marker descriptors.

What this is
------------
A transparent, deterministic rule set that assigns a *predicted* Biopharmaceutics
Classification System class to a standardization marker using retrieved
physicochemical descriptors, plus curated overrides for effects descriptors cannot
see. Every call returns its rationale, so a reviewer can see why a class was assigned.

What this is not
----------------
A regulatory BCS determination. That requires measured equilibrium solubility across
the physiological pH range at the highest dose strength, and human permeability or a
validated surrogate. ``BCSAssessment.is_regulatory_determination`` is hard-wired to
``False`` for this reason.

Known limitations of descriptor-based prediction
------------------------------------------------
* **Crystal-lattice-limited solubility.** Several polyphenols (quercetin and ellagic
  acid are the standard examples) have modest logP but very low aqueous solubility
  driven by lattice energy and intramolecular hydrogen bonding. The logP proxy will
  call them soluble. A measured solubility value overrides the proxy when supplied.
* **Efflux-limited permeability.** Descriptors model passive diffusion only. A
  compound that is a P-glycoprotein substrate can show excellent predicted
  permeability and poor observed absorption. Handled by curated override.
* **Volatile constituents.** Essential-oil markers are poorly represented by a
  solid-dose solubility model.

Where a classification rests on descriptors alone, confidence is capped and the
limitation is stated in the rationale rather than left implicit.
"""

from __future__ import annotations

from herbenzo.schemas.contracts import BCSAssessment, BCSClass, PhysicochemicalProfile
from herbenzo.services.records import MarkerRecord

__all__ = ["classify", "SOLUBILITY_LOGP_CUTOFF", "PERMEABILITY_TPSA_CUTOFF"]

#: logP at or above which a botanical marker is treated as poorly water-soluble.
SOLUBILITY_LOGP_CUTOFF = 3.0
#: Molecular weight above which solubility and passive permeability both degrade.
MW_CUTOFF = 500.0
#: Veber polar-surface-area ceiling for good oral permeability.
PERMEABILITY_TPSA_CUTOFF = 140.0
#: Lipinski hydrogen-bond-donor ceiling.
HBD_CUTOFF = 5
#: Veber rotatable-bond ceiling.
ROTB_CUTOFF = 10
#: Measured aqueous solubility below which a compound is called low-solubility.
MEASURED_LOW_SOLUBILITY_MG_PER_ML = 0.1

_CONF_DESCRIPTORS_ONLY = 0.60
_CONF_MEASURED = 0.80
_CONF_CURATED_OVERRIDE = 0.70


def _solubility_call(p: PhysicochemicalProfile) -> tuple[str, list[str], bool]:
    """Return (call, rationale lines, used_measured)."""
    lines: list[str] = []
    if p.measured_solubility_mg_per_ml is not None:
        low = p.measured_solubility_mg_per_ml < MEASURED_LOW_SOLUBILITY_MG_PER_ML
        lines.append(
            f"Measured aqueous solubility {p.measured_solubility_mg_per_ml} mg/mL "
            f"(PMID {p.solubility_source_pmid}) → "
            f"{'low' if low else 'high'} solubility."
        )
        return ("low" if low else "high"), lines, True

    reasons: list[str] = []
    if p.xlogp is not None and p.xlogp >= SOLUBILITY_LOGP_CUTOFF:
        reasons.append(f"XLogP {p.xlogp} ≥ {SOLUBILITY_LOGP_CUTOFF}")
    if p.molecular_weight > MW_CUTOFF:
        reasons.append(f"MW {p.molecular_weight} > {MW_CUTOFF}")

    if reasons:
        lines.append("Low solubility predicted: " + "; ".join(reasons) + ".")
        call = "low"
    else:
        if p.xlogp is None:
            lines.append(
                f"High solubility predicted from MW {p.molecular_weight} ≤ {MW_CUTOFF}. "
                "XLogP was not reported."
            )
        else:
            lines.append(
                f"High solubility predicted: XLogP {p.xlogp} < {SOLUBILITY_LOGP_CUTOFF} "
                f"and MW {p.molecular_weight} ≤ {MW_CUTOFF}."
            )
        call = "high"
    lines.append(
        "Solubility is inferred from computed descriptors; lattice-limited solubility "
        "is not captured. A measured value is required to confirm."
    )
    return call, lines, False


def _permeability_call(p: PhysicochemicalProfile) -> tuple[str, list[str]]:
    failures: list[str] = []
    if p.tpsa > PERMEABILITY_TPSA_CUTOFF:
        failures.append(f"TPSA {p.tpsa} > {PERMEABILITY_TPSA_CUTOFF}")
    if p.hbd > HBD_CUTOFF:
        failures.append(f"HBD {p.hbd} > {HBD_CUTOFF}")
    if p.rotatable_bonds > ROTB_CUTOFF:
        failures.append(f"rotatable bonds {p.rotatable_bonds} > {ROTB_CUTOFF}")
    if p.molecular_weight > MW_CUTOFF:
        failures.append(f"MW {p.molecular_weight} > {MW_CUTOFF}")

    if failures:
        return "low", [
            "Low passive permeability predicted: " + "; ".join(failures) + "."
        ]
    return "high", [
        f"High passive permeability predicted: TPSA {p.tpsa} ≤ {PERMEABILITY_TPSA_CUTOFF}, "
        f"HBD {p.hbd} ≤ {HBD_CUTOFF}, MW {p.molecular_weight} ≤ {MW_CUTOFF}, "
        f"rotatable bonds {p.rotatable_bonds} ≤ {ROTB_CUTOFF}."
    ]


_MATRIX = {
    ("high", "high"): BCSClass.I,
    ("low", "high"): BCSClass.II,
    ("high", "low"): BCSClass.III,
    ("low", "low"): BCSClass.IV,
}


def classify(
    properties: PhysicochemicalProfile,
    marker: MarkerRecord | None = None,
) -> BCSAssessment:
    """Assign a provisional BCS class to a marker compound."""
    sol, sol_lines, used_measured = _solubility_call(properties)
    perm, perm_lines = _permeability_call(properties)
    rationale = [*sol_lines, *perm_lines]

    basis: str = "measured_solubility" if used_measured else "computed_descriptors"
    confidence = _CONF_MEASURED if used_measured else _CONF_DESCRIPTORS_ONLY

    # Curated override: efflux transport is invisible to descriptors.
    if marker is not None and marker.efflux_substrate and perm == "high":
        perm = "low"
        basis = "curated_override"
        confidence = _CONF_CURATED_OVERRIDE
        rationale.append(
            f"Permeability call overridden to low: {marker.marker_name} is a curated "
            f"substrate of {marker.efflux_transporter or 'an efflux transporter'}. "
            "Descriptor models represent passive diffusion only and cannot see "
            "efflux-limited absorption."
        )
        if marker.evidence_note:
            rationale.append(f"Override note: {marker.evidence_note}")

    if properties.xlogp is None:
        confidence = min(confidence, 0.45)
        rationale.append("XLogP unavailable for this compound; solubility call is weakened.")

    return BCSAssessment(
        bcs_class=_MATRIX[(sol, perm)],
        solubility_call=sol,  # type: ignore[arg-type]
        permeability_call=perm,  # type: ignore[arg-type]
        evidence_basis=basis,  # type: ignore[arg-type]
        rationale=rationale,
        confidence=confidence,
    )

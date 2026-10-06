"""Delivery-system selection matrix.

Maps a provisional BCS class plus curated physicochemical barriers onto candidate
delivery technologies, and names the excipients each carrier implies.

Two invariants are enforced structurally rather than by convention:

* **The active is never modified.** Every recommendation changes the *carrier*, not
  the herbal active. ``DeliveryRecommendation.active_unchanged`` is hard-wired True.
* **No uncited enhancement number.** A bioavailability fold-change can only be
  attached when a citation, evidence tier and model system accompany it — the
  contract rejects a bare number. With no citation the engine emits a qualitative
  expectation instead. This is deliberate: asserting "≈2× absorption" for a
  formulation that has never been manufactured and measured is the single most
  common unverifiable claim in this product category.
"""

from __future__ import annotations

from herbenzo.schemas.contracts import (
    BCSAssessment,
    BCSClass,
    BioavailabilityEvidence,
    DeliveryRecommendation,
    DeliveryTechnology as DT,
)
from herbenzo.services.records import MarkerRecord

__all__ = ["recommend", "EXCIPIENTS"]

#: Representative excipients per carrier. Grades and levels are formulation-development
#: decisions; these name the functional class, not a finished formula.
EXCIPIENTS: dict[DT, list[str]] = {
    DT.CONVENTIONAL: ["microcrystalline cellulose (diluent)", "magnesium stearate (lubricant)"],
    DT.PHYTOSOME: [
        "phosphatidylcholine (sunflower or soy lecithin) — complexing lipid",
        "microcrystalline cellulose (carrier)",
    ],
    DT.SNEDDS: [
        "medium-chain triglyceride oil (lipid phase)",
        "non-ionic surfactant, e.g. polysorbate 80",
        "co-solvent, e.g. propylene glycol",
    ],
    DT.NANOEMULSION: [
        "medium-chain triglyceride oil (oil phase)",
        "non-ionic surfactant, e.g. polysorbate 80",
        "glycerol (co-surfactant)",
    ],
    DT.LIPOSOME: ["phosphatidylcholine (bilayer former)", "cholesterol (membrane stabilizer)"],
    DT.CYCLODEXTRIN: ["hydroxypropyl-beta-cyclodextrin (complexing agent)"],
    DT.SOLID_DISPERSION: [
        "polymeric carrier, e.g. povidone K30 or hypromellose acetate succinate",
        "surfactant for wetting",
    ],
    DT.NANOPARTICLE: [
        "biodegradable polymer, e.g. PLGA or chitosan",
        "poloxamer (steric stabilizer)",
    ],
    DT.PERMEATION_ENHANCER: [
        "medium-chain fatty acid salt, e.g. sodium caprate",
        "chitosan (tight-junction modulator)",
    ],
    DT.MUCOADHESIVE: ["carbomer (mucoadhesive polymer)", "hypromellose (matrix former)"],
    DT.ENTERIC: [
        "methacrylic acid copolymer or hypromellose acetate succinate (enteric film)",
        "triethyl citrate (plasticizer)",
    ],
}

_MATRIX: dict[BCSClass, tuple[DT, list[DT], str]] = {
    BCSClass.I: (
        DT.CONVENTIONAL,
        [],
        "Class I (high solubility, high permeability): absorption is not the limiting "
        "step. No solubility- or permeability-enhancing carrier is indicated; adding "
        "one would raise cost without a defensible rationale.",
    ),
    BCSClass.II: (
        DT.PHYTOSOME,
        [DT.SNEDDS, DT.NANOEMULSION, DT.SOLID_DISPERSION, DT.CYCLODEXTRIN],
        "Class II (low solubility, high permeability): dissolution is rate-limiting. "
        "Lipid-based and solubility-enhancing carriers address the limiting step "
        "without altering the active.",
    ),
    BCSClass.III: (
        DT.PERMEATION_ENHANCER,
        [DT.MUCOADHESIVE],
        "Class III (high solubility, low permeability): membrane transport is "
        "rate-limiting. Transient permeation enhancement and prolonged mucosal "
        "residence target that step; solubilization would not help.",
    ),
    BCSClass.IV: (
        DT.PHYTOSOME,
        [DT.NANOPARTICLE, DT.SNEDDS, DT.LIPOSOME],
        "Class IV (low solubility, low permeability): both steps are limiting. "
        "Phospholipid complexation and nanoparticulate encapsulation address "
        "solubility and membrane transit together.",
    ),
}


def recommend(
    assessment: BCSAssessment,
    marker: MarkerRecord | None = None,
    bioavailability: BioavailabilityEvidence | None = None,
) -> DeliveryRecommendation:
    """Select a delivery system for a classified marker.

    ``bioavailability`` may carry a cited fold-change retrieved and adjudicated
    upstream. When omitted, a qualitative expectation is emitted instead of a number.
    """
    primary, alternatives, rationale_text = _MATRIX[assessment.bcs_class]
    alternatives = list(alternatives)
    rationale = [rationale_text]

    if marker is not None and marker.efflux_substrate:
        for tech in (DT.NANOPARTICLE, DT.LIPOSOME):
            if tech not in alternatives and tech != primary:
                alternatives.append(tech)
        rationale.append(
            f"{marker.marker_name} is a curated substrate of "
            f"{marker.efflux_transporter or 'an efflux transporter'}; carrier selection "
            "should additionally aim to reduce efflux exposure (lipid or particulate "
            "encapsulation). Any co-formulated efflux inhibitor is a drug-interaction "
            "risk and must be assessed in the safety section, not treated as a "
            "formulation convenience."
        )

    if marker is not None and marker.acid_labile:
        if DT.ENTERIC not in alternatives:
            alternatives.insert(0, DT.ENTERIC)
        rationale.append(
            f"{marker.marker_name} is flagged acid-labile; an enteric or delayed-release "
            "presentation is required to protect the active through the gastric phase."
        )

    if assessment.evidence_basis == "computed_descriptors":
        rationale.append(
            "Classification rests on computed descriptors alone, so this recommendation "
            "is a formulation-development starting point, not a validated design. "
            "Confirm with measured solubility and a dissolution or permeability study."
        )

    if bioavailability is None:
        if assessment.bcs_class is BCSClass.I:
            expectation = (
                "No bioavailability enhancement is indicated for a Class I marker; "
                "none is claimed."
            )
        else:
            expectation = (
                "An absorption improvement is the rationale for this carrier, but no "
                "magnitude is claimed: no study of this marker in this delivery system "
                "has been supplied. Quantify only against a retrieved, adjudicated "
                "citation for the same marker and the same delivery system."
            )
        bioavailability = BioavailabilityEvidence(qualitative_expectation=expectation)
    elif bioavailability.applies_to_same_delivery_system is False:
        rationale.append(
            "Cited bioavailability evidence was measured in a different delivery "
            "system; it supports the principle, not this specific formulation."
        )

    return DeliveryRecommendation(
        primary=primary,
        alternatives=alternatives,
        rationale=rationale,
        excipients=EXCIPIENTS[primary],
        bioavailability=bioavailability,
    )

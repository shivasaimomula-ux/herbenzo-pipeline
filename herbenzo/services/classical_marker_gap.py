"""Advisory indicator for classical Ayurvedic forms with no active-marker assignment.

The signal is a gap already stored on the ingredient registry: ``markers`` is
empty. This module does not invent a marker, a physicochemical profile, or a
pharmacological claim.

A hit is indicator-only. Callers must keep modernization, evidence retrieval,
and citation adjudication running, and must not treat the indicator as a
confidence-floor violation.

herbenzo-contracts has no field for this yet. The pipeline report carries it
*beside* ``sku`` (``classical_active_marker_gap``). The HTTP body adds the same
object only after outbound contract validation, so a marker-backed
ModernizedSKU still validates. When contracts grow an optional field, it can
move onto the SKU without changing this detection.
"""

from __future__ import annotations

import re

from herbenzo.schemas.contracts import FormulationSpec

__all__ = [
    "CLASSICAL_PREPARATION_FORMS",
    "INDICATOR_CODE",
    "classical_active_marker_gap",
    "matched_classical_forms",
]

INDICATOR_CODE = "classical_active_marker_gap"

#: Dosage-form names only — orthography of classical preparations, not a
#: constituent or pharmacology taxonomy. ``churna`` is included because the
#: Triphala example already uses that form name.
CLASSICAL_PREPARATION_FORMS: tuple[str, ...] = (
    "decoction",
    "kwath",
    "kwatha",
    "kashaya",
    "kashayam",
    "quath",
    "lehya",
    "lehyam",
    "leha",
    "avaleha",
    "bhasma",
    "churna",
    "arishta",
    "asava",
    "asavam",
    "ghrita",
    "taila",
    "tailam",
    "gutika",
    "vati",
)

_FORM_SET = frozenset(CLASSICAL_PREPARATION_FORMS)
_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)

_GAP_REASON = "no standardization marker assigned in the ingredient registry"

_MESSAGE = (
    "Classical Ayurvedic preparation has no established active-marker data "
    "in the ingredient registry. Indicator only: modernization, evidence "
    "retrieval, and citation adjudication are not stopped, and this is not "
    "a confidence-floor violation."
)


def matched_classical_forms(dosage_form: str, product_name: str) -> tuple[str, ...]:
    """Return recognized classical form names mentioned by the preparation.

    Matches whole tokens in ``dosage_form`` and ``product_name`` (case
    insensitive), plus a single trailing plural ``s``. Does not scan
    botanical names or plant parts.
    """
    found: set[str] = set()
    for match in _TOKEN.finditer(f"{dosage_form} {product_name}"):
        token = match.group(0).lower()
        if token in _FORM_SET:
            found.add(token)
        elif (
            len(token) > 4
            and token.endswith("s")
            and not token.endswith("ss")
            and token[:-1] in _FORM_SET
        ):
            found.add(token[:-1])
    return tuple(form for form in CLASSICAL_PREPARATION_FORMS if form in found)


def classical_active_marker_gap(
    spec: FormulationSpec,
    registries,
) -> dict | None:
    """Build the advisory payload, or ``None`` when the indicator does not apply.

    ``registries`` is anything with ``lookup_ingredient``. Unknown ingredient
    IDs still raise ``UnknownIngredient`` — missing identity is not this
    indicator. A named marker whose physicochemical cache is empty is also
    not this indicator; that remains ``UnknownMarker`` from descriptor lookup.
    """
    forms = matched_classical_forms(spec.dosage_form, spec.product_name)
    if not forms:
        return None

    gaps: list[dict] = []
    for ing in spec.ingredients:
        record = registries.lookup_ingredient(ing.ingredient_id)
        if record.markers:
            continue
        gaps.append(
            {
                "ingredient_id": ing.ingredient_id,
                "botanical_name": ing.botanical_name,
                "reason": _GAP_REASON,
            }
        )
    if not gaps:
        return None

    return {
        "code": INDICATOR_CODE,
        "present": True,
        "advisory_only": True,
        "blocking": False,
        "affects_confidence_floor": False,
        "matched_forms": list(forms),
        "formulation_id": spec.formulation_id,
        "product_name": spec.product_name,
        "dosage_form": spec.dosage_form,
        "message": _MESSAGE,
        "ingredients": gaps,
    }

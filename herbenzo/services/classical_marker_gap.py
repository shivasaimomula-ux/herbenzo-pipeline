"""Advisory indicator for an approved ingredient with no standardization marker.

The signal is an empty marker list on this request's approval. It is not a
registry miss. This module does not invent a marker, a physicochemical
profile, or a pharmacological claim.

A hit is indicator-only. Callers must keep modernization of marker-backed
ingredients running, and must not treat the indicator as a confidence-floor
violation. The code name stays ``classical_active_marker_gap`` so existing
readers keep working; the flag now also covers non-classical forms.

herbenzo-contracts has no field for this yet. The pipeline report carries it
*beside* ``sku``. The HTTP body adds the same object only after outbound
contract validation.
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

_GAP_REASON = "marker pending: no PubChem standardization marker on this approved ingredient"

_HOW_TO_ADD = (
    "Add one by adjudication: POST /research/marker with this request's approval "
    "and marker_name set to a specific compound (for Clitoria ternatea, Ternatin A1 or clitorin). "
    "Then POST /modernize again with that updated approval. A name alone is not enough."
)

_MESSAGE_NO_SKU = (
    "No ModernizedSKU because the marker is pending. "
    "This approved ingredient has no PubChem standardization marker, so no chemistry-backed SKU was built. "
    "The indicator does not change the confidence floor. "
    + _HOW_TO_ADD
)

_MESSAGE_PARTIAL = (
    "Marker pending on some approved ingredients, so those ingredients were left out of the SKU. "
    "Marker-backed ingredients were still modernized. The indicator does not change the confidence floor. "
    + _HOW_TO_ADD
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
    """Build the advisory payload, or ``None`` when every ingredient has a marker.

    ``registries`` is anything with ``lookup_ingredient``. An id that is not on
    the approval snapshot still raises. A named marker whose descriptor block
    is missing is not this indicator; descriptor lookup raises ``UnknownMarker``.
    """
    forms = matched_classical_forms(spec.dosage_form, spec.product_name)

    gaps: list[dict] = []
    for ing in spec.ingredients:
        record = registries.lookup_ingredient(ing.ingredient_id)
        if record.markers:
            continue
        gaps.append(
            {
                "ingredient_id": ing.ingredient_id,
                "botanical_name": ing.botanical_name,
                "marker_status": "pending",
                "reason": _GAP_REASON,
            }
        )
    if not gaps:
        return None

    all_pending = len(gaps) == len(spec.ingredients)
    return {
        "code": INDICATOR_CODE,
        "present": True,
        "advisory_only": True,
        "blocking": False,
        "affects_confidence_floor": False,
        "marker_status": "pending",
        "matched_forms": list(forms),
        "formulation_id": spec.formulation_id,
        "product_name": spec.product_name,
        "dosage_form": spec.dosage_form,
        "message": _MESSAGE_NO_SKU if all_pending else _MESSAGE_PARTIAL,
        "how_to_add_marker": {
            "method": "POST",
            "path": "/research/marker",
            "fields": ["approval", "marker_name"],
            "note": _HOW_TO_ADD,
        },
        "ingredients": gaps,
    }

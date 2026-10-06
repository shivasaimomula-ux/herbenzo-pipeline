"""Command-line entry point.

    python -m herbenzo.cli run examples/ashwagandha.json -o out/report.json
    python -m herbenzo.cli run examples/triphala.json --offline
    python -m herbenzo.cli adjudicate --pmid 37257749 \
        --subject "Terminalia bellirica" --claim "well tolerated orally" --domain safety
    python -m herbenzo.cli markers
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from herbenzo.pipeline import Pipeline
from herbenzo.services.adjudication import AdjudicationService
from herbenzo.services.registries import _INGREDIENTS, StaticRegistriesClient


def _cmd_run(args) -> int:
    from herbenzo.contract_gate import format_cli_error, validate_inbound_formulation_spec
    from pydantic import ValidationError

    try:
        raw = json.loads(pathlib.Path(args.spec).read_text())
        validate_inbound_formulation_spec(raw)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(format_cli_error(exc), file=sys.stderr)
        return 2

    report = Pipeline(allow_network=not args.offline).run(raw)

    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))

    c = report["confidence"]
    sku = report["sku"]
    gap = report.get("classical_active_marker_gap")
    product = sku["product_name"] if sku else (gap or {}).get("product_name", "")
    n_ingredients = len(sku["ingredients"]) if sku else 0
    print(f"\n  product        : {product}")
    print(f"  ingredients    : {n_ingredients}")
    print(f"  confidence     : A={c['inherited_from_A']} "
          f"-> B={c['after_modernization']} -> adjudicated={c['after_adjudication']}")
    print(f"  citations      : {report['citation_summary']}")
    if gap:
        print("  indicator      : classical_active_marker_gap (advisory, does not block)")
        for item in gap.get("ingredients") or []:
            print(f"                   - {item['ingredient_id']}: {item['reason']}")
    if report["declared_gaps"]:
        print(f"  declared gaps  : {len(report['declared_gaps'])} query/queries with no records")
        for g in report["declared_gaps"]:
            print(f"                   - {g}")
    print(f"\n  BCS classification")
    if not sku or not sku["ingredients"]:
        print("    (no marker-backed ingredient)")
    else:
        for ing in sku["ingredients"]:
            print(f"    {ing['marker']['marker_name']:<40s} class {ing['bcs']['bcs_class']:<4s}"
                  f" -> {ing['delivery']['primary']}")
    print(f"\n  report written : {out}\n")
    return 0


def _cmd_adjudicate(args) -> int:
    a = AdjudicationService().adjudicate(
        claim=args.claim, subject=args.subject, pmid=args.pmid,
        subject_aliases=tuple(args.alias or ()), claim_domain=args.domain,
    )
    print(json.dumps(a.as_dict(), indent=2))
    return 0 if a.verdict != "reject" else 1


def _cmd_markers(args) -> int:
    client = StaticRegistriesClient()
    print(f"{'ID':<10} {'BOTANICAL':<26} {'MARKER':<40} {'CID':>10}")
    for iid, rec in _INGREDIENTS.items():
        m = rec.markers[0]
        try:
            cid = client.get_physicochemical_properties(m.marker_name).pubchem_cid
        except Exception:
            cid = "-"
        flag = "  [efflux override]" if m.efflux_substrate else ""
        print(f"{iid:<10} {rec.botanical_name:<26} {m.marker_name:<40} {cid:>10}{flag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="herbenzo", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a FormulationSpec through the pipeline")
    r.add_argument("spec")
    r.add_argument("-o", "--output", default="out/report.json")
    r.add_argument("--offline", action="store_true",
                   help="use cached descriptors only; skip all network calls")
    r.set_defaults(func=_cmd_run)

    a = sub.add_parser("adjudicate", help="adjudicate one claim against one PMID")
    a.add_argument("--pmid", required=True)
    a.add_argument("--subject", required=True)
    a.add_argument("--claim", required=True)
    a.add_argument("--alias", action="append")
    a.add_argument("--domain", default="general",
                   choices=["general", "mechanism", "safety", "efficacy"])
    a.set_defaults(func=_cmd_adjudicate)

    m = sub.add_parser("markers", help="list registry ingredients and their markers")
    m.set_defaults(func=_cmd_markers)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

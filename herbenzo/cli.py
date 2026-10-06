"""Command-line entry point.

    python -m herbenzo.cli run examples/ashwagandha.json -o out/report.json
    python -m herbenzo.cli run examples/triphala.json --offline
    python -m herbenzo.cli adjudicate --pmid 37257749 \
        --subject "Terminalia bellirica" --claim "well tolerated orally" --domain safety
    python -m herbenzo.cli markers
    python -m herbenzo.cli enrich propose "Bacopa monnieri"
    python -m herbenzo.cli enrich list
    python -m herbenzo.cli enrich approve c0123456789abcdef
    python -m herbenzo.cli suggest-formats HB-ASHW HB-AMLA --dosage-form avaleha \\
        --product-name Chyawanprash --audience adults
    python -m herbenzo.cli ayush accept ARP_AYU030906 --note "journal checked"
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from herbenzo.config import load_project_env
from herbenzo.pipeline import Pipeline
from herbenzo.services.adjudication import AdjudicationService
from herbenzo.services.enrichment import EnrichmentError, build_enrichment_service
from herbenzo.services.registries import StaticRegistriesClient, iter_registry_records


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


def _parse_quantity(raw: str) -> tuple[str, float]:
    if "=" not in raw:
        raise ValueError(f"quantity must look like HB-ASHW=500, not {raw!r}")
    ingredient_id, amount = raw.split("=", 1)
    ingredient_id = ingredient_id.strip()
    if not ingredient_id:
        raise ValueError(f"quantity must look like HB-ASHW=500, not {raw!r}")
    try:
        quantity = float(amount)
    except ValueError as exc:
        raise ValueError(f"quantity must look like HB-ASHW=500, not {raw!r}") from exc
    if quantity <= 0:
        raise ValueError(f"quantity must be greater than 0, not {raw!r}")
    return ingredient_id, quantity


def _cmd_suggest_formats(args) -> int:
    """Advisory format ranking. Does not run the modernizer."""
    from herbenzo.format_suggestions import suggest_formats
    from herbenzo.services.registries import UnknownIngredient, UnknownMarker

    quantities: dict[str, float] = {}
    try:
        for raw in args.quantity or []:
            ingredient_id, quantity = _parse_quantity(raw)
            quantities[ingredient_id] = quantity
        result = suggest_formats(
            list(args.ingredient_ids),
            audience=args.audience,
            dosage_form=args.dosage_form,
            product_name=args.product_name,
            quantities_mg=quantities,
        )
    except (UnknownIngredient, UnknownMarker) as exc:
        print(f"unknown_ingredient: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


def _cmd_markers(args) -> int:
    client = StaticRegistriesClient()
    print(f"{'ID':<12} {'BOTANICAL':<26} {'MARKER':<40} {'CID':>10}")
    for rec in iter_registry_records():
        m = rec.markers[0]
        try:
            cid = client.get_physicochemical_properties(m.marker_name).pubchem_cid
        except Exception:
            cid = "-"
        flag = "  [efflux override]" if m.efflux_substrate else ""
        print(f"{rec.ingredient_id:<12} {rec.botanical_name:<26} {m.marker_name:<40} {cid:>10}{flag}")
    return 0


def _enrichment():
    return build_enrichment_service()


def _cmd_enrich_propose(args) -> int:
    try:
        doc = _enrichment().propose(
            args.query,
            part_used=args.part,
            max_markers=args.max_markers,
            max_pmids=args.max_pmids,
        )
    except EnrichmentError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(doc, indent=2))
    return 0


def _cmd_enrich_list(args) -> int:
    rows = _enrichment().list(status=args.status)
    if args.json:
        print(json.dumps({"candidates": rows}, indent=2))
        return 0
    print(f"{'ID':<18} {'STATUS':<10} {'PROPOSED':<12} QUERY")
    for row in rows:
        print(
            f"{row.get('candidate_id') or '':<18} {row.get('status') or '':<10} "
            f"{row.get('proposed_ingredient_id') or '':<12} {row.get('query') or ''}"
        )
    return 0


def _cmd_enrich_show(args) -> int:
    try:
        doc = _enrichment().get(args.candidate_id)
    except EnrichmentError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(doc, indent=2))
    return 0


def _cmd_enrich_approve(args) -> int:
    try:
        doc = _enrichment().approve(
            args.candidate_id,
            marker_name=args.marker,
            part_used=args.part,
            common_name=args.common_name,
            note=args.note,
        )
    except EnrichmentError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    decision = doc.get("decision") or {}
    print(f"approved {doc.get('candidate_id')} as {decision.get('ingredient_id')}")
    return 0


def _cmd_ayush_search(args) -> int:
    from herbenzo.services.ayush_portal import ayush_portal_search

    result = ayush_portal_search(
        args.query,
        system=args.system,
        category=args.category,
        limit=args.limit,
    )
    print(json.dumps(result, indent=2))
    return 0


def _cmd_ayush_accept(args) -> int:
    from herbenzo.config import get_settings
    from herbenzo.services.ayush_portal import AyushPortalService

    try:
        decision = AyushPortalService.from_settings(get_settings()).accept(args.arp_id, note=args.note or "")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(decision, indent=2))
    return 0


def _cmd_enrich_reject(args) -> int:
    try:
        doc = _enrichment().reject(args.candidate_id, reason=args.reason or "")
    except EnrichmentError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    print(f"rejected {doc.get('candidate_id')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_project_env()
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

    enrich = sub.add_parser("enrich", help="propose and review ingredient candidates")
    enrich_sub = enrich.add_subparsers(dest="enrich_cmd", required=True)

    propose = enrich_sub.add_parser("propose", help="resolve a species into a pending candidate")
    propose.add_argument("query")
    propose.add_argument("--part", default=None, help="plant part to store on the candidate")
    propose.add_argument("--max-markers", type=int, default=3)
    propose.add_argument("--max-pmids", type=int, default=5)
    propose.set_defaults(func=_cmd_enrich_propose)

    listing = enrich_sub.add_parser("list", help="list enrichment candidates")
    listing.add_argument("--status", choices=["pending", "approved", "rejected"])
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=_cmd_enrich_list)

    show = enrich_sub.add_parser("show", help="print one candidate evidence bundle")
    show.add_argument("candidate_id")
    show.set_defaults(func=_cmd_enrich_show)

    approve = enrich_sub.add_parser("approve", help="promote a pending candidate into the registry")
    approve.add_argument("candidate_id")
    approve.add_argument("--marker", default=None)
    approve.add_argument("--part", default=None)
    approve.add_argument("--common-name", default=None)
    approve.add_argument("--note", default=None)
    approve.set_defaults(func=_cmd_enrich_approve)

    reject = enrich_sub.add_parser("reject", help="reject a pending candidate")
    reject.add_argument("candidate_id")
    reject.add_argument("--reason", default="")
    reject.set_defaults(func=_cmd_enrich_reject)

    s = sub.add_parser(
        "suggest-formats",
        help="rank advisory finished formats for one or more ingredient ids",
    )
    s.add_argument("ingredient_ids", nargs="+", metavar="INGREDIENT_ID")
    s.add_argument("--audience", choices=["kids", "teens", "adults", "elderly"])
    s.add_argument("--dosage-form", default=None)
    s.add_argument("--product-name", default=None)
    s.add_argument(
        "--quantity",
        action="append",
        default=[],
        metavar="ID=MG",
        help="optional milligrams per serving, repeatable (HB-ASHW=500)",
    )
    s.set_defaults(func=_cmd_suggest_formats)

    ayush = sub.add_parser("ayush", help="Ayush Research Portal bibliographic lookup (off unless enabled)")
    ayush_sub = ayush.add_subparsers(dest="ayush_cmd", required=True)
    ayush_search = ayush_sub.add_parser("search", help="search ARP titles; prints compact hits or an unavailable result")
    ayush_search.add_argument("query")
    ayush_search.add_argument("--system", default="any")
    ayush_search.add_argument("--category", default="any")
    ayush_search.add_argument("--limit", type=int, default=None)
    ayush_search.set_defaults(func=_cmd_ayush_search)
    ayush_accept = ayush_sub.add_parser("accept", help="mark an ARP id reviewer-accepted")
    ayush_accept.add_argument("arp_id")
    ayush_accept.add_argument("--note", default="")
    ayush_accept.set_defaults(func=_cmd_ayush_accept)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

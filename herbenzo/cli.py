"""Command-line entry point.

    python -m herbenzo.cli research "Clitoria ternatea"
    python -m herbenzo.cli research-approve candidate.json
    python -m herbenzo.cli suggest "butterfly pea"
    python -m herbenzo.cli run examples/ashwagandha.json -o out/report.json
    python -m herbenzo.cli suggest-formats approvals.json --ingredient tax-43366
    python -m herbenzo.cli ayush search "Withania somnifera" --system ayurveda --limit 5
    python -m herbenzo.cli ayush record ARP_AYU030864
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
from herbenzo.services.gemini_research import ResearchFrontDoor
from herbenzo.services.records import ResearchError, UnknownMarker, apply_marker_overrides, snapshot_from_approvals
from herbenzo.services.research import build_research_service


def _load_json(path: str) -> dict:
    return json.loads(pathlib.Path(path).read_text())


def _cmd_run(args) -> int:
    from herbenzo.contract_gate import format_cli_error, validate_inbound_formulation_spec
    from pydantic import ValidationError

    try:
        raw = _load_json(args.spec)
    except (OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    approvals: list = []
    overrides = None
    if isinstance(raw, dict) and isinstance(raw.get("spec"), dict):
        spec_payload = raw["spec"]
        approvals = list(raw.get("approvals") or [])
        overrides = raw.get("marker_overrides")
    else:
        spec_payload = raw
    try:
        validate_inbound_formulation_spec(spec_payload)
        approvals = apply_marker_overrides(approvals, overrides)
    except (ValidationError, ValueError, ResearchError) as exc:
        if isinstance(exc, ResearchError):
            print(f"{exc.code}: {exc}", file=sys.stderr)
        else:
            print(format_cli_error(exc), file=sys.stderr)
        return 2

    lookup = snapshot_from_approvals([doc for doc in approvals if isinstance(doc, dict)])
    try:
        report = Pipeline(lookup=lookup, approvals=approvals, allow_network=not args.offline).run(spec_payload)
    except ResearchError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2

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
    warnings = report.get("warnings") or []
    if warnings:
        print("  warnings       : " + "; ".join(warnings))
    if gap:
        print("  indicator      : classical_active_marker_gap (advisory, does not block)")
        if gap.get("release"):
            print(f"                   release: {gap['release']}")
        if gap.get("message"):
            print(f"                   {gap['message']}")
        for item in gap.get("ingredients") or []:
            print(f"                   - {item['ingredient_id']}: {item['reason']}")
    if report["declared_gaps"]:
        print(f"  declared gaps  : {len(report['declared_gaps'])} query/queries with no records")
        for g in report["declared_gaps"]:
            print(f"                   - {g}")
    print("\n  BCS classification")
    if not sku or not sku["ingredients"]:
        print("    (no ingredients)")
    else:
        for ing in sku["ingredients"]:
            marker = ing.get("marker") or {}
            pending = ing.get("marker_status") == "pending" or marker.get("marker_status") == "pending"
            if pending:
                print(f"    {ing.get('botanical_name', ing.get('ingredient_id', '')):<40s} marker pending (unstandardized)")
            else:
                bcs_class = str((ing.get("bcs") or {}).get("bcs_class") or "")
                primary = str((ing.get("delivery") or {}).get("primary") or "")
                print(f"    {marker.get('marker_name', ''):<40s} class {bcs_class:<4s} -> {primary}")
    print(f"\n  report written : {out}\n")
    return 0


def _cmd_adjudicate(args) -> int:
    a = AdjudicationService().adjudicate(
        claim=args.claim, subject=args.subject, pmid=args.pmid,
        subject_aliases=tuple(args.alias or ()), claim_domain=args.domain,
    )
    print(json.dumps(a.as_dict(), indent=2))
    return 0 if a.verdict != "reject" else 1


def _cmd_research(args) -> int:
    try:
        doc = ResearchFrontDoor(build_research_service()).research(args.query, part_used=args.part)
    except ResearchError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(doc, indent=2))
    return 0


def _cmd_research_approve(args) -> int:
    try:
        candidate = _load_json(args.candidate)
        doc = build_research_service().approve(
            candidate,
            marker_name=args.marker,
            part_used=args.part,
            common_name=args.common_name,
            note=args.note,
        )
    except (OSError, json.JSONDecodeError, ResearchError) as exc:
        code = getattr(exc, "code", "error")
        print(f"{code}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(doc, indent=2))
    return 0


def _cmd_suggest(args) -> int:
    print(json.dumps(build_research_service().suggest(args.query), indent=2))
    return 0


def _cmd_suggest_formats(args) -> int:
    from herbenzo.format_suggestions import suggest_formats

    try:
        payload = _load_json(args.approvals)
        approvals = payload.get("approvals") if isinstance(payload, dict) and "approvals" in payload else payload
        if not isinstance(approvals, list):
            raise ValueError("approvals file must be a list or an object with approvals")
        lookup = snapshot_from_approvals(approvals)
        quantities: dict[str, float] = {}
        for raw in args.quantity or []:
            ingredient_id, amount = raw.split("=", 1)
            quantities[ingredient_id.strip()] = float(amount)
        result = suggest_formats(
            list(args.ingredient_ids),
            audience=args.audience,
            dosage_form=args.dosage_form,
            product_name=args.product_name,
            quantities_mg=quantities,
            registries=lookup,
        )
    except (ResearchError, UnknownMarker, ValueError, OSError, json.JSONDecodeError) as exc:
        code = getattr(exc, "code", "not_approved")
        print(f"{code}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    p = argparse.ArgumentParser(prog="herbenzo", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="modernize a FormulationSpec plus approvals")
    r.add_argument("spec")
    r.add_argument("-o", "--output", default="out/report.json")
    r.add_argument("--offline", action="store_true",
                   help="skip literature network calls during adjudication")
    r.set_defaults(func=_cmd_run)

    a = sub.add_parser("adjudicate", help="adjudicate one claim against one PMID")
    a.add_argument("--pmid", required=True)
    a.add_argument("--subject", required=True)
    a.add_argument("--claim", required=True)
    a.add_argument("--alias", action="append")
    a.add_argument("--domain", default="general",
                   choices=["general", "mechanism", "safety", "efficacy"])
    a.set_defaults(func=_cmd_adjudicate)

    research = sub.add_parser("research", help="live research for one ingredient name")
    research.add_argument("query")
    research.add_argument("--part", default=None)
    research.set_defaults(func=_cmd_research)

    approve = sub.add_parser("research-approve", help="approve a research JSON document")
    approve.add_argument("candidate")
    approve.add_argument("--marker", default=None)
    approve.add_argument("--part", default=None)
    approve.add_argument("--common-name", default=None)
    approve.add_argument("--note", default=None)
    approve.set_defaults(func=_cmd_research_approve)

    suggest = sub.add_parser("suggest", help="live NCBI Taxonomy suggestions")
    suggest.add_argument("query")
    suggest.set_defaults(func=_cmd_suggest)

    s = sub.add_parser(
        "suggest-formats",
        help="rank advisory finished formats from an approvals file",
    )
    s.add_argument("approvals", help="JSON list of approval documents, or an object with approvals")
    s.add_argument("ingredient_ids", nargs="+", metavar="INGREDIENT_ID")
    s.add_argument("--audience", choices=["kids", "teens", "adults", "elderly"])
    s.add_argument("--dosage-form", default=None)
    s.add_argument("--product-name", default=None)
    s.add_argument("--quantity", action="append", default=[], metavar="ID=MG")
    s.set_defaults(func=_cmd_suggest_formats)

    ayush = sub.add_parser("ayush", help="Ayush Research Portal bibliographic lookup (off unless enabled)")
    ayush_sub = ayush.add_subparsers(dest="ayush_cmd", required=True)
    ayush_search = ayush_sub.add_parser("search", help="search ARP titles; prints hits with citations, or disabled/unavailable")
    ayush_search.add_argument("query")
    ayush_search.add_argument("--system", default="any")
    ayush_search.add_argument("--category", default="any")
    ayush_search.add_argument("--limit", type=int, default=None)
    ayush_search.add_argument("--offset", type=int, default=0, help="row offset (portal startPage)")
    ayush_search.set_defaults(func=_cmd_ayush_search)
    ayush_record = ayush_sub.add_parser("record", help="fetch one ARP record page by ARP id")
    ayush_record.add_argument("arp_id")
    ayush_record.set_defaults(func=_cmd_ayush_record)
    ayush_accept = ayush_sub.add_parser("accept", help="record a reviewer accept note for an ARP id")
    ayush_accept.add_argument("arp_id")
    ayush_accept.add_argument("--note", default="")
    ayush_accept.set_defaults(func=_cmd_ayush_accept)

    args = p.parse_args(argv)
    return args.func(args)


def _cmd_ayush_search(args) -> int:
    from herbenzo.services.ayush_portal import ayush_public_search

    result = ayush_public_search(
        args.query,
        system=args.system,
        category=args.category,
        limit=args.limit,
        offset=args.offset,
    )
    print(json.dumps(result, indent=2))
    return 0


def _cmd_ayush_record(args) -> int:
    from herbenzo.services.ayush_portal import ayush_public_record

    try:
        result = ayush_public_record(args.arp_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
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


if __name__ == "__main__":
    sys.exit(main())

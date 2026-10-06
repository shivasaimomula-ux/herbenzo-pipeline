"""Command-line entry point.

    python -m herbenzo.cli run examples/ashwagandha.json -o out/report.json
    python -m herbenzo.cli run examples/triphala.json --offline
    python -m herbenzo.cli adjudicate --pmid 37257749 \
        --subject "Terminalia bellirica" --claim "well tolerated orally" --domain safety
    python -m herbenzo.cli markers
    python -m herbenzo.cli enrich propose "Bacopa monnieri"
    python -m herbenzo.cli enrich list
    python -m herbenzo.cli enrich approve c0123456789abcdef
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
    print(f"\n  product        : {report['sku']['product_name']}")
    print(f"  ingredients    : {len(report['sku']['ingredients'])}")
    print(f"  confidence     : A={c['inherited_from_A']} "
          f"-> B={c['after_modernization']} -> adjudicated={c['after_adjudication']}")
    print(f"  citations      : {report['citation_summary']}")
    if report["declared_gaps"]:
        print(f"  declared gaps  : {len(report['declared_gaps'])} query/queries with no records")
        for g in report["declared_gaps"]:
            print(f"                   - {g}")
    print(f"\n  BCS classification")
    for ing in report["sku"]["ingredients"]:
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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""Command line entry point.

    python -m billing.cli init
    python -m billing.cli validate
    python -m billing.cli reconcile [--station A]
    python -m billing.cli compare --station A [--window 195:300] [--day 1] [--json]
    python -m billing.cli quote --station A --tariff new
    python -m billing.cli relocate --right R1 --to B --window 225:270 [--json]
    python -m billing.cli customers
    python -m billing.cli curve --station B
    python -m billing.cli migrations
    python -m billing.cli request --right R1 --site B --window 225:270
    python -m billing.cli event --id C5 --right R1 --state offered --minute 225 --site B
    python -m billing.cli migrate --id M1 --right R1 --to B --minute 240 --signed-kw 16
    python -m billing.cli accept --id M1 --day 7
    python -m billing.cli withdraw --id M1 --day 7 --note "client paused"
    python -m billing.cli overlap --right R1 [--signed-day 4 --handoff-day 6]
    python -m billing.cli correct --id X1 --day 3 --set A/main@240=41 [--basis estimated] [--restate]
    python -m billing.cli settle --id S1 --station A --window 195:300 --day 1 --note baseline
    python -m billing.cli restate --id S1
    python -m billing.cli settlements
    python -m billing.cli revisions
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from .errors import BillingError
from .reports import comparison_text, migration_text, relocation_text
from .service import Billing


def _window(value: str) -> tuple[int, int]:
    try:
        start, end = (int(part) for part in value.replace("-", ":").split(":"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("window must look like START:END, e.g. 195:300") from exc
    if start >= end:
        raise argparse.ArgumentTypeError("window start must be before end")
    return start, end


def _assignment(value: str) -> tuple[str, str, int, str]:
    try:
        meter_part, rest = value.split("@", 1)
        minute_text, number = rest.split("=", 1)
        station, meter_name = meter_part.split("/")
        return station, meter_name, int(minute_text), number
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("assignment must look like A/main@240=41") from exc


def _quote_text(result: dict[str, object]) -> str:
    rows = [f"station {result['station']}  tariff {result['tariff']}  "
            f"window [{result['window'][0]}, {result['window'][1]})"]
    rows.append(f"  evidence: revision {result['evidence']['revision']} "
                f"as of day {result['evidence']['as_of_day']}")
    for charge in result["charges"]:
        rows.append(
            f"  {charge['kind']:16}{charge['quantity']:>10} {charge['unit']:<12}"
            f"{charge['amount']:>12}  {charge['basis']}"
        )
    rows.append(f"  {'billed total':16}{'':>10} {'':<12}{result['billed_total_cent']:>12}")
    if result["exposure_cent"] not in ("0", "0.00"):
        rows.append(f"  {'exposure':16}{'':>10} {'':<12}{result['exposure_cent']:>12}  (not billed)")
    return "\n".join(rows)


def _dumps(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="billing", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", default="sources", help="source CSV directory")
    parser.add_argument("--ledger", default="var/ledger.json", help="local ledger path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the first revision from the source tables")

    p = sub.add_parser("validate", help="validate the live sources and ledger")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reconcile", help="check one live reading per interval")
    p.add_argument("--station")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("compare", help="price old vs new terms on one evidence set")
    p.add_argument("--station", required=True)
    p.add_argument("--window", type=_window)
    p.add_argument("--day", type=int)
    p.add_argument("--revision", type=int)
    p.add_argument("--include-estimated", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("quote", help="price one tariff on its own rules")
    p.add_argument("--station", required=True)
    p.add_argument("--tariff", required=True)
    p.add_argument("--window", type=_window)
    p.add_argument("--day", type=int)
    p.add_argument("--revision", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("relocate", help="move one right and show the remaining cost")
    p.add_argument("--right", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--window", type=_window, required=True)
    p.add_argument("--day", type=int)
    p.add_argument("--revision", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("customers", help="per-client reserve rights")
    p.add_argument("--day", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("curve", help="per-interval evidence, with bands for blanks")
    p.add_argument("--station", required=True)
    p.add_argument("--day", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("migrations", help="current migration states")
    p.add_argument("--day", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("request", help="dry-run a migration request")
    p.add_argument("--right", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--window", type=_window, required=True)
    p.add_argument("--day", type=int)

    p = sub.add_parser("event", help="record a transition event")
    p.add_argument("--id", required=True)
    p.add_argument("--right", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--minute", type=int, required=True)
    p.add_argument("--site", default="")
    p.add_argument("--day", type=int, default=1)

    p = sub.add_parser("migrate", help="record a partial migration signed by the client")
    p.add_argument("--id", required=True)
    p.add_argument("--right", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--minute", type=int, required=True)
    p.add_argument("--signed-kw", type=Decimal)
    p.add_argument("--day", type=int, default=1)
    p.add_argument("--note", default="")

    p = sub.add_parser("accept", help="fully accept a migration")
    p.add_argument("--id", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--note", default="")

    p = sub.add_parser("withdraw", help="withdraw a migration request")
    p.add_argument("--id", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--note", default="")

    p = sub.add_parser("overlap", help="extra cost of staged migration")
    p.add_argument("--right", required=True)
    p.add_argument("--signed-day", type=int)
    p.add_argument("--handoff-day", type=int)

    p = sub.add_parser("correct", help="record a metering correction as a new revision")
    p.add_argument("--id", required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--set", dest="assignments", action="append", type=_assignment, required=True,
                   metavar="STATION/METER@MINUTE=KW")
    p.add_argument("--basis", default="estimated")
    p.add_argument("--note", default="")
    p.add_argument("--restate", action="store_true")

    p = sub.add_parser("settle", help="pin an analysis to a revision")
    p.add_argument("--id", required=True)
    p.add_argument("--station", required=True)
    p.add_argument("--window", type=_window, required=True)
    p.add_argument("--day", type=int, required=True)
    p.add_argument("--note", default="")

    p = sub.add_parser("restate", help="recompute a settlement under the current revision")
    p.add_argument("--id", required=True)

    p = sub.add_parser("settlements", help="list pinned analyses")
    p.add_argument("--json", action="store_true")

    sub.add_parser("revisions", help="list the ledger revisions")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = Billing(args.sources, args.ledger)
    try:
        if args.command == "init":
            print(f"revision {service.init()}")
        elif args.command == "validate":
            bundle = service.validate()
            payload = {
                "errors": [f.__dict__ for f in bundle.report.errors],
                "warnings": [f.__dict__ for f in bundle.report.warnings],
                "drift": service.drift(),
            }
            if args.json:
                _dumps(payload)
            else:
                print(f"{len(payload['errors'])} error(s), {len(payload['warnings'])} warning(s)")
                for finding in payload["errors"] + payload["warnings"]:
                    print(f"  [{finding['severity']}] {finding['code']}: {finding['message']}")
                for finding in payload["drift"]:
                    print(f"  [drift] {finding['message']}")
        elif args.command == "reconcile":
            reconciliation = service.reconcile(args.station)
            if args.json:
                _dumps(reconciliation.as_dict())
            else:
                print(f"station {args.station or '*'}: {reconciliation.live}/{reconciliation.intervals} live")
                for minute, reason in reconciliation.unreconciled:
                    print(f"  unobserved @{minute}: {reason}")
                for finding in reconciliation.findings:
                    print(f"  [{finding.severity}] {finding.code}: {finding.message}")
        elif args.command == "compare":
            result = service.compare(
                args.station, args.window, day=args.day, revision=args.revision,
                include_estimated=args.include_estimated,
            )
            if args.json:
                _dumps(result)
            else:
                print(comparison_text(result))
        elif args.command == "quote":
            result = service.quote(args.station, args.tariff, args.window, day=args.day, revision=args.revision)
            _dumps(result) if args.json else print(_quote_text(result))
        elif args.command == "relocate":
            result = service.relocate(
                args.right, args.to, args.window, day=args.day, revision=args.revision
            )
            _dumps(result) if args.json else print(relocation_text(result))
        elif args.command == "customers":
            rows = service.customers(day=args.day)
            _dumps(rows) if args.json else print("\n".join(
                f"{r['right']} {r['client']}: reserve {r['reserve_kw']} kW at {r['reserve_site']} "
                f"confirmed={r['confirmed']} (site free {r['site_free_kw']}, short {r['site_short_kw']})"
                for r in rows
            ))
        elif args.command == "curve":
            rows = service.curve(args.station, day=args.day)
            if args.json:
                _dumps(rows)
            else:
                for row in rows:
                    if row["kw"] is None:
                        low, high = row["band_kw"]
                        print(f"  {row['minute']:>3}  blank       band {low}..{high} kW")
                    else:
                        print(f"  {row['minute']:>3}  {row['kw']:>6} kW  {row['basis']:<9} {row['reading']}")
        elif args.command == "migrations":
            states = service.migrations(day=args.day)
            as_of = args.day if args.day is not None else service.context().book().as_of_day
            _dumps(states) if args.json else print(migration_text(states, as_of))
        elif args.command == "request":
            _dumps(service.check_request(args.right, args.site, args.window, day=args.day))
        elif args.command == "event":
            _dumps(service.record_event(args.id, args.right, args.state, args.minute, args.site, args.day))
        elif args.command == "migrate":
            _dumps(service.record_migration(
                args.id, args.right, args.to, args.minute, args.day, args.signed_kw, args.note,
            ))
        elif args.command == "accept":
            _dumps(service.accept_migration(args.id, args.day, args.note))
        elif args.command == "withdraw":
            _dumps(service.withdraw_migration(args.id, args.day, args.note))
        elif args.command == "overlap":
            _dumps(service.overlap_cost(args.right, signed_day=args.signed_day, handoff_day=args.handoff_day))
        elif args.command == "correct":
            _dumps(service.correct(
                args.id, args.day, args.assignments, args.note,
                basis=args.basis, restate=args.restate,
            ))
        elif args.command == "settle":
            _dumps(service.settle(args.id, args.station, args.window, at_day=args.day, note=args.note))
        elif args.command == "restate":
            _dumps(service.restate(args.id))
        elif args.command == "settlements":
            rows = service.settlements()
            _dumps(rows) if args.json else print("\n".join(
                f"{r['id']}: {r['station']} {r['window']} at day {r['at_day']} revision {r['revision']}"
                for r in rows
            ) or "no settlements")
        elif args.command == "revisions":
            for entry in service.ledger.revisions():
                print(f"{entry['revision']}: {entry['action']} — {entry['summary']} ({entry['snapshot_digest'][:12]})")
        else:  # pragma: no cover
            parser.error(f"unknown command {args.command}")
    except BillingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

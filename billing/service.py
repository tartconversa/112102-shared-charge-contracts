"""The operator surface, one snapshot per mutation.

Every mutating operation loads the live sources plus the ledger, validates the
result, and commits a new revision whose snapshot every future quote replays.
Nothing here writes to ``sources/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import BillingError, CorrectionError, SourceError, TransitionError
from .evidence import EvidenceBook
from .ledger import Ledger
from .reconcile import Reconciliation, check_against_source, reconcile
from .pricing import (
    compare_station,
    day_curve,
    quote_station,
    relocate,
    renegotiation_options,
    right_reserve_view,
)
from .sources import EVENT_STATES, Guarantee, SourceBundle, bundle_from_snapshot, load_sources
from .transitions import (
    MigrationState,
    check_new_request,
    evaluate_migration,
    is_effective,
    overlap_cost,
)


@dataclass
class Context:
    bundle: SourceBundle
    revision: int | None
    digest: str

    def book(self, as_of_day: int | None = None) -> EvidenceBook:
        return EvidenceBook(self.bundle, as_of_day)


class Billing:
    def __init__(self, root: str | Path, ledger_path: str | Path):
        self.root = Path(root)
        self.ledger = Ledger(ledger_path)

    # ------------------------------------------------------------- snapshots
    def _raw(self) -> dict[str, list[dict[str, Any]]]:
        bundle = load_sources(self.root, self.ledger.corrected_readings(), self.ledger.transition_rows())
        return bundle.snapshot()

    def context(self, revision: int | None = None) -> Context:
        revisions = self.ledger.revisions()
        if not revisions:
            bundle = load_sources(
                self.root, self.ledger.corrected_readings(), self.ledger.transition_rows()
            )
            return Context(bundle, None, "live")
        if revision is None:
            revision = revisions[-1]["revision"]
        entry = next((r for r in revisions if r["revision"] == revision), None)
        if entry is None:
            raise CorrectionError(f"unknown revision {revision}")
        return Context(bundle_from_snapshot(entry["snapshot"]), entry["revision"], entry["snapshot_digest"])

    def _commit(self, action: str, summary: str) -> int:
        snapshot = self._raw()
        bundle = bundle_from_snapshot(snapshot)
        if bundle.report.errors:
            raise SourceError(
                "refusing to commit an invalid revision: "
                + "; ".join(f.message for f in bundle.report.errors[:5])
            )
        return self.ledger.commit(action, snapshot, summary)

    def init(self) -> int:
        return self._commit("init", "baseline snapshot from source tables")

    def ensure(self) -> Context:
        if not self.ledger.revisions():
            self.init()
        return self.context()

    # -------------------------------------------------------------- read-only
    def validate(self) -> SourceBundle:
        return load_sources(self.root, self.ledger.corrected_readings(), self.ledger.transition_rows())

    def reconcile(self, station: str | None = None, revision: int | None = None) -> Reconciliation:
        return reconcile(self.context(revision).bundle, station)

    def drift(self) -> list[dict[str, str]]:
        context = self.context()
        if context.revision is None:
            return []
        return [f.as_dict() for f in check_against_source(context.bundle, str(self.root))]

    def revisions(self) -> list[dict[str, object]]:
        return [
            {
                "revision": r["revision"],
                "action": r["action"],
                "summary": r["summary"],
                "snapshot_digest": r["snapshot_digest"],
            }
            for r in self.ledger.revisions()
        ]

    # --------------------------------------------------------------- quoting
    def compare(
        self,
        station: str,
        window: tuple[int, int] | None = None,
        *,
        day: int | None = None,
        revision: int | None = None,
        include_estimated: bool = False,
    ) -> dict[str, object]:
        context = self.context(revision)
        book = context.book(day)
        result = compare_station(book, station, window, include_estimated=include_estimated)
        result["evidence"] = {
            "as_of_day": book.as_of_day,
            "revision": context.revision,
            "snapshot_digest": context.digest,
        }
        quote = quote_station(book, station, "new", window, include_estimated=include_estimated)
        states = self._states(book)
        result["cap_consequences"] = renegotiation_options(
            book, station, quote,
            effective={s.right: is_effective(s) for s in states},
            manual_migrations=self.ledger.migrations(),
        )
        return result

    def quote(
        self,
        station: str,
        tariff_name: str,
        window: tuple[int, int] | None = None,
        *,
        day: int | None = None,
        revision: int | None = None,
    ) -> dict[str, object]:
        context = self.context(revision)
        book = context.book(day)
        quote = quote_station(book, station, tariff_name, window)
        payload = quote.as_dict()
        payload["evidence"] = {
            "as_of_day": book.as_of_day,
            "revision": context.revision,
            "snapshot_digest": context.digest,
        }
        return payload

    def relocate(
        self,
        right: str,
        destination: str,
        window: tuple[int, int],
        *,
        day: int | None = None,
        revision: int | None = None,
    ) -> dict[str, object]:
        context = self.context(revision)
        book = context.book(day)
        guarantee = self._right(book.bundle, right)
        result = relocate(book, guarantee, destination, window)
        result["evidence"] = {
            "as_of_day": book.as_of_day,
            "revision": context.revision,
            "snapshot_digest": context.digest,
        }
        return result

    def curve(self, station: str, *, day: int | None = None) -> list[dict[str, object]]:
        return day_curve(self.context().book(day), station)

    def customers(self, *, day: int | None = None) -> list[dict[str, object]]:
        book = self.context().book(day)
        return [right_reserve_view(book, right) for right in book.bundle.guarantees.values()]

    # ------------------------------------------------------------- migration
    def migrations(self, day: int | None = None) -> list[dict[str, object]]:
        book = self.context().book(day)
        return [state.as_dict() for state in self._states(book)]

    def migration(self, right: str, day: int | None = None) -> dict[str, object]:
        book = self.context().book(day)
        guarantee = self._right(book.bundle, right)
        return evaluate_migration(
            book, guarantee, self._events(book.bundle, right), manual=self.ledger.migrations()
        ).as_dict()

    def check_request(
        self, right: str, site: str, window: tuple[int, int], *, day: int | None = None
    ) -> dict[str, object]:
        book = self.context().book(day)
        guarantee = self._right(book.bundle, right)
        return check_new_request(book, guarantee, site, window, existing=self._states(book))

    def record_event(
        self,
        event_id: str,
        right: str,
        state: str,
        effective_minute: int,
        temporary_site: str,
        day: int,
    ) -> dict[str, object]:
        if state not in EVENT_STATES:
            raise TransitionError(f"unknown transition state {state!r}")
        context = self.ensure()
        book = context.book(day)
        guarantee = self._right(book.bundle, right)
        if state in {"offered", "partial_signed", "handoff_received"} and temporary_site:
            verdict = check_new_request(
                book, guarantee, temporary_site,
                (effective_minute, guarantee.end_minute), existing=self._states(book),
            )
            if not verdict["ok"] and verdict["code"] == "incompatible-request":
                raise TransitionError(verdict["reason"])
        self.ledger.add_event(
            event_id, right, state, effective_minute, temporary_site, day,
            EVENT_STATES, book.bundle.guarantees,
        )
        revision = self._commit(
            "transition",
            f"{event_id}: {right} -> {state} at {temporary_site or 'n/a'} from minute {effective_minute}",
        )
        return {"revision": revision, "event": event_id, "state": state}

    def record_migration(
        self,
        migration_id: str,
        right: str,
        deliver_to: str,
        effective_minute: int,
        requested_day: int,
        signed_kw: Decimal | None,
        note: str,
    ) -> dict[str, object]:
        context = self.ensure()
        book = context.book(requested_day)
        guarantee = self._right(book.bundle, right)
        if deliver_to not in book.bundle.stations:
            raise TransitionError(f"unknown site {deliver_to!r}")
        if not guarantee.start_minute <= effective_minute <= guarantee.end_minute:
            raise TransitionError(
                f"effective minute {effective_minute} is outside right {right} window {guarantee.window}"
            )
        if signed_kw is not None and not 0 <= signed_kw <= guarantee.firm_kw:
            raise TransitionError(
                f"signed {signed_kw} kW is outside [0, {guarantee.firm_kw}] for right {right}"
            )
        verdict = check_new_request(
            book, guarantee, deliver_to, (effective_minute, guarantee.end_minute),
            existing=self._states(book),
        )
        if not verdict["ok"]:
            raise TransitionError(verdict["reason"])
        self.ledger.add_migration(
            migration_id, right, deliver_to, effective_minute, requested_day,
            accepted_day=None, reserve_confirmation=verdict, note=note, signed_kw=signed_kw,
        )
        revision = self._commit(
            "migration",
            f"{migration_id}: {right} partial move to {deliver_to} at minute {effective_minute}",
        )
        return {"revision": revision, "migration": migration_id, "reserve": verdict}

    def accept_migration(self, migration_id: str, day: int, note: str) -> dict[str, object]:
        self.ensure()
        self.ledger.accept_migration(migration_id, day, note)
        revision = self._commit("migration", f"{migration_id}: accepted on day {day}")
        return {"revision": revision, "migration": migration_id, "accepted_day": day}

    def withdraw_migration(self, migration_id: str, day: int, note: str) -> dict[str, object]:
        self.ensure()
        self.ledger.withdraw_migration(migration_id, day, note)
        revision = self._commit("migration", f"{migration_id}: withdrawn on day {day}")
        return {"revision": revision, "migration": migration_id, "withdrawn_day": day}

    def overlap_cost(
        self, right: str, *, signed_day: int | None = None, handoff_day: int | None = None
    ) -> dict[str, object]:
        book = self.context().book()
        guarantee = self._right(book.bundle, right)
        state = evaluate_migration(
            book, guarantee, self._events(book.bundle, right), manual=self.ledger.migrations()
        )
        return overlap_cost(book, guarantee, state, signed_day=signed_day, handoff_day=handoff_day)

    # ------------------------------------------------------------ corrections
    def correct(
        self,
        correction_id: str,
        at_day: int,
        assignments: list[tuple[str, str, int, str]],
        note: str,
        *,
        basis: str = "estimated",
        restate: bool = False,
    ) -> dict[str, object]:
        self.ensure()
        context = self.context()
        book = context.book(at_day)
        rows: list[dict[str, str]] = []
        for index, (station, meter, minute, value) in enumerate(assignments, start=1):
            role = book.bundle.roles.get((station, meter))
            if role is None:
                raise CorrectionError(f"{station}/{meter} has no meter role")
            point = book.station_evidence(station).point(meter, minute)
            if point is None:
                raise CorrectionError(f"{station}/{meter}@{minute} has no interval in the data")
            if point.observed_day is not None and at_day < point.observed_day:
                raise CorrectionError(
                    f"correction at day {at_day} predates the reading it would replace "
                    f"({point.reading} observed day {point.observed_day})"
                )
            raw = Decimal(str(value))
            if raw < 0 and role.role != "buffer":
                raise CorrectionError(f"negative kW on non-buffer meter {station}/{meter}")
            rows.append({
                "station": station,
                "meter": meter,
                "timestamp_minute": str(minute),
                "kw": format(raw, "f"),
                "observed_day": str(at_day),
                "supersedes": point.reading or "",
                "reading": f"{correction_id}-{index}",
                "origin": "ledger",
                "basis": basis,
            })
        touched = self._touched_settlements(assignments, at_day)
        if touched and not restate:
            names = ", ".join(sorted({t["settlement"] for t in touched}))
            raise CorrectionError(
                f"correction {correction_id} changes intervals used by {names}; "
                "re-run with --restate to record the restatement"
            )
        self.ledger.add_correction(correction_id, at_day, rows, note, affects_settlement=restate)
        revision = self._commit("correction", f"{correction_id}: {note or 'meter correction'}")
        result: dict[str, object] = {
            "revision": revision,
            "correction": correction_id,
            "readings": rows,
            "touched_settlements": touched,
        }
        if touched:
            result["restatements"] = [
                self._restatement(t["settlement"], revision, learned_day=at_day) for t in touched
            ]
        return result

    def _touched_settlements(
        self, assignments: list[tuple[str, str, int, str]], at_day: int
    ) -> list[dict[str, object]]:
        touched: list[dict[str, object]] = []
        for settlement in self.ledger.settlements():
            start, end = settlement["window"]
            for station, meter, minute, _ in assignments:
                if station != settlement["station"] or not start <= minute < end:
                    continue
                touched.append({
                    "settlement": settlement["id"],
                    "station": station,
                    "meter": meter,
                    "minute": minute,
                    "settled_revision": settlement["revision"],
                    "settled_at_day": settlement["at_day"],
                    "correction_at_day": at_day,
                })
                break
        return touched

    # ------------------------------------------------------------ settlements
    def settle(
        self,
        settlement_id: str,
        station: str,
        window: tuple[int, int],
        *,
        at_day: int,
        note: str,
        conditions: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        context = self.ensure()
        result = self.compare(station, window, day=at_day)
        self.ledger.add_settlement(
            settlement_id,
            at_day=at_day,
            revision=context.revision,
            station=station,
            window=window,
            conditions=conditions or {},
            note=note,
        )
        revision = self._commit(
            "settlement", f"{settlement_id}: {station} [{window[0]}, {window[1]}) at day {at_day}"
        )
        return {
            "settlement": settlement_id,
            "revision": revision,
            "snapshot_digest": self.context(revision).digest,
            "analysis": result,
        }

    def settlements(self) -> list[dict[str, object]]:
        return self.ledger.settlements()

    def restate(self, settlement_id: str, *, note: str = "") -> dict[str, object]:
        records = [s for s in self.ledger.settlements() if s["id"] == settlement_id]
        if not records:
            raise CorrectionError(f"unknown settlement {settlement_id}")
        record = records[0]
        current = self.ensure()
        before = self.compare(record["station"], tuple(record["window"]), day=record["at_day"], revision=record["revision"])
        after = self.compare(record["station"], tuple(record["window"]), day=current.book().as_of_day, revision=current.revision)
        return self._restatement_payload(record, before, after, current.revision, note)

    def _restatement(self, settlement_id: str, revision: int, learned_day: int) -> dict[str, object]:
        record = next(s for s in self.ledger.settlements() if s["id"] == settlement_id)
        before = self.compare(record["station"], tuple(record["window"]), day=record["at_day"], revision=record["revision"])
        after = self.compare(record["station"], tuple(record["window"]), day=learned_day, revision=revision)
        return self._restatement_payload(record, before, after, revision, "automatic after correction")

    def _restatement_payload(
        self,
        record: dict[str, Any],
        before: dict[str, object],
        after: dict[str, object],
        revision: int | None,
        note: str,
    ) -> dict[str, object]:
        old_delta = Decimal(str(before["delta_cent"]))
        new_delta = Decimal(str(after["delta_cent"]))
        return {
            "settlement": record["id"],
            "station": record["station"],
            "window": record["window"],
            "as_of_day": record["at_day"],
            "settled_revision": record["revision"],
            "current_revision": revision,
            "before": {
                "old_cent": before["old"]["billed_total_cent"],
                "new_cent": before["new"]["billed_total_cent"],
                "delta_cent": before["delta_cent"],
                "evidence_revision": before["evidence"]["revision"],
                "as_of_day": before["evidence"]["as_of_day"],
            },
            "after": {
                "old_cent": after["old"]["billed_total_cent"],
                "new_cent": after["new"]["billed_total_cent"],
                "delta_cent": after["delta_cent"],
                "evidence_revision": after["evidence"]["revision"],
                "as_of_day": after["evidence"]["as_of_day"],
            },
            "change_cent": format(new_delta - old_delta, "f"),
            "note": note,
        }

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _right(bundle: SourceBundle, right: str) -> Guarantee:
        if right not in bundle.guarantees:
            raise BillingError(f"unknown right {right!r}")
        return bundle.guarantees[right]

    @staticmethod
    def _events(bundle: SourceBundle, right: str) -> list:
        return [event for event in bundle.transitions if event.right == right]

    def _states(self, book: EvidenceBook) -> list[MigrationState]:
        return [
            evaluate_migration(
                book, guarantee, self._events(book.bundle, guarantee.right),
                manual=self.ledger.migrations(),
            )
            for guarantee in book.bundle.guarantees.values()
        ]

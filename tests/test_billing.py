"""Tests for the evidence, pricing, migration and reproducibility rules."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from billing.errors import CorrectionError, TransitionError
from billing.evidence import EvidenceBook, resolve_terminal
from billing.pricing import (
    quote_station,
    relocate,
    renegotiation_options,
    reserve_profile,
    right_reserve_view,
)
from billing.reconcile import reconcile
from billing.service import Billing
from billing.sources import load_sources

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"


def fresh_service() -> Billing:
    directory = Path(tempfile.mkdtemp(prefix="billing-test-"))
    return Billing(SOURCES, directory / "ledger.json")


class Workspace:
    """A temporary copy of the source tables, for mutation tests."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="billing-src-"))
        for name in ("meter_readings", "meter_roles", "tariffs", "guarantees", "reserve_inventory", "transition_events"):
            shutil.copy(SOURCES / f"{name}.csv", self.path / f"{name}.csv")
        self.ledger = self.path / "ledger.json"

    def service(self) -> Billing:
        return Billing(self.path, self.ledger)

    def rewrite(self, name: str, rows: str) -> None:
        (self.path / f"{name}.csv").write_text(rows, encoding="utf-8")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_sources(SOURCES)

    def test_blank_is_not_zero_and_has_a_band(self):
        book = EvidenceBook(self.bundle, as_of_day=1)
        evidence = book.station_evidence("B")
        statement = book.demand_statement(evidence, 195, 300)
        self.assertIn((240, 255), statement.unobserved)
        self.assertGreater(statement.high_kw, statement.peak_kw)
        self.assertGreater(statement.low_kw, Decimal(0))

    def test_component_floor_excludes_zero_invention(self):
        book = EvidenceBook(self.bundle, as_of_day=1)
        evidence = book.station_evidence("B")
        floor = book.component_floor(evidence, 240)
        self.assertEqual(floor, Decimal(7))  # sub1 9 + buffer -2
        self.assertIsNone(book.unattributed(evidence, 240))

    def test_late_correction_supersedes_blank(self):
        bundle = load_sources(SOURCES, extra_readings=[{
            "station": "B", "meter": "main", "timestamp_minute": "240", "kw": "30",
            "observed_day": "5", "supersedes": "BM16", "reading": "BX16", "basis": "estimated",
        }])
        book = EvidenceBook(bundle, as_of_day=5)
        point = book.station_evidence("B").point("main", 240)
        self.assertEqual(point.kw, Decimal(30))
        self.assertEqual(point.basis, "estimated")
        self.assertEqual(point.reading, "BX16")
        # Before the correction was known, the interval is still blank.
        earlier = EvidenceBook(bundle, as_of_day=4).station_evidence("B").point("main", 240)
        self.assertIsNone(earlier.kw)

    def test_correction_after_source_reread_wins_on_latest_day(self):
        bundle = load_sources(SOURCES, extra_readings=[{
            "station": "A", "meter": "main", "timestamp_minute": "240", "kw": "52",
            "observed_day": "3", "supersedes": "AM16", "reading": "X1-1", "basis": "estimated",
        }])
        # AX16 and X1-1 both supersede AM16; the later day wins.
        book = EvidenceBook(bundle, as_of_day=5)
        point = book.station_evidence("A").point("main", 240)
        self.assertEqual(point.reading, "AX16")
        self.assertEqual(point.kw, Decimal(48))
        later = EvidenceBook(bundle, as_of_day=6).station_evidence("A").point("main", 240)
        self.assertEqual(later.reading, "AX16")

    def test_resolve_terminal_is_deterministic(self):
        bundle = load_sources(SOURCES)
        group = bundle.readings_by_interval()[("A", "main", 240)]
        self.assertEqual(resolve_terminal(group).reading, "AX16")

    def test_reduce_splits_reserve_window_and_takes_the_minimum(self):
        bundle = load_sources(SOURCES)
        day1 = EvidenceBook(bundle, as_of_day=1)
        self.assertEqual(day1.guaranteed_free("B", 195, 300)[:2], (Decimal(18), "verified"))
        day5 = EvidenceBook(bundle, as_of_day=5)
        free, state, gaps = day5.guaranteed_free("B", 195, 300)
        self.assertEqual(free, Decimal(10))
        self.assertEqual(state, "reduced")
        self.assertEqual(gaps, [])
        # Outside the reduced window the original 18 kW still stands.
        self.assertEqual(day5.guaranteed_free("B", 195, 225)[:2], (Decimal(18), "verified"))

    def test_reserve_evidence_never_shows_old_and_new_overlap(self):
        book = EvidenceBook(load_sources(SOURCES), as_of_day=5)
        rows = [r for r in book.reserve_evidence() if r.site == "B"]
        spans = [(r.from_min, r.to_min) for r in rows]
        for a, b in zip(spans, spans[1:]):
            self.assertLessEqual(a[1], b[0])

    def test_child_meters_are_not_an_extra_total(self):
        book = EvidenceBook(self.bundle, as_of_day=1)
        evidence = book.station_evidence("A")
        self.assertEqual(book.unattributed(evidence, 0), Decimal(12))
        self.assertEqual(book.supply_meter("A"), "main")


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_sources(SOURCES)
        self.book = EvidenceBook(self.bundle, as_of_day=1)
        self.service = fresh_service()
        self.service.init()

    def test_old_is_energy_only_new_adds_demand_and_reserve(self):
        old = quote_station(self.book, "A", "old")
        new = quote_station(self.book, "A", "new")
        self.assertEqual(old.subtotal("demand"), Decimal(0))
        self.assertEqual(old.subtotal("reserve"), Decimal(0))
        self.assertGreater(new.subtotal("demand"), Decimal(0))
        self.assertEqual(new.subtotal("reserve"), Decimal(22) * Decimal(19))

    def test_energy_matches_observed_kwh(self):
        quote = quote_station(self.book, "A", "old", window=(195, 300))
        kwh = quote.charges[0].quantity
        # 202 readings, 12 of them at 48 kW on the corrected 240 interval.
        expected = sum(
            point.kw for (meter, minute), point in self.book.station_evidence("A").points.items()
            if meter == "main" and point.kw is not None and 195 <= minute < 300
        ) * Decimal("0.25")
        self.assertEqual(kwh, expected)
        self.assertEqual(quote.charges[0].amount, expected * 80)

    def test_demand_uses_demonstrable_peak_not_blank_or_cap(self):
        quote = quote_station(self.book, "A", "new", window=(195, 300))
        self.assertEqual(quote.demand.peak_kw, Decimal(46))
        self.assertEqual(quote.demand.peak_minute, 225)
        self.assertIn((240, 255), quote.demand.unobserved)

    def test_reserve_is_concurrent_not_summed(self):
        profile = reserve_profile(self.book, "A", 195, 300)
        self.assertEqual(profile.requirement_kw, Decimal(22))  # 13 + 9 overlap at 225
        self.assertEqual(profile.peak_minute, 225)
        self.assertEqual([c["right"] for c in profile.contributors], ["R2", "R3"])

    def test_exposure_is_reported_but_not_billed(self):
        quote = quote_station(self.book, "A", "new")
        self.assertEqual(quote.billed_total, quote.subtotal("energy") + quote.subtotal("demand") + quote.subtotal("reserve"))
        self.assertGreater(quote.exposure_total, Decimal(0))
        self.assertEqual(quote.reserve.short_kw, Decimal(4))

    def test_no_tenant_receives_the_station_maximum(self):
        for right in self.bundle.guarantees.values():
            view = right_reserve_view(self.book, right)
            self.assertNotEqual(view["reserve_kw"], "48")
            self.assertIn("not allocated", view["demand_note"])
            self.assertEqual(Decimal(view["reserve_kw"]), right.reserve_kw)

    def test_comparison_pins_evidence_revision(self):
        result = self.service.compare("A")
        self.assertEqual(result["evidence"]["revision"], 1)
        self.assertEqual(result["cheaper"], "old")
        self.assertEqual(
            Decimal(result["delta_cent"]),
            Decimal(result["new"]["billed_total_cent"]) - Decimal(result["old"]["billed_total_cent"]),
        )

    def test_cap_breach_splits_customers_three_ways(self):
        # Evaluate with all paperwork known (as of the latest day).
        book = EvidenceBook(self.bundle, as_of_day=6)
        quote = quote_station(book, "A", "new")
        options = renegotiation_options(book, "A", quote)
        self.assertTrue(options["triggered"])
        # R1 is old terms in the source but its handoff has already been
        # received, so it is an effective commitment that must be performed.
        self.assertEqual(options["must_perform"], ["R1"])
        self.assertNotIn("R1", options["renegotiate_not_effective"])
        self.assertIn("R2", options["temporary_alternative"])
        self.assertIn("R3", options["temporary_alternative"])

    def test_cap_breach_respects_live_migration_state(self):
        self.service.record_event("E1", "R1", "handoff_received", 255, "B", day=6)
        result = self.service.compare("A")
        self.assertIn("R1", result["cap_consequences"]["must_perform"])
        self.assertNotIn("R1", result["cap_consequences"]["renegotiate_not_effective"])

    def test_relocation_shows_costs_that_stay_behind(self):
        result = relocate(self.book, self.bundle.guarantees["R1"], "B", (225, 270))
        self.assertEqual(result["destination_reserve_after_move"]["requirement_kw"], "12")
        self.assertEqual(result["origin_reserve_after_move"]["requirement_kw"], "22")
        self.assertGreater(Decimal(result["still_borne_cent"]["total"]), Decimal(0))
        low, high = (Decimal(x) for x in result["origin_peak_after_move_band_kw"])
        self.assertLess(low, high)

    def test_relocation_under_reduced_inventory_shows_shortfall(self):
        book = EvidenceBook(self.bundle, as_of_day=5)
        result = relocate(book, self.bundle.guarantees["R1"], "B", (225, 270))
        self.assertEqual(result["destination_reserve_after_move"]["inventory_state"], "reduced")
        self.assertEqual(result["destination_reserve_after_move"]["short_kw"], "2")

    def test_explicit_window_is_respected(self):
        quote = quote_station(self.book, "A", "new", window=(225, 270))
        self.assertEqual(quote.window, (225, 270))
        self.assertEqual(quote.demand.peak_kw, Decimal(46))
        # Outside the window the 48 kW interval is excluded from demand...
        self.assertLess(quote.demand.peak_kw, Decimal(48))
        # ...but never silently filled in as zero.
        self.assertIn((240, 255), quote.demand.unobserved)


class ReconciliationTests(unittest.TestCase):
    def test_source_has_one_blank_interval(self):
        bundle = load_sources(SOURCES)
        result = reconcile(bundle, "B")
        self.assertEqual(result.live, result.intervals)
        self.assertEqual(len(result.unreconciled), 1)
        self.assertIn("reconstructable", result.unreconciled[0][1])

    def test_correction_fills_the_gap(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        service.correct("X1", 5, [("B", "main", 240, "30")], "late read")
        bundle = service.validate()
        result = reconcile(bundle, "B")
        self.assertEqual(result.unreconciled, [])
        book = EvidenceBook(bundle, as_of_day=5)
        self.assertEqual(book.station_evidence("B").point("main", 240).kw, Decimal(30))

    def test_correction_cannot_predate_its_target(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        with self.assertRaises(CorrectionError):
            service.correct("X1", 0, [("B", "main", 240, "30")], "too early")

    def test_negative_value_rejected_outside_buffers(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        with self.assertRaises(CorrectionError):
            service.correct("X1", 5, [("B", "main", 240, "-3")], "bad")
        # Buffers are allowed to go negative.
        service.correct("X2", 5, [("B", "buffer", 240, "-3")], "buffer discharge")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.service = fresh_service()
        self.service.init()

    def test_partial_signature_keeps_old_carrier_until_handoff(self):
        state = self.service.migration("R1")
        self.assertEqual(state["phase"], "handoff_received")
        self.assertFalse(state["original_carrier_still_in_force"])
        self.assertEqual(state["moved_kw"], "32")

    def test_request_rejects_reduced_inventory(self):
        verdict = self.service.check_request("R1", "B", (225, 270))
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["code"], "reduced")

    def test_request_rejects_window_outside_right(self):
        verdict = self.service.check_request("R1", "B", (180, 270))
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["code"], "outside-window")

    def test_request_accepts_verified_day_one(self):
        verdict = self.service.check_request("R1", "B", (225, 270), day=1)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["available_kw"], "18")

    def test_second_incompatible_request_is_not_confirmed(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        service.record_event("E1", "R1", "offered", 225, "B", day=1)
        with self.assertRaises(TransitionError):
            service.record_event("E2", "R1", "offered", 225, "A", day=1)

    def test_instalments_are_cumulative_and_bounded(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        service.record_migration("M1", "R1", "B", 240, 2, Decimal(16), "first instalment")
        state = service.migration("R1")
        self.assertEqual(state["moved_kw"], "16")
        self.assertEqual(state["remaining_kw"], "16")
        self.assertTrue(state["dual_terms_in_force"])
        service.accept_migration("M1", 6, "rest signed")
        state = service.migration("R1")
        self.assertEqual(state["phase"], "accepted")
        self.assertEqual(state["remaining_kw"], "0")

    def test_withdrawal_restores_original_clause(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        service.record_migration("M1", "R1", "B", 240, 2, Decimal(16), "first")
        service.withdraw_migration("M1", 3, "paused")
        state = service.migration("R1")
        self.assertEqual(state["phase"], "handoff_received")
        self.assertEqual(state["moved_kw"], "32")

    def test_instalments_above_firm_capacity_rejected(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        with self.assertRaises(TransitionError):
            service.record_migration("M1", "R1", "B", 240, 2, Decimal(40), "too much")


class ReproducibilityTests(unittest.TestCase):
    def test_same_revision_reproduces_same_numbers(self):
        service = fresh_service()
        service.init()
        first = service.compare("A")
        second = service.compare("A")
        self.assertEqual(first, second)
        self.assertEqual(first["evidence"]["snapshot_digest"], second["evidence"]["snapshot_digest"])

    def test_revision_replay_after_correction(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        baseline = service.compare("B", (195, 300), day=1)
        service.correct("X1", 5, [("B", "main", 240, "30")], "late read")
        replayed = service.compare("B", (195, 300), day=1, revision=1)
        self.assertEqual(baseline["delta_cent"], replayed["delta_cent"])
        current = service.compare("B", (195, 300), day=5)
        self.assertNotEqual(current["delta_cent"], baseline["delta_cent"])

    def test_settlement_refuses_silent_correction(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        service.settle("S1", "B", (195, 300), at_day=1, note="baseline")
        with self.assertRaises(CorrectionError):
            service.correct("X1", 5, [("B", "main", 240, "30")], "late read")
        result = service.correct("X1", 5, [("B", "main", 240, "30")], "late read", restate=True)
        self.assertEqual(len(result["restatements"]), 1)
        restatement = result["restatements"][0]
        self.assertEqual(restatement["settled_revision"], 1)
        # The original analysis stays readable, and it does change.
        replayed = service.compare("B", (195, 300), day=1, revision=1)
        self.assertEqual(replayed["new"]["billed_total_cent"], restatement["before"]["new_cent"])
        self.assertNotEqual(restatement["after"]["new_cent"], restatement["before"]["new_cent"])

    def test_drift_detected(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        self.assertEqual(service.drift(), [])
        rows = (workspace.path / "tariffs.csv").read_text(encoding="utf-8")
        workspace.rewrite("tariffs", rows.replace("80,0,0", "81,0,0"))
        self.assertTrue(any("changed since the snapshot" in f["message"] for f in service.drift()))

    def test_ledger_digest_changes_with_each_revision(self):
        service = fresh_service()
        revision = service.init()
        first = service.context(revision).digest
        service.record_event("E1", "R2", "offered", 225, "A", day=1)
        second = service.context().digest
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()


class IncompatibleRequestTests(unittest.TestCase):
    """A customer holding two rights that cannot both be served."""

    def setUp(self):
        self.workspace = Workspace()
        # Give U1 a second right whose window overlaps R1 on a different slot.
        rows = (self.workspace.path / "guarantees.csv").read_text(encoding="utf-8")
        rows += "R4,U1,B,A,270,300,10,8,new\n"
        self.workspace.rewrite("guarantees", rows)
        self.service = self.workspace.service()
        self.service.init()

    def test_two_sites_for_same_right_cannot_both_stand(self):
        first = self.service.check_request("R4", "A", (270, 300), day=1)
        self.assertTrue(first["ok"], first)
        self.service.record_event("E1", "R4", "offered", 270, "A", day=2)
        second = self.service.check_request("R4", "B", (270, 300))
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "incompatible-request")

    def test_withdrawal_allows_the_other_site(self):
        self.service.record_event("E1", "R4", "offered", 270, "A", day=2)
        blocked = self.service.check_request("R4", "B", (270, 300))
        self.assertEqual(blocked["code"], "incompatible-request")
        self.service.record_event("E2", "R4", "withdrawn", 270, "", day=3)
        withdrawal = self.service.migration("R4")
        self.assertEqual(withdrawal["phase"], "withdrawn")
        retry = self.service.check_request("R4", "B", (270, 300), day=1)
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(retry["available_kw"], "18")

    def test_partial_migration_on_one_right_does_not_leak_to_another(self):
        self.service.record_migration("M1", "R4", "A", 270, 2, Decimal(4), "part")
        self.assertEqual(self.service.migration("R4")["moved_kw"], "4")
        self.assertEqual(self.service.migration("R1")["moved_kw"], "32")


class EvidenceTieBreakTests(unittest.TestCase):
    def test_operator_correction_wins_a_same_day_tie(self):
        bundle = load_sources(SOURCES, extra_readings=[{
            "station": "A", "meter": "main", "timestamp_minute": "240", "kw": "51",
            "observed_day": "5", "supersedes": "AM16", "reading": "A0-1", "basis": "estimated",
        }])
        point = EvidenceBook(bundle, as_of_day=5).station_evidence("A").point("main", 240)
        self.assertEqual(point.reading, "A0-1")
        self.assertEqual(point.kw, Decimal(51))
        self.assertEqual(point.basis, "estimated")

    def test_children_exceeding_boundary_show_negative_unattributed(self):
        bundle = load_sources(SOURCES, extra_readings=[{
            "station": "A", "meter": "main", "timestamp_minute": "0", "kw": "5",
            "observed_day": "5", "supersedes": "AM0", "reading": "A0-5",
        }])
        book = EvidenceBook(bundle, as_of_day=5)
        evidence = book.station_evidence("A")
        self.assertLess(book.unattributed(evidence, 0), Decimal(0))


class SourceValidationTests(unittest.TestCase):
    def test_children_above_boundary_is_a_warning(self):
        workspace = Workspace()
        rows = (workspace.path / "meter_readings.csv").read_text(encoding="utf-8")
        workspace.rewrite("meter_readings", rows.replace("A,main,0,21,1,,AM0", "A,main,0,5,1,,AM0"))
        bundle = load_sources(workspace.path)
        codes = {f.code for f in bundle.report.findings}
        self.assertIn("children_exceed_boundary", codes)
        self.assertFalse(bundle.report.errors)

    def test_unknown_meter_and_station_are_errors(self):
        workspace = Workspace()
        rows = (workspace.path / "meter_readings.csv").read_text(encoding="utf-8")
        workspace.rewrite(
            "meter_readings",
            rows.replace("A,main,0,21,1,,AM0", "Z,main,0,21,1,,AM0", 1),
        )
        bundle = load_sources(workspace.path)
        self.assertTrue(any(f.code == "reading_station" for f in bundle.report.errors))

    def test_negative_non_buffer_reading_is_an_error(self):
        workspace = Workspace()
        rows = (workspace.path / "meter_readings.csv").read_text(encoding="utf-8")
        workspace.rewrite("meter_readings", rows.replace("A,main,0,21,1,,AM0", "A,main,0,-21,1,,AM0"))
        bundle = load_sources(workspace.path)
        self.assertTrue(any(f.code == "reading_negative" for f in bundle.report.errors))

    def test_bad_rollback_reference_is_an_error(self):
        workspace = Workspace()
        rows = (workspace.path / "meter_readings.csv").read_text(encoding="utf-8")
        workspace.rewrite("meter_readings", rows.replace("AM16,AX16", "NOPE,AX16"))
        bundle = load_sources(workspace.path)
        self.assertTrue(any(f.code == "supersedes_missing" for f in bundle.report.errors))


class AsOfTests(unittest.TestCase):
    def setUp(self):
        self.service = fresh_service()
        self.service.init()

    def test_paperwork_after_the_as_of_day_is_invisible(self):
        day5 = self.service.migration("R1", day=5)
        day6 = self.service.migration("R1", day=6)
        self.assertEqual(day5["phase"], "partial_signed")
        self.assertTrue(day5["original_carrier_still_in_force"])
        self.assertEqual(day6["phase"], "handoff_received")
        self.assertFalse(day6["original_carrier_still_in_force"])

    def test_acceptance_after_the_as_of_day_does_not_take_effect(self):
        workspace = Workspace()
        service = workspace.service()
        service.init()
        service.record_migration("M1", "R1", "B", 240, 2, Decimal(16), "part")
        service.accept_migration("M1", 6, "rest")
        day5 = service.migration("R1", day=5)
        self.assertEqual(day5["phase"], "partial_signed")
        self.assertEqual(day5["moved_kw"], "16")
        self.assertTrue(day5["original_carrier_still_in_force"])
        day6 = service.migration("R1", day=6)
        self.assertEqual(day6["phase"], "accepted")
        self.assertEqual(day6["remaining_kw"], "0")

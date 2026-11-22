"""Reprice the same evidence under old (energy + demand) and new (demand + reserve) terms.

Why comparisons stay at station level
-------------------------------------
Demand is measured at the supply boundary.  The aggregate tenant meters cannot
say which client drew which interval, so this module never copies a station
peak onto a tenant, and never splits one interval's maximum among tenants.  A
client's own numbers are limited to the capacity it actually contracted
(``firm_kw`` / ``reserve_kw``), which is written in the contract, not measured.

Counting rules
--------------
old  energy + demand, no reserve charge.
new  energy + demand + reserve on the maximum *concurrent* reserve capacity
     promised at the site (never the sum of overlapping contracts).

Evidence rules
--------------
* One snapshot, one as-of day for both sides.
* Energy is observed kWh at the boundary; unobserved intervals add no energy.
* Demand is the demonstrable peak.  A window with unobserved intervals also
  reports a low/high band, so a missing reading is neither billed at zero nor
  replaced by an invented exact peak.
* Reserve is only ``confirmed`` when verified inventory covers the window and
  the site's concurrent commitments fit inside what the latest evidence
  supports.  A ``reduced`` row still states real capacity; it just cannot
  confirm a commitment larger than that capacity.
* Load the boundary sees but no tenant meter explains is reported as
  unattributed.  It is never billed to a tenant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from .errors import BillingError
from .evidence import DemandStatement, EvidenceBook
from .money import ZERO, q2
from .sources import Guarantee, Tariff
from .transitions import evaluate_migration


BILLED_KINDS = {"energy", "demand", "reserve"}
EXPOSURE_KINDS = {"demand-band", "reserve-shortfall"}


@dataclass(slots=True)
class Charge:
    kind: str  # energy | demand | reserve | demand-band | reserve-shortfall
    basis: str
    quantity: Decimal
    unit: str
    rate: Decimal
    amount: Decimal
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "basis": self.basis,
            "quantity": format(self.quantity, "f"),
            "unit": self.unit,
            "rate": format(self.rate, "f"),
            "amount": format(self.amount, "f"),
            "note": self.note,
        }


@dataclass(slots=True)
class Gap:
    station: str
    start: int
    end: int
    reason: str
    low_kw: Decimal
    high_kw: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "station": self.station,
            "window": [self.start, self.end],
            "reason": self.reason,
            "band_kw": [format(self.low_kw, "f"), format(self.high_kw, "f")],
        }


@dataclass(slots=True)
class ReserveProfile:
    site: str
    start: int
    end: int
    requirement_kw: Decimal
    peak_minute: int | None
    contributors: list[dict[str, object]] = field(default_factory=list)
    free_kw: Decimal = ZERO
    inventory_state: str = "none"
    short_kw: Decimal = ZERO
    unconfirmed: list[tuple[int, int]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "site": self.site,
            "window": [self.start, self.end],
            "requirement_kw": format(self.requirement_kw, "f"),
            "peak_minute": self.peak_minute,
            "contributors": self.contributors,
            "free_kw": format(self.free_kw, "f"),
            "inventory_state": self.inventory_state,
            "short_kw": format(self.short_kw, "f"),
            "unconfirmed": [list(span) for span in self.unconfirmed],
            "confirmed": (
                self.inventory_state == "verified"
                and self.short_kw == 0
                and not self.unconfirmed
            ),
        }


@dataclass(slots=True)
class Quote:
    label: str
    station: str
    tariff: str
    window: tuple[int, int]
    peak_applies: bool
    reserve_applies: bool
    charges: list[Charge] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    demand: DemandStatement | None = None
    reserve: ReserveProfile | None = None
    cap_kw: Decimal = ZERO
    cap_breach_kw: Decimal = ZERO

    @property
    def billed_total(self) -> Decimal:
        return q2(sum((c.amount for c in self.charges if c.kind in BILLED_KINDS), ZERO))

    @property
    def exposure_total(self) -> Decimal:
        return q2(sum((c.amount for c in self.charges if c.kind in EXPOSURE_KINDS), ZERO))

    @property
    def demand_band_total(self) -> Decimal:
        return q2(sum((c.amount for c in self.charges if c.kind == "demand-band"), ZERO))

    @property
    def reserve_shortfall_total(self) -> Decimal:
        return q2(sum((c.amount for c in self.charges if c.kind == "reserve-shortfall"), ZERO))

    def subtotal(self, kind: str) -> Decimal:
        return q2(sum((c.amount for c in self.charges if c.kind == kind), ZERO))

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "station": self.station,
            "tariff": self.tariff,
            "window": list(self.window),
            "rules": {"demand": self.peak_applies, "reserve": self.reserve_applies},
            "charges": [c.as_dict() for c in self.charges],
            "billed_total_cent": format(self.billed_total, "f"),
            "exposure_cent": format(self.exposure_total, "f"),
            "subtotals_cent": {
                "energy": format(self.subtotal("energy"), "f"),
                "demand": format(self.subtotal("demand"), "f"),
                "reserve": format(self.subtotal("reserve"), "f"),
            },
            "exposure_by_kind_cent": {
                "demand_band": format(self.demand_band_total, "f"),
                "reserve_shortfall": format(self.reserve_shortfall_total, "f"),
            },
            "demand": self.demand.as_dict() if self.demand else None,
            "gaps": [g.as_dict() for g in self.gaps],
            "cap": {
                "cap_kw": format(self.cap_kw, "f"),
                "breach_kw": format(self.cap_breach_kw, "f"),
            },
            "reserve": self.reserve.as_dict() if self.reserve else None,
        }


def unattributed_summary(book: EvidenceBook, station: str, window: tuple[int, int]) -> dict[str, object]:
    """Load that the boundary sees but no tenant meter explains.

    This is the "has consumption but cannot be tied to a customer" bucket.  It
    is never billed to a tenant; it is either station-internal consumption or a
    sign that a child meter is missing or double-counted.
    """
    evidence = book.station_evidence(station)
    minutes = [
        minute for minute in book.window_minutes(*window)
        if book.unattributed(evidence, minute) is not None
    ]
    values = {minute: book.unattributed(evidence, minute) for minute in minutes}
    negative = [minute for minute, value in values.items() if value is not None and value < 0]
    energy = sum((value for value in values.values() if value is not None), ZERO) * Decimal("0.25")
    return {
        "station": station,
        "window": list(window),
        "intervals": len(values),
        "energy_kwh": format(energy, "f"),
        "max_kw": format(max(values.values(), default=ZERO), "f"),
        "negative_intervals": negative,
        "note": (
            "negative unattributed means components exceed the boundary: a meter "
            "may be double-counted or the supply reading is too low"
        ),
    }


def renegotiation_options(
    book: EvidenceBook,
    station: str,
    quote: Quote,
    *,
    effective: dict[str, bool] | None = None,
    manual_migrations: list[dict] | None = None,
) -> dict[str, object]:
    """Distinguish what a cap breach can and cannot change.

    * ``must_perform``           already-effective commitments; the cap cannot
                                 cancel them, so they continue as written.
    * ``renegotiate_not_effective`` offers / unsigned plans; these can still be
                                 repriced or withdrawn before they bind.
    * ``temporary_alternative``  clients relying on this station's reserve; the
                                 breach shifts reserve elsewhere, so they need a
                                 temporary substitute rather than a new contract.

    The split uses live migration states, not tariff names.
    """
    if quote.cap_breach_kw <= 0:
        return {"triggered": False, "cap_kw": format(quote.cap_kw, "f"), "breach_kw": "0"}
    from .transitions import is_effective

    must: list[str] = []
    pending: list[str] = []
    substitute: list[str] = []
    for right in sorted(book.bundle.guarantees.values(), key=lambda r: r.right):
        if effective is not None and right.right in effective:
            live = effective[right.right]
        else:
            state = evaluate_migration(
                book, right,
                [e for e in book.bundle.transitions if e.right == right.right],
                manual=manual_migrations,
            )
            live = is_effective(state)
        if right.home == station:
            (must if live else pending).append(right.right)
        if right.reserve_site == station and right.reserve_kw > 0:
            substitute.append(right.right)
    return {
        "triggered": True,
        "cap_kw": format(quote.cap_kw, "f"),
        "breach_kw": format(quote.cap_breach_kw, "f"),
        "must_perform": must,
        "renegotiate_not_effective": pending,
        "temporary_alternative": sorted(set(substitute) - set(must)),
    }


def _window_for_station(book: EvidenceBook, station: str) -> tuple[int, int]:
    """Default historical window: every contract touching the station.

    The analyst can always pass an explicit window; the default must not clip
    away part of a contract, so it spans the union, capped to the data.
    """
    windows = [
        (g.start_minute, g.end_minute)
        for g in book.bundle.guarantees.values()
        if g.home == station or g.reserve_site == station
    ]
    if windows:
        start = min(w[0] for w in windows)
        end = max(w[1] for w in windows)
        return start, min(end, book.bundle.data_end_minute(station) or end)
    return 0, book.bundle.data_end_minute(station)


def reserve_profile(
    book: EvidenceBook,
    site: str,
    start: int,
    end: int,
    *,
    replace: dict[str, str] | None = None,
    add: Iterable[Guarantee] = (),
) -> ReserveProfile:
    """Maximum concurrent reserve commitment at ``site`` over [start, end).

    ``replace`` remaps a right's reserve site (relocation scenarios); ``add``
    injects a hypothetical right.  Concurrent, not summed: at each 15-minute
    step only rights whose own window is live count.
    """
    replace = replace or {}
    rights = list(book.bundle.guarantees.values()) + list(add)
    assignments = [
        (right, replace.get(right.right, right.reserve_site))
        for right in rights
        if replace.get(right.right, right.reserve_site) == site
    ]

    best = ZERO
    best_minute: int | None = None
    best_contributors: list[Guarantee] = []
    for minute in range(start, end, 15):
        total = ZERO
        here: list[Guarantee] = []
        for right, _ in assignments:
            if right.start_minute <= minute < right.end_minute:
                total += right.reserve_kw
                here.append(right)
        if total > best:
            best, best_minute, best_contributors = total, minute, here

    free, inventory_state, gaps = book.guaranteed_free(site, start, end)
    short = max(best - free, ZERO)
    return ReserveProfile(
        site=site,
        start=start,
        end=end,
        requirement_kw=best,
        peak_minute=best_minute,
        contributors=[
            {
                "right": r.right,
                "client": r.client,
                "reserve_kw": format(r.reserve_kw, "f"),
                "contract": r.contract,
            }
            for r in sorted(best_contributors, key=lambda r: r.right)
        ],
        free_kw=free,
        inventory_state=inventory_state,
        short_kw=short,
        unconfirmed=gaps,
    )


def quote_station(
    book: EvidenceBook,
    station: str,
    tariff_name: str,
    window: tuple[int, int] | None = None,
    *,
    label: str | None = None,
    include_estimated: bool = False,
    replace: dict[str, str] | None = None,
    add: Iterable[Guarantee] = (),
) -> Quote:
    bundle = book.bundle
    if tariff_name not in bundle.tariffs:
        raise BillingError(f"unknown tariff {tariff_name!r}")
    tariff: Tariff = bundle.tariffs[tariff_name]
    start, end = window or _window_for_station(book, station)
    if not 0 <= start < end <= 1440:
        raise BillingError(f"invalid window [{start}, {end})")
    evidence = book.station_evidence(station)
    quote = Quote(
        label=label or f"{station}:{tariff_name}",
        station=station,
        tariff=tariff_name,
        window=(start, end),
        peak_applies=tariff.demand_cent_per_kw > 0,
        reserve_applies=tariff.reserve_cent_per_kw > 0,
        cap_kw=tariff.cap_kw,
    )

    kwh, gaps = book.metered_kwh(evidence, book.supply_meter(station), start, end)
    quote.charges.append(Charge(
        "energy", "observed kWh at the supply boundary", kwh, "kWh",
        tariff.energy_cent_per_kwh, q2(kwh * tariff.energy_cent_per_kwh),
        "unobserved intervals add no energy" if gaps else "",
    ))
    for span in gaps:
        quote.gaps.append(Gap(
            station, span[0], span[1], "supply boundary not observed",
            book.component_floor(evidence, span[0]) or ZERO,
            book.supply_ceiling(evidence),
        ))

    if quote.peak_applies:
        demand = book.demand_statement(evidence, start, end, include_estimated=include_estimated)
        quote.demand = demand
        quote.charges.append(Charge(
            "demand", f"demonstrable peak ({demand.basis})", demand.peak_kw, "kW",
            tariff.demand_cent_per_kw, q2(demand.peak_kw * tariff.demand_cent_per_kw),
            "" if not demand.unobserved else "unobserved band reported separately",
        ))
        if demand.unobserved:
            low_amount = q2(demand.low_kw * tariff.demand_cent_per_kw)
            high_amount = q2(demand.high_kw * tariff.demand_cent_per_kw)
            quote.charges.append(Charge(
                "demand-band", "unobserved demand band (not billed)",
                demand.high_kw - demand.low_kw, "kW", tariff.demand_cent_per_kw,
                high_amount - low_amount,
                f"true peak lies between {format(demand.low_kw, 'f')} and "
                f"{format(demand.high_kw, 'f')} kW; billed at the observed maximum",
            ))
        if demand.peak_kw > tariff.cap_kw:
            quote.cap_breach_kw = demand.peak_kw - tariff.cap_kw

    if quote.reserve_applies:
        profile = reserve_profile(book, station, start, end, replace=replace, add=add)
        quote.reserve = profile
        quote.charges.append(Charge(
            "reserve", "maximum concurrent reserve capacity", profile.requirement_kw, "kW",
            tariff.reserve_cent_per_kw, q2(profile.requirement_kw * tariff.reserve_cent_per_kw),
            f"site {station}; {profile.inventory_state} free {format(profile.free_kw, 'f')} kW",
        ))
        if profile.short_kw > 0:
            quote.charges.append(Charge(
                "reserve-shortfall", "unconfirmed reserve exposure (not billed)", profile.short_kw, "kW",
                tariff.reserve_cent_per_kw, q2(profile.short_kw * tariff.reserve_cent_per_kw),
                f"{format(profile.short_kw, 'f')} kW of the concurrent commitment is not "
                f"backed by verified inventory at {station}",
            ))
    return quote


def compare_station(
    book: EvidenceBook,
    station: str,
    window: tuple[int, int] | None = None,
    *,
    include_estimated: bool = False,
    replace: dict[str, str] | None = None,
    add: Iterable[Guarantee] = (),
) -> dict[str, object]:
    """Station-level old-versus-new comparison on identical evidence."""
    old = quote_station(book, station, "old", window, label="old-terms",
                        include_estimated=include_estimated, replace=replace, add=add)
    new = quote_station(book, station, "new", window, label="new-terms",
                        include_estimated=include_estimated, replace=replace, add=add)
    delta = q2(new.billed_total - old.billed_total)
    breakdown = {
        kind: format(q2(new.subtotal(kind) - old.subtotal(kind)), "f")
        for kind in ("energy", "demand", "reserve")
    }
    return {
        "station": station,
        "window": list(new.window),
        "old": old.as_dict(),
        "new": new.as_dict(),
        "delta_cent": format(delta, "f"),
        "delta_by_kind_cent": breakdown,
        "cheaper": "old" if delta > 0 else "new" if delta < 0 else "tie",
        "exposure_cent": format(new.exposure_total, "f"),
        "unattributed": unattributed_summary(book, station, tuple(new.window)),
    }


def relocate(
    book: EvidenceBook,
    guarantee: Guarantee,
    destination: str,
    window: tuple[int, int],
    *,
    reserve_site: str | None = None,
    include_estimated: bool = False,
) -> dict[str, object]:
    """Move one right to a temporary site; show every cost that stays behind.

    Origin peak reduction is bounded by the client's firm capacity and shown as
    a band, because aggregate meters cannot attribute one client's intervals.
    Reserve is recomputed as concurrent capacity at both sites, using the same
    inventory evidence the operator sees on that day.
    """
    bundle = book.bundle
    start, end = window
    if not 0 <= start < end <= 1440:
        raise BillingError(f"invalid relocation window {window}")
    if destination not in bundle.stations:
        raise BillingError(f"unknown destination station {destination!r}")
    target_site = reserve_site or destination

    origin_evidence = book.station_evidence(guarantee.home)
    origin_peak = book.demand_statement(origin_evidence, start, end, include_estimated=include_estimated)
    residual_low = max(ZERO, origin_peak.low_kw - guarantee.firm_kw)
    residual_high = max(ZERO, origin_peak.high_kw - guarantee.firm_kw)

    moved = {guarantee.right: "__moved__"}
    origin_after = reserve_profile(book, guarantee.home, start, end, replace=moved)
    destination_after = reserve_profile(book, target_site, start, end,
                                         replace={guarantee.right: target_site})
    new_rate = bundle.tariffs["new"].reserve_cent_per_kw
    origin_cost = q2(origin_after.requirement_kw * new_rate)
    destination_cost = q2(destination_after.requirement_kw * new_rate)
    short_cost = q2(destination_after.short_kw * new_rate)
    return {
        "right": guarantee.right,
        "client": guarantee.client,
        "from": guarantee.home,
        "to": destination,
        "reserve_site": target_site,
        "window": [start, end],
        "effective_window_covers_move": guarantee.start_minute <= start and end <= guarantee.end_minute,
        "origin_peak": origin_peak.as_dict(),
        "origin_peak_after_move_band_kw": [format(residual_low, "f"), format(residual_high, "f")],
        "origin_peak_note": (
            "aggregate meters cannot attribute which client drew an interval; the "
            f"reduction is bounded by firm_kw {format(guarantee.firm_kw, 'f')}"
        ),
        "origin_reserve_after_move": origin_after.as_dict(),
        "destination_reserve_after_move": destination_after.as_dict(),
        "still_borne_cent": {
            "origin_reserve_capacity": format(origin_cost, "f"),
            "destination_reserve_capacity": format(destination_cost, "f"),
            "destination_shortfall_exposure": format(short_cost, "f"),
            "total": format(q2(origin_cost + destination_cost), "f"),
        },
        "still_borne_note": (
            "moving a client frees its firm capacity at the origin but not the "
            "origin's remaining reserve promise; at the destination the shared "
            "slot may already be committed or reduced"
        ),
    }


def right_reserve_view(book: EvidenceBook, right: Guarantee) -> dict[str, object]:
    """Per-right reserve facts; deliberately no station demand allocation."""
    profile = reserve_profile(book, right.reserve_site, right.start_minute, right.end_minute)
    rows = book.reserve_covering(right.reserve_site, right.start_minute, right.end_minute)
    weakest = min(rows, key=lambda r: (r.state != "verified", r.free_kw)) if rows else None
    return {
        "right": right.right,
        "client": right.client,
        "home": right.home,
        "reserve_site": right.reserve_site,
        "window": [right.start_minute, right.end_minute],
        "reserve_kw": format(right.reserve_kw, "f"),
        "firm_kw": format(right.firm_kw, "f"),
        "inventory": None if weakest is None else {
            "free_kw": format(weakest.free_kw, "f"),
            "state": weakest.state,
            "known_day": weakest.known_day,
        },
        "evidence_rows": [
            {
                "window": [r.from_min, r.to_min],
                "free_kw": format(r.free_kw, "f"),
                "state": r.state,
                "known_day": r.known_day,
            }
            for r in rows
        ],
        "concurrent_requirement_kw": format(profile.requirement_kw, "f"),
        "site_free_kw": format(profile.free_kw, "f"),
        "site_short_kw": format(profile.short_kw, "f"),
        "inventory_state": profile.inventory_state,
        "unconfirmed": [list(span) for span in profile.unconfirmed],
        "reserve_requirement_kw": format(right.reserve_kw, "f"),
        "confirmed": (
            profile.inventory_state == "verified"
            and not profile.unconfirmed
            and profile.free_kw >= profile.requirement_kw
        ),
        "confirmation_note": (
            "confirmed only when verified inventory covers the right's whole window "
            "and still fits every concurrent commitment at that site"
        ),
        "demand_note": "station peak is not allocated to this client; only contracted capacity is reported",
    }


def day_curve(book: EvidenceBook, station: str) -> list[dict[str, object]]:
    """Evidence curve per interval, with bands where the boundary is missing."""
    evidence = book.station_evidence(station)
    supply = book.supply_meter(station)
    rows: list[dict[str, object]] = []
    for minute in evidence.minutes():
        point = evidence.point(supply, minute)
        if point is not None and point.kw is not None:
            rows.append({
                "minute": minute,
                "kw": format(point.kw, "f"),
                "basis": point.basis,
                "reading": point.reading,
                "day": point.observed_day,
            })
            continue
        floor = book.component_floor(evidence, minute)
        rows.append({
            "minute": minute,
            "kw": None,
            "basis": "blank",
            "band_kw": [
                format(floor or ZERO, "f"),
                format(book.supply_ceiling(evidence), "f"),
            ],
            "unattributed": None if book.unattributed(evidence, minute) is None
            else format(book.unattributed(evidence, minute), "f"),
        })
    return rows

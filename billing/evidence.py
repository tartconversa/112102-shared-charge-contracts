"""Demand evidence, as of a day, without inventing observations.

Resolution rules
----------------
* A reading may supersede exactly one earlier reading for the same interval;
  the live terminal is authoritative.  Readings observed after the as-of day
  are invisible, so an operator can replay what was knowable then.
* Blanks are *not* zero.  An unobserved supply interval becomes a documented
  band: the boundary cannot be below the metered components that did survive
  (the station-internal load is physically non-negative), and cannot be stated
  above the supply meter's nameplate / observed maximum.
* Buffer meters are signed: positive charging from the site, negative
  discharging into the site.  They are components of the boundary, never an
  extra total.
* Reserve inventory is per site.  A later ``reduced`` row replaces the matching
  part of earlier evidence, and a window's guaranteeable free capacity is the
  *minimum* across that window, not whichever row happens to cover its start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .money import ZERO, merge_spans, uncovered
from .sources import Reading, SourceBundle

COMPONENT_ROLES = {"tenant", "buffer"}


def resolve_terminal(group: list[Reading]) -> Reading | None:
    """Pick the authoritative reading for one interval.

    ``supersedes`` names the reading this row replaces.  Rows form a directed
    forest: the live reading is one that nothing else supersedes.  When two
    readings both supersede the same target (a correction inserted between two
    source re-reads), the latest observed day wins; on the same day an operator
    correction beats a source row, then the label breaks the tie.  The choice is
    deterministic, so a revision always replays to the same number.
    """
    if not group:
        return None
    superseded = {r.supersedes for r in group if r.supersedes}
    terminals = [r for r in group if r.reading not in superseded]
    if not terminals:
        # A cycle: no reading is authoritative.
        return None
    return max(
        terminals,
        key=lambda r: (r.observed_day, r.origin == "ledger", r.reading),
    )


@dataclass(slots=True)
class MeterPoint:
    station: str
    meter: str
    role: str
    minute: int
    kw: Decimal | None
    basis: str  # observed | estimated | blank
    observed_day: int | None
    reading: str | None


@dataclass(slots=True)
class StationEvidence:
    station: str
    as_of_day: int
    points: dict[tuple[str, int], MeterPoint] = field(default_factory=dict)

    def point(self, meter: str, minute: int) -> MeterPoint | None:
        return self.points.get((meter, minute))

    def observed_max_day(self) -> Decimal:
        values = [p.kw for p in self.points.values() if p.kw is not None]
        return max(values, default=ZERO)

    def minutes(self) -> list[int]:
        return sorted({minute for _, minute in self.points})


@dataclass(frozen=True, slots=True)
class DemandStatement:
    """A demonstrable peak demand and, if evidence is missing, its band."""

    station: str
    start: int
    end: int
    peak_kw: Decimal
    peak_minute: int
    basis: str
    low_kw: Decimal
    high_kw: Decimal
    unobserved: tuple[tuple[int, int], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "station": self.station,
            "window": [self.start, self.end],
            "peak_kw": format(self.peak_kw, "f"),
            "peak_minute": self.peak_minute,
            "basis": self.basis,
            "band_kw": [format(self.low_kw, "f"), format(self.high_kw, "f")],
            "unobserved": [list(span) for span in self.unobserved],
            "complete": not self.unobserved,
        }


@dataclass(frozen=True, slots=True)
class ReserveEvidence:
    site: str
    from_min: int
    to_min: int
    free_kw: Decimal
    state: str
    known_day: int


class EvidenceBook:
    """As-of view of readings and reserve evidence for a chosen day."""

    def __init__(self, bundle: SourceBundle, as_of_day: int | None = None):
        self.bundle = bundle
        self.as_of_day = as_of_day if as_of_day is not None else bundle.max_day()

    # ------------------------------------------------------------------ roles
    def stations(self) -> list[str]:
        return self.bundle.stations

    def supply_meter(self, station: str) -> str:
        return self.bundle.meters(station, "supply")[0]

    def component_meters(self, station: str) -> list[str]:
        return [
            r.meter for r in self.bundle.roles.values()
            if r.station == station and r.role in COMPONENT_ROLES
        ]

    # --------------------------------------------------------------- readings
    def _live(self, station: str, meter: str) -> dict[int, Reading]:
        grouped = {
            key: group for key, group in self.bundle.readings_by_interval().items()
            if key[0] == station and key[1] == meter
            and any(r.observed_day <= self.as_of_day for r in group)
        }
        result: dict[int, Reading] = {}
        for (_, _, minute), group in grouped.items():
            visible = [r for r in group if r.observed_day <= self.as_of_day]
            terminal = resolve_terminal(visible)
            if terminal is not None:
                result[minute] = terminal
        return result

    def station_evidence(self, station: str) -> StationEvidence:
        evidence = StationEvidence(station, self.as_of_day)
        meters = self.bundle.meters(station)
        live = {meter: self._live(station, meter) for meter in meters}
        minutes = sorted({minute for values in live.values() for minute in values})
        for meter in meters:
            role = self.bundle.role(station, meter)
            for minute in minutes:
                reading = live[meter].get(minute)
                if reading is None:
                    evidence.points[(meter, minute)] = MeterPoint(
                        station, meter, role.role, minute, None, "blank", None, None,
                    )
                else:
                    basis = "blank" if reading.kw is None else reading.basis
                    evidence.points[(meter, minute)] = MeterPoint(
                        station, meter, role.role, minute, reading.kw, basis,
                        reading.observed_day, reading.reading,
                    )
        return evidence

    # ------------------------------------------------------------ components
    def component_floor(self, evidence: StationEvidence, minute: int) -> Decimal | None:
        """Best defensible lower bound from observed components.

        ``supply = sum(signed components) + unattributed`` and station-internal
        load is non-negative, so the observed component sum is a floor whenever
        any component is observed.
        """
        total = ZERO
        seen = False
        for meter in self.component_meters(evidence.station):
            point = evidence.point(meter, minute)
            if point is None or point.kw is None:
                continue
            total += point.kw
            seen = True
        return max(total, ZERO) if seen else None

    def unattributed(self, evidence: StationEvidence, minute: int) -> Decimal | None:
        """Supply boundary minus metered components, when all are observed."""
        supply = evidence.point(self.supply_meter(evidence.station), minute)
        if supply is None or supply.kw is None:
            return None
        total = ZERO
        for meter in self.component_meters(evidence.station):
            point = evidence.point(meter, minute)
            if point is None or point.kw is None:
                return None
            total += point.kw
        return supply.kw - total

    def supply_ceiling(self, evidence: StationEvidence) -> Decimal:
        """Most the boundary can plausibly have drawn: nameplate, else history."""
        role = self.bundle.role(evidence.station, self.supply_meter(evidence.station))
        return max(role.capacity_kw, evidence.observed_max_day())

    # -------------------------------------------------------------- windows
    @staticmethod
    def window_minutes(start: int, end: int) -> list[int]:
        return list(range(start, end, 15))

    def demand_statement(
        self,
        evidence: StationEvidence,
        start: int,
        end: int,
        *,
        include_estimated: bool = False,
    ) -> DemandStatement:
        """Demonstrable peak over a half-open window.

        Observed intervals carry the peak.  Unobserved intervals contribute a
        lower bound (component floor) and an upper bound (supply ceiling); the
        reported band spans those, so a missing interval can never silently
        lower demand to zero or be replaced by an invented exact value.
        """
        supply_meter = self.supply_meter(evidence.station)
        ceiling = self.supply_ceiling(evidence)
        best_kw: Decimal | None = None
        best_minute = start
        best_basis = "observed"
        low = ZERO
        high = ZERO
        gaps: list[tuple[int, int]] = []
        for minute in self.window_minutes(start, end):
            point = evidence.point(supply_meter, minute)
            if point is not None and point.kw is not None and (point.basis == "observed" or include_estimated):
                low = max(low, point.kw)
                high = max(high, point.kw)
                if best_kw is None or point.kw > best_kw:
                    best_kw, best_minute, best_basis = point.kw, minute, point.basis
                continue
            gaps.append((minute, minute + 15))
            floor = self.component_floor(evidence, minute)
            low = max(low, floor or ZERO)
            high = max(high, ceiling)
        if best_kw is None:
            best_kw = low
            best_minute = gaps[0][0] if gaps else start
            best_basis = "unobserved-band"
        return DemandStatement(
            station=evidence.station,
            start=start,
            end=end,
            peak_kw=best_kw,
            peak_minute=best_minute,
            basis=best_basis,
            low_kw=low,
            high_kw=high,
            unobserved=tuple(merge_spans(gaps)),
        )

    def metered_kwh(
        self,
        evidence: StationEvidence,
        meter: str,
        start: int,
        end: int,
    ) -> tuple[Decimal, list[tuple[int, int]]]:
        """Energy from observed intervals only; returns (kWh, unobserved spans)."""
        hours = Decimal("0.25")
        total = ZERO
        gaps: list[tuple[int, int]] = []
        for minute in self.window_minutes(start, end):
            point = evidence.point(meter, minute)
            if point is None or point.kw is None:
                gaps.append((minute, minute + 15))
                continue
            total += point.kw * hours
        return total, gaps

    # --------------------------------------------------------------- reserve
    def reserve_evidence(self, as_of_day: int | None = None) -> list[ReserveEvidence]:
        """Latest evidence per site; later rows supersede overlapping earlier ones.

        Rows known after the as-of day are invisible.  A later, narrower row
        (a ``reduced`` revision of part of a span) also replaces that part of
        the older row, so a window never sees old and new evidence at once.
        """
        day = self.as_of_day if as_of_day is None else as_of_day
        rows = sorted(
            (slot for slot in self.bundle.reserve if slot.known_day <= day),
            key=lambda slot: (slot.known_day, slot.from_min, slot.to_min),
        )
        kept: list[ReserveEvidence] = []
        for slot in rows:
            survivor = ReserveEvidence(
                slot.site, slot.from_min, slot.to_min, slot.free_kw, slot.state, slot.known_day,
            )
            fragment: list[ReserveEvidence] = []
            for existing in kept:
                if existing.site != slot.site:
                    fragment.append(existing)
                    continue
                if existing.to_min <= slot.from_min or slot.to_min <= existing.from_min:
                    fragment.append(existing)
                    continue
                if existing.from_min < slot.from_min:
                    fragment.append(ReserveEvidence(
                        existing.site, existing.from_min, slot.from_min,
                        existing.free_kw, existing.state, existing.known_day,
                    ))
                if slot.to_min < existing.to_min:
                    fragment.append(ReserveEvidence(
                        existing.site, slot.to_min, existing.to_min,
                        existing.free_kw, existing.state, existing.known_day,
                    ))
            kept = fragment + [survivor]
        return sorted(kept, key=lambda s: (s.site, s.from_min, s.to_min))

    def reserve_covering(self, site: str, start: int, end: int) -> list[ReserveEvidence]:
        """Latest evidence rows overlapping [start, end) at ``site``."""
        return [
            slot for slot in self.reserve_evidence()
            if slot.site == site and slot.from_min < end and start < slot.to_min
        ]

    def guaranteed_free(
        self, site: str, start: int, end: int, *, requirement_kw: Decimal = ZERO,
    ) -> tuple[Decimal, str, list[tuple[int, int]]]:
        """Free capacity the site can guarantee across a whole window.

        Returns ``(available_kw, state, gaps)``:

        * ``available_kw`` is the *minimum* across the window, using the latest
          evidence for each piece -- including a ``reduced`` row's stated free
          capacity, because that row is the best claim the site can still make.
        * ``state`` is ``verified`` only when every overlapping row is verified;
          ``reduced`` when the latest evidence for some part was reduced;
          ``partial`` when part of the window has no evidence at all.
        * ``gaps`` lists uncovered spans so a caller never reads them as zero.

        A *confirmation* still requires ``verified``; a shortfall is computed
        against ``available_kw`` so the operator sees the real number even when
        the evidence cannot support it.
        """
        rows = self.reserve_covering(site, start, end)
        if not rows:
            return ZERO, "none", [(start, end)]
        spans = [(r.from_min, r.to_min) for r in rows]
        gaps = uncovered(spans, start, end)
        available = min(r.free_kw for r in rows)
        if gaps:
            return available, "partial", gaps
        if any(r.state != "verified" for r in rows):
            return available, "reduced", gaps
        if available < requirement_kw:
            return available, "short", gaps
        return available, "verified", gaps

    def reserve_confirmation(self, site: str, start: int, end: int, requirement_kw: Decimal) -> dict[str, object]:
        """One verdict object shared by quotes, requests and migrations."""
        available, state, gaps = self.guaranteed_free(
            site, start, end, requirement_kw=requirement_kw
        )
        if state == "none":
            return {
                "confirmed": False, "available_kw": "0", "state": state,
                "gaps": [list(g) for g in gaps],
                "reason": f"no inventory evidence at {site} for [{start}, {end})",
            }
        if state == "partial":
            return {
                "confirmed": False, "available_kw": format(available, "f"), "state": state,
                "gaps": [list(g) for g in gaps],
                "reason": f"inventory evidence does not cover all of [{start}, {end}) at {site}",
            }
        if state == "reduced":
            return {
                "confirmed": False, "available_kw": format(available, "f"), "state": state,
                "gaps": [],
                "reason": f"latest evidence for [{start}, {end}) at {site} is reduced to "
                          f"{format(available, 'f')} kW",
            }
        if state == "short":
            return {
                "confirmed": False, "available_kw": format(available, "f"), "state": state,
                "gaps": [],
                "reason": f"verified {format(available, 'f')} kW at {site} is below the "
                          f"{format(requirement_kw, 'f')} kW concurrent commitment",
            }
        return {
            "confirmed": True, "available_kw": format(available, "f"), "state": state,
            "gaps": [], "requirement_kw": format(requirement_kw, "f"),
            "headroom_kw": format(available - requirement_kw, "f"),
        }

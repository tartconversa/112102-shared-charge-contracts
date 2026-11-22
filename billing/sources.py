"""Load and validate the synthetic source tables.

Source CSVs are never modified by the tool.  Corrective readings, manual
transition events, settlements and migrations live in a separate local ledger
(see :mod:`billing.ledger`).  A snapshot produced here is the only input to all
fees, so a later revision can always be replayed byte-for-byte.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .errors import SourceError, ValidationReport
from .money import dec

HEADERS = {
    "meter_readings": ["station", "meter", "timestamp_minute", "kw", "observed_day", "supersedes", "reading"],
    "meter_roles": ["station", "meter", "parent", "role", "capacity_kw"],
    "tariffs": ["tariff", "energy_cent_per_kwh", "demand_cent_per_kw", "reserve_cent_per_kw", "cap_kw", "meter_interval_min"],
    "guarantees": ["right", "client", "home", "reserve_site", "start_minute", "end_minute", "firm_kw", "reserve_kw", "contract"],
    "reserve_inventory": ["site", "from_min", "to_min", "free_kw", "known_day", "state"],
    "transition_events": ["event", "right", "day", "state", "effective_minute", "temporary_site"],
}

ROLES = {"supply", "tenant", "buffer"}
RESERVE_STATES = {"verified", "reduced"}
EVENT_STATES = {"offered", "accepted", "partial_signed", "handoff_received", "withdrawn", "rejected"}
SIGNED_STATES = {"accepted", "partial_signed", "handoff_received"}
READING_BASES = {"observed", "estimated"}


@dataclass(frozen=True, slots=True)
class MeterRole:
    station: str
    meter: str
    parent: str
    role: str
    capacity_kw: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "station": self.station,
            "meter": self.meter,
            "parent": self.parent,
            "role": self.role,
            "capacity_kw": format(self.capacity_kw, "f"),
        }


@dataclass(frozen=True, slots=True)
class Reading:
    station: str
    meter: str
    timestamp_minute: int
    kw: Decimal | None
    observed_day: int
    supersedes: str
    reading: str
    origin: str = "source"
    basis: str = "observed"

    @property
    def blank(self) -> bool:
        return self.kw is None

    def as_dict(self) -> dict[str, str]:
        return {
            "station": self.station,
            "meter": self.meter,
            "timestamp_minute": str(self.timestamp_minute),
            "kw": "" if self.kw is None else format(self.kw, "f"),
            "observed_day": str(self.observed_day),
            "supersedes": self.supersedes,
            "reading": self.reading,
            "origin": self.origin,
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class Tariff:
    tariff: str
    energy_cent_per_kwh: Decimal
    demand_cent_per_kw: Decimal
    reserve_cent_per_kw: Decimal
    cap_kw: Decimal
    meter_interval_min: int

    def as_dict(self) -> dict[str, str]:
        return {
            "tariff": self.tariff,
            "energy_cent_per_kwh": format(self.energy_cent_per_kwh, "f"),
            "demand_cent_per_kw": format(self.demand_cent_per_kw, "f"),
            "reserve_cent_per_kw": format(self.reserve_cent_per_kw, "f"),
            "cap_kw": format(self.cap_kw, "f"),
            "meter_interval_min": str(self.meter_interval_min),
        }


@dataclass(frozen=True, slots=True)
class Guarantee:
    right: str
    client: str
    home: str
    reserve_site: str
    start_minute: int
    end_minute: int
    firm_kw: Decimal
    reserve_kw: Decimal
    contract: str

    @property
    def window(self) -> tuple[int, int]:
        return self.start_minute, self.end_minute

    def as_dict(self) -> dict[str, str]:
        return {
            "right": self.right,
            "client": self.client,
            "home": self.home,
            "reserve_site": self.reserve_site,
            "start_minute": str(self.start_minute),
            "end_minute": str(self.end_minute),
            "firm_kw": format(self.firm_kw, "f"),
            "reserve_kw": format(self.reserve_kw, "f"),
            "contract": self.contract,
        }


@dataclass(frozen=True, slots=True)
class ReserveSlot:
    site: str
    from_min: int
    to_min: int
    free_kw: Decimal
    known_day: int
    state: str

    def as_dict(self) -> dict[str, str]:
        return {
            "site": self.site,
            "from_min": str(self.from_min),
            "to_min": str(self.to_min),
            "free_kw": format(self.free_kw, "f"),
            "known_day": str(self.known_day),
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class TransitionEvent:
    event: str
    right: str
    day: int
    state: str
    effective_minute: int
    temporary_site: str
    origin: str = "source"

    def as_dict(self) -> dict[str, str]:
        return {
            "event": self.event,
            "right": self.right,
            "day": str(self.day),
            "state": self.state,
            "effective_minute": str(self.effective_minute),
            "temporary_site": self.temporary_site,
            "origin": self.origin,
        }


@dataclass(slots=True)
class SourceBundle:
    roles: dict[tuple[str, str], MeterRole]
    readings: list[Reading]
    tariffs: dict[str, Tariff]
    guarantees: dict[str, Guarantee]
    reserve: list[ReserveSlot]
    transitions: list[TransitionEvent]
    report: ValidationReport = field(default_factory=ValidationReport)

    @property
    def stations(self) -> list[str]:
        return sorted({r.station for r in self.roles.values() if r.role == "supply"})

    def station_capacity(self, station: str) -> Decimal:
        for role in self.roles.values():
            if role.station == station and role.role == "supply":
                return role.capacity_kw
        raise SourceError(f"station {station!r} has no supply meter")

    def role(self, station: str, meter: str) -> MeterRole:
        try:
            return self.roles[(station, meter)]
        except KeyError as exc:
            raise SourceError(f"meter {station}/{meter} has no role") from exc

    def meters(self, station: str, role: str | None = None) -> list[str]:
        return sorted(
            r.meter
            for r in self.roles.values()
            if r.station == station and (role is None or r.role == role)
        )

    def readings_by_interval(self) -> dict[tuple[str, str, int], list[Reading]]:
        grouped: dict[tuple[str, str, int], list[Reading]] = {}
        for reading in self.readings:
            grouped.setdefault((reading.station, reading.meter, reading.timestamp_minute), []).append(reading)
        return grouped

    def resolve(self, station: str, meter: str, minute: int) -> Reading | None:
        """Authoritative reading for one interval, or None if never observed."""
        from .evidence import resolve_terminal
        group = self.readings_by_interval().get((station, meter, minute), [])
        return resolve_terminal(group)

    def max_day(self) -> int:
        days = [r.observed_day for r in self.readings]
        days += [s.known_day for s in self.reserve]
        days += [e.day for e in self.transitions]
        return max(days, default=1)

    def data_end_minute(self, station: str | None = None) -> int:
        return max(
            (r.timestamp_minute + 15 for r in self.readings if station is None or r.station == station),
            default=0,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "meter_roles": [r.as_dict() for r in sorted(self.roles.values(), key=lambda x: (x.station, x.meter))],
            "meter_readings": [r.as_dict() for r in self.readings],
            "tariffs": [t.as_dict() for t in sorted(self.tariffs.values(), key=lambda x: x.tariff)],
            "guarantees": [g.as_dict() for g in sorted(self.guarantees.values(), key=lambda x: x.right)],
            "reserve_inventory": [s.as_dict() for s in self.reserve],
            "transition_events": [e.as_dict() for e in self.transitions],
        }


def _int(value: Any, field_name: str, source: str, row: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SourceError(f"{source}:{row}: {field_name} must be an integer, got {value!r}") from exc


def _number(value: Any, field_name: str, source: str, row: str, optional: bool = False) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if optional:
            return None
        raise SourceError(f"{source}:{row}: {field_name} is required")
    try:
        return dec(str(value).strip())
    except InvalidOperation as exc:
        raise SourceError(f"{source}:{row}: {field_name} must be a number, got {value!r}") from exc


def _check_headers(path: Path, name: str) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != HEADERS[name]:
            raise SourceError(f"{name}: unexpected header {reader.fieldnames!r}; expected {HEADERS[name]!r}")
        return list(reader)


def load_sources(
    root: str | Path,
    extra_readings: Iterable[dict[str, Any]] = (),
    extra_transitions: Iterable[dict[str, Any]] = (),
) -> SourceBundle:
    root = Path(root)
    raw: dict[str, list[dict[str, Any]]] = {}
    for name in HEADERS:
        path = root / f"{name}.csv"
        if not path.exists():
            raise SourceError(f"missing source table: {path}")
        raw[name] = _check_headers(path, name)

    for row in raw["meter_readings"]:
        row.setdefault("origin", "source")
        row.setdefault("basis", "observed")
    for row in raw["transition_events"]:
        row.setdefault("origin", "source")

    extra_readings = [dict(row, origin="ledger") for row in extra_readings]
    extra_transitions = [dict(row, origin="ledger") for row in extra_transitions]
    raw["meter_readings"].extend(extra_readings)
    raw["transition_events"].extend(extra_transitions)
    return build_bundle(raw)


def bundle_from_snapshot(snapshot: dict[str, Any]) -> SourceBundle:
    """Rebuild a bundle from a ledger snapshot without consulting live files."""
    raw = {name: [dict(row) for row in snapshot.get(name, [])] for name in HEADERS}
    return build_bundle(raw)


def build_bundle(raw: dict[str, list[dict[str, Any]]]) -> SourceBundle:
    report = ValidationReport()

    roles: dict[tuple[str, str], MeterRole] = {}
    role_rows: list[MeterRole] = []
    for index, row in enumerate(raw["meter_roles"], start=2):
        tag = f"meter_roles:{index}"
        station, meter = str(row.get("station", "")).strip(), str(row.get("meter", "")).strip()
        if not station or not meter:
            report.add("role_key", "error", "station and meter are required", "meter_roles", tag)
            continue
        if (station, meter) in roles:
            report.add("role_duplicate", "error", f"duplicate meter role {station}/{meter}", "meter_roles", tag)
            continue
        role_name = str(row.get("role", "")).strip()
        if role_name not in ROLES:
            report.add("role_kind", "error", f"unknown role {role_name!r}", "meter_roles", tag)
        capacity = _number(row.get("capacity_kw"), "capacity_kw", "meter_roles", tag) or Decimal(0)
        if capacity < 0:
            report.add("role_capacity", "error", f"negative capacity for {station}/{meter}", "meter_roles", tag)
        role = MeterRole(station, meter, str(row.get("parent", "")).strip(), role_name, capacity)
        roles[(station, meter)] = role
        role_rows.append(role)

    stations = {r.station for r in role_rows if r.role == "supply"}
    for station in sorted({r.station for r in role_rows}):
        supply = [r for r in role_rows if r.station == station and r.role == "supply"]
        if len(supply) != 1:
            report.add("supply_meter", "error", f"station {station} must have exactly one supply meter, found {len(supply)}", "meter_roles")
        for role in role_rows:
            if role.station != station:
                continue
            if role.role == "supply":
                if role.parent:
                    report.add("supply_parent", "error", f"supply meter {role.meter} must not have a parent", "meter_roles")
                continue
            parent = roles.get((station, role.parent))
            if parent is None:
                report.add("role_parent", "error", f"parent {role.parent!r} of {station}/{role.meter} is missing", "meter_roles")
            elif parent.station != station:
                report.add("role_parent", "error", f"parent {role.parent!r} is in another station", "meter_roles")
            elif parent.role != "supply":
                report.add("role_parent", "error", f"{role.role} meter {role.meter} must hang off the supply meter", "meter_roles")

    readings: list[Reading] = []
    labels: dict[str, Reading] = {}
    for index, row in enumerate(raw["meter_readings"], start=2):
        tag = f"meter_readings:{index}"
        station, meter = str(row.get("station", "")).strip(), str(row.get("meter", "")).strip()
        label = str(row.get("reading", "")).strip()
        if not label:
            report.add("reading_key", "error", "reading label is required", "meter_readings", tag)
            continue
        if label in labels:
            report.add("reading_duplicate", "error", f"duplicate reading label {label}", "meter_readings", tag)
            continue
        if station not in stations:
            report.add("reading_station", "error", f"unknown station {station!r}", "meter_readings", tag)
            continue
        role = roles.get((station, meter))
        if role is None:
            report.add("reading_meter", "error", f"meter {station}/{meter} has no role", "meter_readings", tag)
            continue
        minute = _int(row.get("timestamp_minute"), "timestamp_minute", "meter_readings", tag)
        day = _int(row.get("observed_day"), "observed_day", "meter_readings", tag)
        kw = _number(row.get("kw"), "kw", "meter_readings", tag, optional=True)
        origin = str(row.get("origin", "source")).strip() or "source"
        basis = str(row.get("basis", "observed")).strip() or "observed"
        if basis not in READING_BASES:
            report.add("reading_basis", "error", f"unknown evidence basis {basis!r}", "meter_readings", tag)
        if minute % 15:
            report.add("reading_alignment", "error", f"minute {minute} is not on a 15-minute boundary", "meter_readings", tag)
        if not 0 <= minute < 1440:
            report.add("reading_range", "error", f"minute {minute} outside a settlement day", "meter_readings", tag)
        if day < 1:
            report.add("reading_day", "error", f"observed_day {day} must be positive", "meter_readings", tag)
        if kw is not None and kw < 0 and role.role != "buffer":
            report.add("reading_negative", "error", f"negative kW on non-buffer meter {station}/{meter}", "meter_readings", tag)
        reading = Reading(station, meter, minute, kw, day, str(row.get("supersedes", "")).strip(), label, origin, basis)
        readings.append(reading)
        labels[label] = reading

    grouped: dict[tuple[str, str, int], list[Reading]] = {}
    for reading in readings:
        grouped.setdefault((reading.station, reading.meter, reading.timestamp_minute), []).append(reading)

    for reading in readings:
        if not reading.supersedes:
            continue
        target = labels.get(reading.supersedes)
        if target is None:
            report.add("supersedes_missing", "error", f"reading {reading.reading} supersedes unknown {reading.supersedes}", "meter_readings", reading.reading)
            continue
        if (target.station, target.meter, target.timestamp_minute) != (reading.station, reading.meter, reading.timestamp_minute):
            report.add("supersedes_target", "error", f"reading {reading.reading} supersedes a different interval", "meter_readings", reading.reading)
        if reading.observed_day < target.observed_day:
            # Ordering within one supersession chain is checked after the chain
            # is collapsed (see resolve_terminal); comparing every edge here
            # would reject a correction inserted between two re-reads.
            report.add(
                "supersedes_backdated", "warning",
                f"reading {reading.reading} supersedes {target.reading} but arrives on an earlier day",
                "meter_readings", reading.reading,
            )

    for key, group in grouped.items():
        superseded = {r.supersedes for r in group if r.supersedes}
        terminals = [r for r in group if r.reading not in superseded]
        if len(terminals) > 1:
            # Several readings can each supersede the same target when a
            # correction is inserted between two source re-reads.  That is not
            # corruption: the later arrival wins (see evidence.resolve_terminal),
            # but the operator should still see the contention.
            report.add(
                "reading_chain", "warning",
                f"interval {key} has competing live readings {sorted(r.reading for r in terminals)}; "
                "the latest observed day wins",
                "meter_readings",
            )

    # Duplicate reserve evidence for the same span and day is ambiguous; a
    # later day for the same span is legitimate new evidence (e.g. reduced).
    seen_reserve: set[tuple[str, int, int, int]] = set()
    reserve: list[ReserveSlot] = []
    for index, row in enumerate(raw["reserve_inventory"], start=2):
        tag = f"reserve_inventory:{index}"
        site = str(row.get("site", "")).strip()
        if site not in stations:
            report.add("reserve_site", "error", f"unknown site {site!r}", "reserve_inventory", tag)
        start = _int(row.get("from_min"), "from_min", "reserve_inventory", tag)
        end = _int(row.get("to_min"), "to_min", "reserve_inventory", tag)
        known = _int(row.get("known_day"), "known_day", "reserve_inventory", tag)
        free = _number(row.get("free_kw"), "free_kw", "reserve_inventory", tag) or Decimal(0)
        state = str(row.get("state", "")).strip()
        if not 0 <= start < end <= 1440:
            report.add("reserve_window", "error", f"invalid reserve interval [{start}, {end})", "reserve_inventory", tag)
        if known < 1:
            report.add("reserve_day", "error", f"known_day {known} must be positive", "reserve_inventory", tag)
        if free < 0:
            report.add("reserve_capacity", "error", "free_kw must be non-negative", "reserve_inventory", tag)
        if state not in RESERVE_STATES:
            report.add("reserve_state", "error", f"unknown reserve state {state!r}", "reserve_inventory", tag)
        key = (site, start, end, known)
        if key in seen_reserve:
            report.add("reserve_duplicate", "error", f"duplicate reserve evidence {key}", "reserve_inventory", tag)
        seen_reserve.add(key)
        reserve.append(ReserveSlot(site, start, end, free, known, state))

    tariffs: dict[str, Tariff] = {}
    for index, row in enumerate(raw["tariffs"], start=2):
        tag = f"tariffs:{index}"
        name = str(row.get("tariff", "")).strip()
        if not name:
            report.add("tariff_key", "error", "tariff key is required", "tariffs", tag)
            continue
        energy = _number(row.get("energy_cent_per_kwh"), "energy_cent_per_kwh", "tariffs", tag) or Decimal(0)
        demand = _number(row.get("demand_cent_per_kw"), "demand_cent_per_kw", "tariffs", tag) or Decimal(0)
        res_rate = _number(row.get("reserve_cent_per_kw"), "reserve_cent_per_kw", "tariffs", tag) or Decimal(0)
        cap = _number(row.get("cap_kw"), "cap_kw", "tariffs", tag) or Decimal(0)
        interval = _int(row.get("meter_interval_min"), "meter_interval_min", "tariffs", tag)
        if interval != 15:
            report.add("tariff_interval", "warning", f"tariff {name} uses {interval}-minute intervals; readings are aligned to 15", "tariffs", tag)
        for field_name, value in (("energy", energy), ("demand", demand), ("reserve", res_rate), ("cap", cap)):
            if value < 0:
                report.add("tariff_negative", "error", f"tariff {name} has negative {field_name}", "tariffs", tag)
        if name in tariffs:
            report.add("tariff_duplicate", "error", f"duplicate tariff {name}", "tariffs", tag)
            continue
        tariffs[name] = Tariff(name, energy, demand, res_rate, cap, interval)

    guarantees: dict[str, Guarantee] = {}
    for index, row in enumerate(raw["guarantees"], start=2):
        tag = f"guarantees:{index}"
        right = str(row.get("right", "")).strip()
        if not right:
            report.add("guarantee_key", "error", "right key is required", "guarantees", tag)
            continue
        if right in guarantees:
            report.add("guarantee_duplicate", "error", f"duplicate right {right}", "guarantees", tag)
            continue
        home = str(row.get("home", "")).strip()
        reserve_site = str(row.get("reserve_site", "")).strip()
        if home not in stations:
            report.add("guarantee_home", "error", f"unknown home station {home!r}", "guarantees", tag)
        if reserve_site not in stations:
            report.add("guarantee_reserve_site", "error", f"unknown reserve station {reserve_site!r}", "guarantees", tag)
        start = _int(row.get("start_minute"), "start_minute", "guarantees", tag)
        end = _int(row.get("end_minute"), "end_minute", "guarantees", tag)
        if not 0 <= start < end <= 1440:
            report.add("guarantee_window", "error", f"invalid window [{start}, {end})", "guarantees", tag)
        firm = _number(row.get("firm_kw"), "firm_kw", "guarantees", tag) or Decimal(0)
        reserve_kw = _number(row.get("reserve_kw"), "reserve_kw", "guarantees", tag) or Decimal(0)
        contract = str(row.get("contract", "")).strip()
        if contract not in tariffs:
            report.add("guarantee_contract", "error", f"unknown contract {contract!r}", "guarantees", tag)
        if firm < 0 or reserve_kw < 0:
            report.add("guarantee_capacity", "error", "firm and reserve kW must be non-negative", "guarantees", tag)
        guarantees[right] = Guarantee(right, str(row.get("client", "")).strip(), home, reserve_site, start, end, firm, reserve_kw, contract)

    transitions: list[TransitionEvent] = []
    event_labels: set[str] = set()
    for index, row in enumerate(raw["transition_events"], start=2):
        tag = f"transition_events:{index}"
        label = str(row.get("event", "")).strip()
        right = str(row.get("right", "")).strip()
        if not label:
            report.add("event_key", "error", "event label is required", "transition_events", tag)
            continue
        if label in event_labels:
            report.add("event_duplicate", "error", f"duplicate event label {label}", "transition_events", tag)
            continue
        event_labels.add(label)
        if right not in guarantees:
            report.add("event_right", "error", f"unknown right {right!r}", "transition_events", tag)
            continue
        day = _int(row.get("day"), "day", "transition_events", tag)
        minute = _int(row.get("effective_minute"), "effective_minute", "transition_events", tag)
        state = str(row.get("state", "")).strip()
        site = str(row.get("temporary_site", "")).strip()
        if state not in EVENT_STATES:
            report.add("event_state", "error", f"unknown transition state {state!r}", "transition_events", tag)
        if day < 1:
            report.add("event_day", "error", f"day {day} must be positive", "transition_events", tag)
        guarantee = guarantees[right]
        if not guarantee.start_minute <= minute <= guarantee.end_minute:
            report.add("event_window", "error", f"event {label} outside right window [{guarantee.start_minute}, {guarantee.end_minute}]", "transition_events", tag)
        if site and site not in stations:
            report.add("event_site", "error", f"unknown temporary site {site!r}", "transition_events", tag)
        if state in {"offered", "accepted", "partial_signed", "handoff_received"} and not site:
            report.add("event_site_missing", "error", f"event {label} requires a temporary_site", "transition_events", tag)
        transitions.append(TransitionEvent(label, right, day, state, minute, site, str(row.get("origin", "source"))))

    bundle = SourceBundle(roles, readings, tariffs, guarantees, reserve, transitions, report)
    _validate_semantics(bundle)
    return bundle


def _validate_semantics(data: SourceBundle) -> None:
    report = data.report
    by_right: dict[str, list[TransitionEvent]] = {}
    for event in data.transitions:
        by_right.setdefault(event.right, []).append(event)

    for right, events in by_right.items():
        ordered = sorted(events, key=lambda e: (e.day, e.effective_minute, e.event))
        signed_minute: int | None = None
        for event in ordered:
            if event.state in SIGNED_STATES:
                if signed_minute is not None and event.effective_minute < signed_minute:
                    report.add(
                        "event_order", "error",
                        f"right {right}: {event.event} effective {event.effective_minute} predates an earlier signature",
                        "transition_events", event.event,
                    )
                signed_minute = event.effective_minute
            if event.state == "handoff_received" and not any(
                e.state in {"accepted", "partial_signed"} and e.effective_minute <= event.effective_minute
                for e in ordered if e is not event
            ):
                report.add(
                    "handoff_without_signature", "error",
                    f"right {right}: handoff {event.event} has no preceding accepted/partial signature",
                    "transition_events", event.event,
                )

    for right, events in by_right.items():
        open_sites = {
            e.temporary_site for e in events
            if e.state == "offered"
        }
        resolved = any(e.state in {"accepted", "partial_signed", "handoff_received", "rejected", "withdrawn"} for e in events)
        if len(open_sites) > 1 and not resolved:
            report.add(
                "incompatible_offers", "error",
                f"right {right}: offers at multiple sites without a resolution: {sorted(open_sites)}",
                "transition_events",
            )

    # Physical plausibility: a resolved meter value above nameplate capacity is
    # suspicious but not impossible (overload), so it is a warning.
    from .evidence import resolve_terminal

    grouped = data.readings_by_interval()
    for (station, meter, minute), group in grouped.items():
        reading = resolve_terminal(group)
        if reading is None:
            continue
        role = data.roles[(station, meter)]
        if reading.kw is not None and reading.kw > role.capacity_kw:
            report.add(
                "reading_over_capacity", "warning",
                f"{station}/{meter} at {minute} reads {reading.kw} kW above meter capacity {role.capacity_kw}",
                "meter_readings", reading.reading,
            )

    # Children cannot exceed their parent boundary in the same interval.
    for station in sorted({role.station for role in data.roles.values()}):
        supply = data.meters(station, "supply")
        if len(supply) != 1:
            continue
        supply_meter = supply[0]
        minutes = {minute for (s, _, minute) in grouped if s == station}
        children = data.meters(station, "tenant") + data.meters(station, "buffer")
        for minute in sorted(minutes):
            boundary = resolve_terminal(grouped.get((station, supply_meter, minute), []))
            if boundary is None or boundary.kw is None:
                continue
            total = Decimal(0)
            for child in children:
                point = resolve_terminal(grouped.get((station, child, minute), []))
                if point is not None and point.kw is not None:
                    total += point.kw
            if total > boundary.kw:
                report.add(
                    "children_exceed_boundary", "warning",
                    f"{station}@{minute}: metered components {total} kW exceed the "
                    f"supply boundary {boundary.kw} kW; a meter may be double-counted",
                    "meter_readings",
                )

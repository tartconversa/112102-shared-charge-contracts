"""Self-contained reports rendered as text or CSV rows."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _money(cent: str | int | Decimal) -> str:
    value = Decimal(str(cent)) / 100
    return f"{value:,.2f}"


def comparison_text(result: dict[str, Any]) -> str:
    old, new = result["old"], result["new"]
    lines = [
        f"station {result['station']}  window [{result['window'][0]}, {result['window'][1]})",
        f"  evidence: revision {result['evidence']['revision']} as of day {result['evidence']['as_of_day']}",
        "",
        f"  {'':28}{'old':>14}{'new':>14}{'delta':>14}",
    ]
    kinds = [("energy", "energy"), ("demand", "demand"), ("reserve", "reserve")]
    for key, label in kinds:
        lines.append(
            f"  {label:28}{_money(old['subtotals_cent'][key]):>14}"
            f"{_money(new['subtotals_cent'][key]):>14}"
            f"{_money(result['delta_by_kind_cent'][key]):>14}"
        )
    lines.append(
        f"  {'total (billed)':28}{_money(old['billed_total_cent']):>14}"
        f"{_money(new['billed_total_cent']):>14}{_money(result['delta_cent']):>14}"
    )
    if Decimal(new["exposure_cent"]) or Decimal(old["exposure_cent"]):
        lines.append(
            f"  {'exposure (not billed)':28}{_money(old['exposure_cent']):>14}"
            f"{_money(new['exposure_cent']):>14}"
        )
    lines.append("")
    lines.append(f"  cheaper on this evidence: {result['cheaper']}")
    demand = new.get("demand")
    if demand:
        band = demand["band_kw"]
        lines.append(
            f"  demand: peak {demand['peak_kw']} kW at minute {demand['peak_minute']} "
            f"({demand['basis']}); band {band[0]}..{band[1]} kW"
        )
        if not demand["complete"]:
            lines.append(f"  unobserved demand intervals: {demand['unobserved']}")
    unattributed = result.get("unattributed")
    if unattributed and unattributed["intervals"]:
        lines.append(
            f"  unattributed load: {unattributed['energy_kwh']} kWh over "
            f"{unattributed['intervals']} intervals (max {unattributed['max_kw']} kW); "
            "not allocated to any tenant"
        )
    reserve = new.get("reserve")
    if reserve:
        lines.append(
            f"  reserve: {reserve['requirement_kw']} kW concurrent at {reserve['site']}, "
            f"free {reserve['free_kw']} kW ({reserve['inventory_state']}), short {reserve['short_kw']} kW"
        )
    consequences = result.get("cap_consequences") or {}
    if consequences.get("triggered"):
        lines.append(
            f"  cap {consequences['cap_kw']} kW breached by {consequences['breach_kw']} kW: "
            f"renegotiate {consequences['renegotiate_not_effective']}, "
            f"perform {consequences['must_perform']}, "
            f"temporary alternative {consequences['temporary_alternative']}"
        )
    return "\n".join(lines)


def relocation_text(result: dict[str, Any]) -> str:
    reserve = result["destination_reserve_after_move"]
    origin = result["origin_reserve_after_move"]
    band = result["origin_peak_after_move_band_kw"]
    lines = [
        f"right {result['right']} ({result['client']}): {result['from']} -> {result['to']} "
        f"[{result['window'][0]}, {result['window'][1]})",
        f"  contract window covers the move: {result['effective_window_covers_move']}",
        f"  origin peak after move: {band[0]}..{band[1]} kW (band, not a point)",
        f"    {result['origin_peak_note']}",
        f"  origin reserve still promised: {origin['requirement_kw']} kW "
        f"(free {origin['free_kw']}, short {origin['short_kw']})",
        f"  destination concurrent reserve: {reserve['requirement_kw']} kW "
        f"(free {reserve['free_kw']}, short {reserve['short_kw']})",
        f"  still borne: {_money(result['still_borne_cent']['total'])} "
        f"(origin {_money(result['still_borne_cent']['origin_reserve_capacity'])}, "
        f"destination {_money(result['still_borne_cent']['destination_reserve_capacity'])})",
    ]
    return "\n".join(lines)


def migration_text(states: list[dict[str, Any]], as_of_day: int | None = None) -> str:
    lines = [] if as_of_day is None else [f"as of day {as_of_day}", ""]
    for state in states:
        lines.append(
            f"{state['right']} {state['client']}: {state['phase']} "
            f"({state['original_contract']} -> {state['current_contract']}) "
            f"moved {state['moved_kw']} kW / remaining {state['remaining_kw']} kW"
        )
        if state["original_carrier_still_in_force"]:
            lines.append("  original carrier still in force")
        reserve = state["reserve"]
        lines.append(
            f"  reserve confirmed: {reserve.get('confirmed')}"
            + (f" — {reserve.get('reason')}" if reserve.get("reason") else "")
        )
        for note in state["notes"]:
            lines.append(f"  - {note}")
    return "\n".join(lines)

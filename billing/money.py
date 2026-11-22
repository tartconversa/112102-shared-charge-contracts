"""Money and interval arithmetic.

Every monetary value is kept as an exact Decimal in cents.  Percentages are
always rounded once, half-up, and booked as an explicit adjustment so that a
reported total is never a product of silently re-rounded components.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


CENT = Decimal("0.01")
ZERO = Decimal("0")


def dec(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def pct(amount: Decimal, percent: Decimal) -> Decimal:
    """Half-up percentage of an exact cent amount."""
    return q2(amount * percent / Decimal(100))


def overlaps(a_from: int, a_to: int, b_from: int, b_to: int) -> bool:
    """Half-open interval intersection."""
    return a_from < b_to and b_from < a_to


def intersect(a_from: int, a_to: int, b_from: int, b_to: int) -> tuple[int, int] | None:
    start, end = max(a_from, b_from), min(a_to, b_to)
    return (start, end) if start < end else None


def minutes(a_from: int, a_to: int) -> int:
    return max(0, a_to - a_from)


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping half-open spans, preserving gap boundaries."""
    ordered = sorted((s, e) for s, e in spans if s < e)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def uncovered(spans: Iterable[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    """Parts of [start, end) not covered by any span."""
    gaps: list[tuple[int, int]] = []
    cursor = start
    for s, e in merge_spans(spans):
        s, e = max(s, start), min(e, end)
        if e <= s:
            continue
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps

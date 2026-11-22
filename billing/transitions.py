"""Contract migration state for customer operations.

A migration is staged, and the paperwork decides which clause bills:

===================  =======================================================
state                billing and rights
===================  =======================================================
``offered``          Proposal only.  The original clause remains in force.
``partial_signed``   Customer signed part of the move; the original carrier is
                     still in force because a handoff has not arrived.  Both
                     clauses are visible.
``handoff_received`` Original carrier handed over the agreed capacity.  The
                     handed-over part bills new terms; any unsigned remainder
                     still bills old terms.
``accepted``         Whole package signed.  New terms are the only terms.
``withdrawn`` /      No migration; the original clause stands.
``rejected``
===================  =======================================================

* Capacity moved is never inferred from aggregate meters.  It comes from the
  signed paperwork (``signed_kw``) and the right's own contracted capacity.
* A signature can arrive in instalments; instalments are cumulative and can
  never exceed the right's firm capacity.
* Reserve confirmation is separate from signature and needs the right's own
  window in force, inventory that covers the whole window, and enough capacity
  for every concurrent commitment at that site.
* Two live requests that cannot both be satisfied never both return success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .errors import TransitionError
from .evidence import EvidenceBook
from .money import ZERO, q2
from .sources import Guarantee, TransitionEvent

FINAL = {"rejected", "withdrawn"}
SIGNED = {"accepted", "partial_signed", "handoff_received"}


@dataclass(slots=True)
class MigrationState:
    right: str
    client: str
    phase: str
    original_contract: str
    current_contract: str
    effective_minute: int | None
    temporary_site: str | None
    original_carrier_still_in_force: bool
    moved_kw: Decimal
    remaining_kw: Decimal
    reserve: dict[str, object] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def dual_terms_in_force(self) -> bool:
        """Both clauses bill at once only when part moved and part stayed."""
        return (
            self.original_carrier_still_in_force
            and self.moved_kw > 0
            and self.remaining_kw > 0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "right": self.right,
            "client": self.client,
            "phase": self.phase,
            "original_contract": self.original_contract,
            "current_contract": self.current_contract,
            "effective_minute": self.effective_minute,
            "temporary_site": self.temporary_site,
            "original_carrier_still_in_force": self.original_carrier_still_in_force,
            "dual_terms_in_force": self.dual_terms_in_force,
            "moved_kw": format(self.moved_kw, "f"),
            "remaining_kw": format(self.remaining_kw, "f"),
            "reserve": self.reserve,
            "events": self.events,
            "notes": self.notes,
        }


def is_effective(state: "MigrationState") -> bool:
    """Has any signed part of this migration taken effect?"""
    if state.phase in {"handoff_received", "accepted"}:
        return True
    return state.phase == "partial_signed" and state.moved_kw > 0


def _signed_kw(record: dict, guarantee: Guarantee) -> Decimal:
    raw = record.get("signed_kw")
    if raw in (None, ""):
        return ZERO
    value = Decimal(str(raw))
    if value < 0 or value > guarantee.firm_kw:
        raise TransitionError(
            f"migration {record['id']}: signed_kw {value} is outside [0, {guarantee.firm_kw}]"
        )
    return value


def evaluate_migration(
    book: EvidenceBook,
    guarantee: Guarantee,
    events: list[TransitionEvent],
    *,
    manual: list[dict] | None = None,
) -> MigrationState:
    """Fold one right's paperwork into a state; signatures are cumulative.

    Paperwork dated after ``book.as_of_day`` is invisible, so the same right can
    be inspected as it stood on an earlier day.
    """
    as_of = book.as_of_day
    ordered = sorted(
        (e for e in events if e.day <= as_of),
        key=lambda e: (e.day, e.effective_minute, e.event),
    )
    state = MigrationState(
        right=guarantee.right,
        client=guarantee.client,
        phase="none",
        original_contract=guarantee.contract,
        current_contract=guarantee.contract,
        effective_minute=None,
        temporary_site=None,
        original_carrier_still_in_force=False,
        moved_kw=ZERO,
        remaining_kw=guarantee.firm_kw,
    )
    state.events = [e.event for e in ordered]

    for event in ordered:
        if event.state in FINAL:
            state.phase = event.state
            state.notes.append(
                f"{event.event}: migration {event.state} on day {event.day}; original clause stands"
            )
            state.original_carrier_still_in_force = False
            state.moved_kw = ZERO
            state.remaining_kw = guarantee.firm_kw
            state.current_contract = guarantee.contract
            continue
        if state.phase in FINAL:
            state.notes.append(f"{event.event}: ignored; the migration was already {state.phase}")
            continue
        if event.state == "offered":
            if state.phase == "none":
                state.phase = "offered"
                state.effective_minute = event.effective_minute
                state.temporary_site = event.temporary_site
                state.notes.append(
                    f"{event.event}: offer at {event.temporary_site}; old clause remains in force"
                )
            continue
        state.temporary_site = event.temporary_site or state.temporary_site
        state.effective_minute = event.effective_minute
        if event.state == "partial_signed":
            state.phase = "partial_signed"
            state.original_carrier_still_in_force = True
            state.notes.append(
                f"{event.event}: partial signature; handoff outstanding, old carrier keeps the rest"
            )
        elif event.state == "handoff_received":
            state.phase = "handoff_received"
            state.original_carrier_still_in_force = False
            state.notes.append(f"{event.event}: handoff received; moved part bills new terms")
        elif event.state == "accepted":
            state.phase = "accepted"
            state.original_carrier_still_in_force = False
            state.notes.append(f"{event.event}: accepted; new terms are the only terms")

    # Manual instalments: total signed capacity cannot exceed the right.
    total_signed = ZERO
    accepted = False
    for record in manual or []:
        if record["right"] != guarantee.right:
            continue
        if record.get("requested_day", 0) > as_of:
            continue
        state.events.append(record["id"])
        if record.get("withdrawn_day") is not None and record["withdrawn_day"] <= as_of:
            state.notes.append(
                f"{record['id']}: withdrawn on day {record['withdrawn_day']}; original clause stands"
            )
            continue
        signed = _signed_kw(record, guarantee)
        total_signed += signed
        state.temporary_site = record.get("deliver_to") or state.temporary_site
        state.effective_minute = record.get("effective_minute", state.effective_minute)
        if record.get("accepted_day") is not None and record["accepted_day"] <= as_of:
            accepted = True
            state.notes.append(f"{record['id']}: accepted on day {record['accepted_day']}")
        elif signed == 0:
            state.notes.append(
                f"{record['id']}: partial signature with no signed_kw; capacity stays unquantified"
            )
        else:
            state.notes.append(
                f"{record['id']}: partial signature ({format(signed, 'f')} kW) pending acceptance"
            )

    if total_signed > guarantee.firm_kw:
        raise TransitionError(
            f"right {guarantee.right}: signed instalments total {total_signed} kW, "
            f"above firm capacity {guarantee.firm_kw} kW"
        )

    if accepted:
        state.phase = "accepted"
        state.original_carrier_still_in_force = False
        state.current_contract = "new"
        state.moved_kw = guarantee.firm_kw
        state.remaining_kw = ZERO
    elif total_signed > 0:
        # Instalments are signed; the handed-over part bills new terms, the rest
        # keeps billing old terms until it is also signed and handed over.
        state.phase = (
            "handoff_received" if state.phase == "handoff_received" else "partial_signed"
        )
        state.moved_kw = total_signed
        state.remaining_kw = guarantee.firm_kw - total_signed
        state.original_carrier_still_in_force = state.remaining_kw > 0
        state.current_contract = "old+new" if state.remaining_kw > 0 else "new"
    elif state.phase in {"handoff_received", "accepted"}:
        state.moved_kw = guarantee.firm_kw
        state.remaining_kw = ZERO
        state.original_carrier_still_in_force = False
        state.current_contract = "new"
    elif state.phase == "partial_signed":
        # Source events do not carry kW; show the split as fully pending rather
        # than inventing a number from meters.
        state.original_carrier_still_in_force = True
        state.current_contract = "old+new"
        state.remaining_kw = guarantee.firm_kw
        state.notes.append("partial signature has no recorded kW; moved capacity stays unquantified")
    if state.phase == "none":
        state.notes.append("no transition paperwork; original clause applies")

    state.reserve = reserve_verdict(book, guarantee, state)
    return state


def reserve_verdict(book: EvidenceBook, guarantee: Guarantee, state: MigrationState) -> dict[str, object]:
    """Confirm reserve only against the right's live window and verified stock.

    The site under test is the destination the right is moving *to*.  The
    right's original reserve site is already part of the existing commitment,
    so nothing is "confirmed" until a destination exists.
    """
    from .pricing import reserve_profile

    if state.phase in FINAL:
        return {"confirmed": False, "reason": f"migration is {state.phase}", "state": "n/a"}
    if state.phase == "none":
        return {"confirmed": False, "reason": "no migration paperwork; nothing to confirm", "state": "n/a"}
    site = state.temporary_site
    if not site:
        return {"confirmed": False, "reason": "no temporary site recorded", "state": "n/a"}
    if state.effective_minute is not None and not (
        guarantee.start_minute <= state.effective_minute <= guarantee.end_minute
    ):
        return {"confirmed": False, "reason": "effective minute is outside the right's window", "state": "n/a"}
    profile = reserve_profile(
        book, site, guarantee.start_minute, guarantee.end_minute,
        replace={guarantee.right: site},
    )
    verdict = book.reserve_confirmation(
        site, guarantee.start_minute, guarantee.end_minute, profile.requirement_kw,
    )
    verdict.update({
        "site": site,
        "reserve_kw": format(guarantee.reserve_kw, "f"),
        "concurrent_requirement_kw": format(profile.requirement_kw, "f"),
    })
    return verdict


def check_new_request(
    book: EvidenceBook,
    guarantee: Guarantee,
    site: str,
    window: tuple[int, int],
    *,
    existing: list[MigrationState],
) -> dict[str, object]:
    """A request either confirms cleanly or explains what blocks it.

    Never returns success for a second, incompatible live request, and never
    treats partial or reduced inventory as if it were verified.
    """
    from .pricing import reserve_profile

    start, end = window
    if not (guarantee.start_minute <= start and end <= guarantee.end_minute):
        return {
            "ok": False,
            "code": "outside-window",
            "reason": f"[{start}, {end}) is outside right {guarantee.right} window {guarantee.window}",
        }
    same_right = [m for m in existing if m.right == guarantee.right and m.phase not in FINAL]
    different_site = [m for m in same_right if m.temporary_site and m.temporary_site != site]
    if different_site:
        return {
            "ok": False,
            "code": "incompatible-request",
            "reason": (
                f"right {guarantee.right} already has a live request at "
                f"{different_site[0].temporary_site}; withdraw it before requesting {site}"
            ),
        }
    profile = reserve_profile(book, site, start, end, replace={guarantee.right: site})
    verdict = book.reserve_confirmation(site, start, end, profile.requirement_kw)
    return {
        "ok": verdict["confirmed"],
        "code": "confirmed" if verdict["confirmed"] else verdict["state"],
        "reason": verdict.get("reason", "verified inventory covers the concurrent commitment"),
        "available_kw": verdict.get("available_kw"),
        "requirement_kw": format(profile.requirement_kw, "f"),
        "headroom_kw": verdict.get("headroom_kw"),
        "gaps": verdict.get("gaps", []),
    }


def overlap_cost(
    book: EvidenceBook,
    guarantee: Guarantee,
    state: MigrationState,
    *,
    signed_day: int | None = None,
    handoff_day: int | None = None,
) -> dict[str, object]:
    """Extra cost of a staged migration, quoted per day of each condition.

    All figures are planning charges, not metered quantities:

    * ``dual_terms`` -- both clauses bill on the moved capacity while the
      original carrier has not yet handed off.
    * ``reserve_hold`` -- temporary reserve kept at the destination while the
      origin still holds its own promise.
    * ``other_terms_until_handoff`` -- new-minus-old rate on the moved capacity
      for the days before handoff.

    A missing ``handoff_day`` means "assume one day of overlap", and the reply
    says so rather than presenting a guess as fact.
    """
    new_rate = book.bundle.tariffs["new"].reserve_cent_per_kw
    old_rate = book.bundle.tariffs["old"].reserve_cent_per_kw
    days = 1
    assumed = True
    if signed_day is not None and handoff_day is not None and handoff_day >= signed_day:
        days = handoff_day - signed_day
        assumed = False
    active = state.dual_terms_in_force
    moved = state.moved_kw
    return {
        "right": guarantee.right,
        "signed_day": signed_day,
        "handoff_day": handoff_day,
        "overlap_days": days if active else 0,
        "assumed_one_day": assumed and active,
        "moved_kw": format(moved, "f"),
        "remaining_kw": format(state.remaining_kw, "f"),
        "dual_terms_cent": format(q2(moved * new_rate) * days if active else ZERO, "f"),
        "reserve_hold_cent": format(q2(guarantee.reserve_kw * new_rate) * days if active else ZERO, "f"),
        "other_terms_until_handoff_cent": format(
            q2((new_rate - old_rate) * moved) * days if active else ZERO, "f"
        ),
        "note": "planning charge for keeping both clauses live; not a metered quantity",
    }

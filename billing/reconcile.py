"""Local reconciliation of a snapshot, before and after corrections.

The point of this module is reproducibility: finance must be able to take the
exact revision that produced a fee statement, re-load the source tables as they
were then, and land on the same numbers.  A correction or manual transition is
therefore a *new revision* that names the revision it is based on; it never
edits history in place.

Checks
------
* Exactly one authoritative reading per meter interval (``contested`` when two
  readings both survive, which is a warning because the latest day wins).
* Blank supply intervals, flagged as reconstructable only when every child
  meter was observed.
* Dangling supersession references.
* Source rows that changed under a ledger revision (the snapshot no longer
  matches the files on disk).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import BillingError
from .evidence import resolve_terminal
from .sources import Reading, SourceBundle, load_sources


@dataclass(slots=True)
class Finding:
    code: str
    severity: str
    message: str
    scope: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "scope": self.scope}


@dataclass(slots=True)
class Reconciliation:
    station: str
    intervals: int = 0
    live: int = 0
    unreconciled: list[tuple[int, str]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.unreconciled and not [f for f in self.findings if f.severity == "error"]

    def as_dict(self) -> dict[str, object]:
        return {
            "station": self.station,
            "intervals": self.intervals,
            "live": self.live,
            "unreconciled": [{"minute": m, "reason": r} for m, r in self.unreconciled],
            "findings": [f.as_dict() for f in self.findings],
            "clean": self.clean,
        }


def reconcile(bundle: SourceBundle, station: str | None = None) -> Reconciliation:
    """Check every station (or one) for a single authoritative reading/interval.

    An interval is *unreconciled* when its authoritative reading is blank (not
    observed and not corrected).  That is reported, never silently counted as
    zero.  A blank supply interval is still partially reconcilable when every
    child meter was observed -- the boundary can be reconstructed from its
    components -- and that is stated explicitly.
    """
    result = Reconciliation(station or "*")
    wanted = {
        key: group for key, group in bundle.readings_by_interval().items()
        if station is None or key[0] == station
    }
    result.intervals = len(wanted)
    authorities: dict[tuple[str, str, int], Reading | None] = {
        key: resolve_terminal(group) for key, group in wanted.items()
    }
    for (s, m, minute), group in sorted(wanted.items()):
        terminal = authorities[(s, m, minute)]
        if terminal is None:
            result.findings.append(Finding("no-live", "error", f"{s}/{m}@{minute} has no authoritative reading"))
            continue
        result.live += 1
        superseded = {r.supersedes for r in group if r.supersedes}
        rivals = [r for r in group if r.reading not in superseded]
        if len(rivals) > 1:
            result.findings.append(Finding(
                "contested", "warning",
                f"{s}/{m}@{minute} has competing readings {sorted(r.reading for r in rivals)}; "
                f"{terminal.reading} is authoritative",
            ))
        if terminal.blank:
            children = [
                role.meter for role in bundle.roles.values()
                if role.station == s and role.role in {"tenant", "buffer"}
            ]
            child_values = [authorities.get((s, child, minute)) for child in children]
            reconstructable = bool(children) and all(
                reading is not None and reading.kw is not None for reading in child_values
            )
            result.unreconciled.append((
                minute,
                f"{s}/{m} blank at day {terminal.observed_day}"
                + ("; reconstructable from observed components" if reconstructable else "; no component coverage"),
            ))
        for reading in group:
            if reading.supersedes:
                target = next((r for r in group if r.reading == reading.supersedes), None)
                if target is None:
                    result.findings.append(Finding(
                        "dangling-supersede", "error",
                        f"{reading.reading} supersedes {reading.supersedes} which is not in this interval",
                    ))
    return result


def check_against_source(bundle: SourceBundle, root: str) -> list[Finding]:
    """Detect source CSV drift under an existing ledger snapshot."""
    findings: list[Finding] = []
    try:
        live = load_sources(root)
    except BillingError as exc:
        return [Finding("source-unreadable", "error", str(exc))]
    snap = bundle.snapshot()
    current = live.snapshot()
    for name, rows in snap.items():
        if rows != current.get(name, []):
            findings.append(Finding(
                "source-drift", "error",
                f"{name} has changed since the snapshot was taken; create a new revision",
            ))
    for finding in live.report.findings:
        findings.append(Finding(
            f"source-{finding.code}", finding.severity, finding.message, finding.source,
        ))
    return findings

"""Append-only local ledger for corrections, transitions, settlements and migrations.

The source CSV files are read-only history.  Everything an operator adds lives
here as a numbered revision, so any fee quote can name the exact revision and
snapshot digest it was computed from.  Writes are atomic.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CorrectionError, TransitionError

SCHEMA = 1


def _digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Ledger:
    path: Path

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ------------------------------------------------------------------ store
    def _empty(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "revisions": [], "corrections": [], "events": [], "settlements": [], "migrations": []}

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        with self.path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("schema") != SCHEMA:
            raise CorrectionError(f"ledger schema {data.get('schema')!r} is not supported")
        for key in ("revisions", "corrections", "events", "settlements", "migrations"):
            data.setdefault(key, [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data["digest"] = _digest({k: v for k, v in data.items() if k != "digest"})
        handle, temporary = tempfile.mkstemp(prefix=".ledger-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=1, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # ------------------------------------------------------------- revisions
    def current_revision(self) -> int:
        revisions = self.load()["revisions"]
        return revisions[-1]["revision"] if revisions else 0

    def revisions(self) -> list[dict[str, Any]]:
        return self.load()["revisions"]

    def snapshot(self, revision: int | None = None) -> dict[str, Any]:
        revisions = self.load()["revisions"]
        if not revisions:
            raise CorrectionError("ledger has no revision; run init first")
        if revision is None:
            entry = revisions[-1]
        else:
            matches = [r for r in revisions if r["revision"] == revision]
            if not matches:
                raise CorrectionError(f"unknown revision {revision}")
            entry = matches[0]
        return entry["snapshot"]

    def commit(self, action: str, snapshot: dict[str, Any], summary: str) -> int:
        data = self.load()
        revision = (data["revisions"][-1]["revision"] if data["revisions"] else 0) + 1
        data["revisions"].append(
            {
                "revision": revision,
                "action": action,
                "summary": summary,
                "snapshot_digest": _digest(snapshot),
                "snapshot": snapshot,
            }
        )
        self._save(data)
        return revision

    # ------------------------------------------------------------ corrections
    def corrections(self) -> list[dict[str, Any]]:
        return self.load()["corrections"]

    def corrected_readings(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for correction in self.corrections():
            rows.extend(correction["readings"])
        return rows

    def settlements(self) -> list[dict[str, Any]]:
        return self.load()["settlements"]

    def add_correction(
        self,
        correction_id: str,
        at_day: int,
        readings: list[dict[str, str]],
        note: str,
        affects_settlement: bool = False,
    ) -> None:
        data = self.load()
        if not data["revisions"]:
            raise CorrectionError("ledger has no revision; run init first")
        if any(c["id"] == correction_id for c in data["corrections"]):
            raise CorrectionError(f"correction {correction_id} already exists")
        touching = [
            s for s in data["settlements"]
            if any(
                r["station"] == s["station"] and s["window"][0] <= int(r["timestamp_minute"]) < s["window"][1]
                for r in readings
            )
        ]
        if touching and not affects_settlement:
            names = ", ".join(sorted({s["id"] for s in touching}))
            raise CorrectionError(
                f"correction {correction_id} changes intervals used by {names}; "
                "re-run with --restate to record the restatement"
            )
        data["corrections"].append(
            {
                "id": correction_id,
                "at_day": at_day,
                "note": note,
                "readings": readings,
                "affects_settlement": [s["id"] for s in touching],
            }
        )
        self._save(data)

    # ----------------------------------------------------------------- events
    def events(self) -> list[dict[str, Any]]:
        return self.load()["events"]

    def transition_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for event in self.events():
            rows.append(
                {
                    "event": event["event"],
                    "right": event["right"],
                    "day": str(event["day"]),
                    "state": event["state"],
                    "effective_minute": str(event["effective_minute"]),
                    "temporary_site": event.get("temporary_site", ""),
                }
            )
        return rows

    def add_event(
        self,
        event_id: str,
        right: str,
        state: str,
        effective_minute: int,
        temporary_site: str,
        day: int,
        valid_states: set[str],
        rights: dict[str, Any],
    ) -> None:
        data = self.load()
        if any(e["event"] == event_id for e in data["events"]):
            raise TransitionError(f"event {event_id} already exists")
        if right not in rights:
            raise TransitionError(f"unknown right {right}")
        if state not in valid_states:
            raise TransitionError(f"unknown transition state {state!r}")
        guarantee = rights[right]
        if not guarantee.start_minute <= effective_minute <= guarantee.end_minute:
            raise TransitionError(
                f"effective minute {effective_minute} outside right window "
                f"[{guarantee.start_minute}, {guarantee.end_minute}]"
            )
        if state in {"offered", "accepted", "partial_signed", "handoff_received"} and not temporary_site:
            raise TransitionError(f"state {state} requires a temporary site")
        data["events"].append(
            {
                "event": event_id,
                "right": right,
                "day": day,
                "state": state,
                "effective_minute": effective_minute,
                "temporary_site": temporary_site,
            }
        )
        self._save(data)

    # ------------------------------------------------------------ settlements
    def add_settlement(
        self,
        settlement_id: str,
        at_day: int,
        revision: int,
        station: str,
        window: tuple[int, int],
        conditions: dict[str, Any],
        note: str,
    ) -> None:
        data = self.load()
        if any(s["id"] == settlement_id for s in data["settlements"]):
            raise CorrectionError(f"settlement {settlement_id} already exists")
        snapshots = {r["revision"]: r for r in data["revisions"]}
        if revision not in snapshots:
            raise CorrectionError(f"unknown revision {revision}")
        data["settlements"].append(
            {
                "id": settlement_id,
                "at_day": at_day,
                "revision": revision,
                "snapshot_digest": snapshots[revision]["snapshot_digest"],
                "station": station,
                "window": list(window),
                "conditions": conditions,
                "note": note,
            }
        )
        self._save(data)

    # ------------------------------------------------------------- migrations
    def migrations(self) -> list[dict[str, Any]]:
        return self.load()["migrations"]

    def add_migration(
        self,
        migration_id: str,
        right: str,
        deliver_to: str,
        effective_minute: int,
        requested_day: int,
        accepted_day: int | None,
        reserve_confirmation: dict[str, Any] | None,
        note: str,
        signed_kw: object = None,
    ) -> None:
        data = self.load()
        if any(m["id"] == migration_id for m in data["migrations"]):
            raise TransitionError(f"migration {migration_id} already exists")
        active = [m for m in data["migrations"] if m["right"] == right and m["withdrawn_day"] is None]
        if active:
            raise TransitionError(
                f"right {right} already has active migration {active[0]['id']}; withdraw it first"
            )
        data["migrations"].append(
            {
                "id": migration_id,
                "right": right,
                "deliver_to": deliver_to,
                "effective_minute": effective_minute,
                "requested_day": requested_day,
                "accepted_day": accepted_day,
                "signed_kw": None if signed_kw is None else str(signed_kw),
                "reserve_confirmation": reserve_confirmation,
                "withdrawn_day": None,
                "note": note,
            }
        )
        self._save(data)

    def withdraw_migration(self, migration_id: str, day: int, note: str) -> None:
        data = self.load()
        matches = [m for m in data["migrations"] if m["id"] == migration_id]
        if not matches:
            raise TransitionError(f"unknown migration {migration_id}")
        migration = matches[0]
        if migration["withdrawn_day"] is not None:
            raise TransitionError(f"migration {migration_id} already withdrawn")
        migration["withdrawn_day"] = day
        migration["withdrawal_note"] = note
        self._save(data)

    def accept_migration(self, migration_id: str, day: int, note: str) -> None:
        data = self.load()
        matches = [m for m in data["migrations"] if m["id"] == migration_id]
        if not matches:
            raise TransitionError(f"unknown migration {migration_id}")
        migration = matches[0]
        if migration["withdrawn_day"] is not None:
            raise TransitionError(f"migration {migration_id} was withdrawn")
        if migration["accepted_day"] is not None:
            raise TransitionError(f"migration {migration_id} already fully accepted")
        migration["accepted_day"] = day
        migration["acceptance_note"] = note
        self._save(data)

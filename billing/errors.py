"""Domain errors and collected validation findings."""

from __future__ import annotations

from dataclasses import dataclass, field


class BillingError(Exception):
    """Base class for errors that should be shown to an operator."""


class SourceError(BillingError):
    """A source table is malformed or internally inconsistent."""


class CorrectionError(BillingError):
    """A meter correction cannot be applied without changing an interval."""


class TransitionError(BillingError):
    """A contract transition is not allowed by the state machine."""


@dataclass
class Finding:
    code: str
    severity: str  # error | warning | info
    message: str
    source: str = ""
    row: str = ""


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: str, message: str, source: str = "", row: str = "") -> None:
        self.findings.append(Finding(code, severity, message, source, row))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

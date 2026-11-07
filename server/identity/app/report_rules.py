"""The rules of namespace.audit and namespace.validate: what each declares, which ran
(the report's coverage), and the live revisions behind each finding's samples."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from server.identity.api.models_findings import FindingEvidence, FindingKind, ReportCoverage
from server.identity.app.request_scope import request_scope
from server.memory.app.finding_evidence import revisions_for, sample_ref

log = logging.getLogger(__name__)

# The citation read's name in a report's coverage and skipped lines.
EVIDENCE = "evidence"
# Under Postgres, the wait for ClickHouse to publish what the namespace committed
# (`pg_validation.published_reads`), skipped when it did not finish.
PROJECTION = "projection"


class RuleContract(NamedTuple):
    """What a rule declares beside its SQL: its kind, its version, what it leaves out by
    design and why a hit may still be correct."""

    kind: FindingKind
    version: int
    exceptions: str = ""
    uncertainty: str = ""
    # Counted over the request's ledger window rather than current state.
    windowed: bool = False


class _Sampled(Protocol):
    samples: list[dict[str, Any]]
    evidence: list[FindingEvidence]


class RuleLog:
    """The rules a report ran, and those it skipped with the reason, in order."""

    def __init__(self) -> None:
        self._ran: list[str] = []
        self._skipped: dict[str, str] = {}

    def ran(self, name: str) -> None:
        self._ran.append(name)

    def skip(self, name: str, reason: str) -> None:
        self._skipped[name] = reason

    def has_run(self, name: str) -> bool:
        return name in self._ran

    def was_skipped(self, name: str) -> bool:
        return name in self._skipped

    @property
    def skipped(self) -> list[str]:
        """The report's readable `skipped` lines, "name: reason"."""
        return [f"{name}: {reason}" for name, reason in self._skipped.items()]

    @property
    def coverage(self) -> ReportCoverage:
        return ReportCoverage(
            rules_run=list(self._ran),
            rules_skipped=list(self._skipped),
            complete=not self._skipped,
        )


async def attach_evidence(
    org: str,
    namespace: str,
    findings: Sequence[_Sampled],
    rules: RuleLog,
    *,
    ch: Any,
) -> None:
    """Cite each sample's live revision (by memory_id or full identity); a failed read
    leaves every finding uncited and is recorded as skipped."""
    samples = [s for f in findings for s in f.samples]
    try:
        by_ref = await revisions_for(org, namespace, samples, scope=request_scope(), ch=ch)
    except Exception as exc:  # noqa: BLE001 — advisory report; an uncited finding is still a finding
        log.warning("%s/%s evidence read skipped: %s", org, namespace, exc)
        rules.skip(EVIDENCE, type(exc).__name__)
        by_ref = {}
    else:
        rules.ran(EVIDENCE)
    for f in findings:
        # Each row once: one memory row can stand behind several embedding-row samples.
        refs = dict.fromkeys(r for r in (sample_ref(s) for s in f.samples) if r is not None)
        cited = [by_ref.get(ref) for ref in refs]
        f.evidence = [FindingEvidence(**row) for row in cited if row is not None]

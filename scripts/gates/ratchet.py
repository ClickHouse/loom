"""Ratchet baselines shared by the mypy and function-length gates.

A ratchet baseline is a committed JSON object `{key: count}` (a key is a file,
or a `file::function`). A gate measures the same counts on the working tree and
fails when any key's count is above its baseline entry (a key absent from the
baseline has a baseline of 0). Debt therefore cannot move between files:
fixing three errors in one module does not buy three new ones in another.
Counts that fell are reported so the baseline can be lowered (`--update`
writes the shrunk file; it never raises a number and never creates one).

A baseline that is missing or malformed makes the gate fail (exit 2): a gate
that cannot read its baseline has measured nothing.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


class GateError(RuntimeError):
    """The gate could not measure. A gate that cannot measure fails."""


@dataclass
class Report:
    regressions: dict[str, tuple[int, int]] = field(default_factory=dict)  # key → (now, base)
    decreases: dict[str, tuple[int, int]] = field(default_factory=dict)  # key → (now, base)

    @property
    def ok(self) -> bool:
        return not self.regressions


def compare(baseline: Mapping[str, int], current: Mapping[str, int]) -> Report:
    """Compare per-key counts against the committed baseline.

    A key is a regression when its count exceeds the baseline (0 when the key
    is not listed). A key whose count fell, including one the tree no longer
    has, is a decrease.
    """
    report = Report()
    for key in sorted(set(baseline) | set(current)):
        now, base = current.get(key, 0), baseline.get(key, 0)
        if now > base:
            report.regressions[key] = (now, base)
        elif now < base:
            report.decreases[key] = (now, base)
    return report


def lowered(baseline: Mapping[str, int], current: Mapping[str, int]) -> dict[str, int]:
    """The baseline after an `--update`: every entry is min(baseline, current).

    Never raises a count: a regression must be fixed, not committed.
    """
    merged = {k: min(baseline.get(k, 0), current.get(k, 0)) for k in set(baseline) | set(current)}
    return {k: v for k, v in merged.items() if v > 0}


def normalise_key(key: str) -> str:
    """A baseline key in the form every gate emits: POSIX separators."""
    return key.replace("\\", "/")


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise GateError(f"duplicate key {key!r}")
        out[key] = value
    return out


def load_baseline(path: Path) -> dict[str, int]:
    """The committed `{key: count}`; raises GateError when missing or malformed.

    Counts are non-negative JSON integers. Keys are normalised to POSIX
    separators; two keys that normalise to one are a duplicate.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise GateError(f"cannot read baseline {path}: {e}") from e
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicates)
    except GateError as e:
        raise GateError(f"{path}: {e}") from e
    except json.JSONDecodeError as e:
        raise GateError(f"{path}: not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise GateError(f"{path}: baseline must be a JSON object of {{key: count}}")
    out: dict[str, int] = {}
    for raw, value in data.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GateError(f"{path}: {raw!r} must map to a non-negative integer, got {value!r}")
        key = normalise_key(raw)
        if key in out:
            raise GateError(f"{path}: duplicate key {key!r} (after normalising separators)")
        out[key] = value
    return out


def write_baseline(path: Path, counts: Mapping[str, int]) -> None:
    """Write the baseline atomically: sorted, zero counts dropped, one entry per line."""
    kept = {k: int(v) for k, v in sorted(counts.items()) if int(v) > 0}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(kept, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def render(report: Report, *, unit: str) -> list[str]:
    lines = [
        f"  REGRESSION {key}: {now} {unit} (baseline {base})"
        for key, (now, base) in report.regressions.items()
    ]
    lines += [
        f"  lowered    {key}: {now} {unit} (baseline {base})"
        if now
        else f"  cleared    {key}: gone or under the threshold (baseline {base} {unit})"
        for key, (now, base) in report.decreases.items()
    ]
    return lines


class Ratchet(ABC):
    """One gate over one baseline file: measure, compare, report, lower.

    A subclass says what it measures and how to explain a failure; the
    compare / update / exit-code protocol is shared.
    """

    name: ClassVar[str]  # output prefix, e.g. "mypy-gate"
    unit: ClassVar[str]  # what a count counts, e.g. "error(s)"
    subject: ClassVar[str] = "file(s)"  # what a key is
    label: ClassVar[str] = "baseline"
    command: ClassVar[str]  # how to run the gate, for the `--update` hint

    def __init__(self, baseline_path: Path) -> None:
        self.baseline_path = baseline_path

    @abstractmethod
    def measure(self) -> Mapping[str, int]:
        """The working tree's counts; raises GateError when it cannot measure."""

    @abstractmethod
    def headline(self, current: Mapping[str, int], baseline: Mapping[str, int]) -> list[str]:
        """The summary lines printed before the per-key report."""

    @abstractmethod
    def failure(self, report: Report) -> str:
        """Why the gate failed, after `FAIL — `."""

    def evidence(self, report: Report) -> list[str]:
        """The offending findings, printed on a failure."""
        return []

    def run(self, *, update: bool = False) -> int:
        """0 at or under the baseline, 1 on a regression, 2 when unmeasurable.

        `update` writes only when every key is at or under its entry, and then
        only lowers: growth is never accepted, and no baseline is created.
        """
        try:
            baseline = load_baseline(self.baseline_path)
            current = dict(self.measure())
        except GateError as e:
            print(f"{self.name}: {e} — treating as FAILURE")
            return 2
        report = compare(baseline, current)
        for line in [*self.headline(current, baseline), *render(report, unit=self.unit)]:
            print(line)
        if not report.ok:
            for line in self.evidence(report):
                print(line)
            print(f"{self.name}: FAIL — {self.failure(report)}")
            return 1
        if report.decreases:
            if update:
                write_baseline(self.baseline_path, lowered(baseline, current))
                print(f"{self.name}: {self.label} lowered and written to {self.baseline_path}")
            else:
                print(
                    f"{self.name}: {len(report.decreases)} {self.subject} below their "
                    f"{self.label} — run `{self.command} --update` to lock in the win."
                )
        return 0

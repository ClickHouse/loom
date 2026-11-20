"""Scripted subprocess fixture; never a model or historical task runner."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from examples.software_factory.__main__ import ROOT
from examples.software_factory.adapters import ReplayContext, ReplayProvider
from examples.software_factory.cohort import Cohort
from examples.software_factory.controller import run_active
from examples.software_factory.model import Task
from examples.software_factory.workspace import GradingBudgetExceeded, ObjectiveGrader


def prepare(task_id, output):
    entry = json.loads((ROOT / "suite.json").read_text())["tasks"][task_id]
    task = Task.load(ROOT / entry["task"]["path"])
    grader = ObjectiveGrader(ROOT / entry["grader"]["path"], entry["grader"]["sha256"])
    return task, grader


class ScriptedProvider(ReplayProvider):
    def __init__(self, output, *, broken_close=False):
        super().__init__(ROOT / "replays/name-validation.json")
        self.output, self.broken_close = output, broken_close

    def step(self, *args):
        return {
            **super().step(*args),
            "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
        }

    def close(self):
        (self.output / "provider-closed").touch()
        if self.broken_close:
            raise RuntimeError("scripted cleanup failure")


class ScriptedContext(ReplayContext):
    def __init__(self, task, output):
        super().__init__(task)
        self.output = output

    def close(self):
        (self.output / "context-closed").touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("order", type=int)
    parser.add_argument("behavior")
    args = parser.parse_args()
    protocol = json.loads((args.root / "protocol.json").read_text())
    if args.behavior in {"crash", "hang"}:
        with Cohort(args.root, protocol) as cohort:
            cohort.reserve(100, 5)
        (args.output.parent / "worker-pid").write_text(str(os.getpid()))
        if args.behavior == "hang":
            time.sleep(10)
        return 23

    def context(task, output, audit):
        if protocol["schedule"][args.order]["mode"] == "baseline":
            raise AssertionError("Baseline must never construct a context client")
        return ScriptedContext(task, output)

    def grader(task_id, output):
        task, oracle = prepare(task_id, output)
        if args.behavior in {"visible-grader-failure", "visible-budget-exhausted"}:

            def fail_visible(checkout, *, hidden=False):
                if not hidden:
                    if args.behavior == "visible-budget-exhausted":
                        raise GradingBudgetExceeded("scripted visible budget exhausted")
                    raise RuntimeError("scripted visible infrastructure failure")
                return {"passed": True, "grader_sha256": oracle.sha256}

            oracle.run = fail_visible
        return task, oracle

    run_active(
        args.root,
        protocol,
        args.order,
        args.output,
        verify_eligibility=lambda _: None,  # Authored fixture only; not real approval.
        prepare=grader,
        provider_factory=lambda output, audit: ScriptedProvider(
            output, broken_close=args.behavior == "cleanup-failure"
        ),
        context_factory=context,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

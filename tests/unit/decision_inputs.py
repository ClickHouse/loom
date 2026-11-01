"""A `decision.record` input for the decision tests, with every field filled."""

from __future__ import annotations

from typing import Any

from server.memory.api.models_decision import DecisionRecordInput


def record_input(**over: Any) -> DecisionRecordInput:
    base: dict[str, Any] = dict(
        org="o",
        namespace="n",
        agent="claude",
        run_id="r1",
        category="Deploy",
        scenario="Roll back or hotfix the 26.5 image?",
        reasoning="Rollback is 2 minutes; the fix is untested.",
        outcome="rollback",
        alternatives=["hotfix forward", "wait"],
        confidence=0.8,
        caused_by=["ci:flaky-runner"],
        influenced_by=["policy:no-friday-deploys", ""],
    )
    base.update(over)
    return DecisionRecordInput(**base)

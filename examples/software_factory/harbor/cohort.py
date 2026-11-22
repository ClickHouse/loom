"""Durable, serial cohort accounting for an injected trusted controller.

This module grants no spending permission and constructs no clients. The caller
must verify the approved protocol, integration, input bounds and billing sources.
An interrupted attempt or unsettled dispatch requires explicit reconciliation;
opening the journal never retries it.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
from pathlib import Path

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.live_support import Reservations


class Cohort:
    """Hold one process lock and fsync each intent before exposing it to callers."""

    def __init__(self, root: Path, protocol: dict):
        self.root, self.protocol = root, copy.deepcopy(protocol)
        self.sha256 = digest(self.protocol)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stream = (root / "cohort.jsonl").open("a+")
        self.poisoned = False
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.stream.seek(0)
            self.sequence, self.previous = 0, None
            self.state = {}
            for line in self.stream:
                if not line.endswith("\n"):
                    raise ValueError("Cohort journal has an incomplete append")
                row = json.loads(line)
                checksum = row.pop("sha256")
                if (
                    checksum != digest(row)
                    or row["sequence"] != self.sequence
                    or row["previous"] != self.previous
                ):
                    raise ValueError("Cohort journal is incomplete or altered")
                self.sequence, self.previous = self.sequence + 1, checksum
                self.state = row["state"]
            if self.sequence:
                if self.state["protocol_sha256"] != self.sha256:
                    raise PermissionError("Protocol differs from the durable cohort")
            else:
                schedule = self.protocol["schedule"]
                if not schedule or [r["order"] for r in schedule] != list(range(len(schedule))):
                    raise ValueError("Freeze a nonempty, ordered attempt schedule")
                budget = Reservations(**self.protocol["model_reservation"])
                self.state = {}
                self._commit(
                    "created",
                    {
                        "protocol_sha256": self.sha256,
                        "attempts": [{"state": "pending"} for _ in schedule],
                        "accounting": self._snapshot(budget),
                    },
                    protocol=self.protocol,
                )
            # Persist the journal's directory entry before any dispatch can start.
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _snapshot(budget: Reservations) -> dict:
        return {
            "spent": dict(budget.spent),
            "reserved_usd": str(budget.reserved),
            "unknown": budget.unknown,
            "rows": copy.deepcopy(budget.rows),
        }

    def _budget(self) -> Reservations:
        budget = Reservations(**self.protocol["model_reservation"])
        state = self.state["accounting"]
        budget.spent = dict(state["spent"])
        budget.reserved = type(budget.reserved)(state["reserved_usd"])
        budget.unknown, budget.rows = state["unknown"], copy.deepcopy(state["rows"])
        return budget

    def _commit(self, event: str, state: dict, **fields) -> None:
        if self.poisoned or self.stream.closed:
            raise RuntimeError("Cohort journal is unavailable")
        row = {
            "sequence": self.sequence,
            "previous": self.previous,
            "event": event,
            "state": copy.deepcopy(state),
            **fields,
        }
        row["sha256"] = digest(row)
        try:
            self.stream.write(json.dumps(row, sort_keys=True) + "\n")
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except BaseException:
            self.poisoned = True  # A partial append must never be retried in this process.
            raise
        self.sequence, self.previous = self.sequence + 1, row["sha256"]
        self.state = copy.deepcopy(state)

    def begin(self, verify_eligibility) -> dict | None:
        """Recheck external authorization/dependencies before each cold attempt."""
        if self.state["accounting"]["unknown"] or any(
            a["state"] == "running" for a in self.state["attempts"]
        ):
            raise PermissionError(
                "Interrupted attempt or unsettled billing requires reconciliation"
            )
        for order, attempt in enumerate(self.state["attempts"]):
            if attempt["state"] != "pending":
                continue
            verify_eligibility(copy.deepcopy(self.protocol))
            state = copy.deepcopy(self.state)
            state["attempts"][order] = {"state": "running"}
            self._commit("attempt_started", state, order=order)
            return copy.deepcopy(self.protocol["schedule"][order])
        return None

    def _active(self) -> int:
        active = [i for i, a in enumerate(self.state["attempts"]) if a["state"] == "running"]
        if len(active) != 1:
            raise PermissionError("Exactly one durable active attempt is required")
        return active[0]

    def reserve(self, input_bound: int, output_bound: int) -> int:
        order, budget = self._active(), self._budget()
        budget.reserve(input_bound, output_bound)
        state = copy.deepcopy(self.state)
        state["accounting"] = self._snapshot(budget)
        request = len(budget.rows) - 1
        self._commit("request_reserved", state, order=order, request=request)
        return request

    def observe(self, request: int, response: dict) -> None:
        self._pending(request)
        fields = ("usage", "provider_request_id", "physical_requests", "cost_usd", "cost_status")
        self._commit(
            "response_observed",
            self.state,
            request=request,
            response={k: copy.deepcopy(response.get(k)) for k in fields},
        )

    def failure(self, request: int, error: Exception) -> None:
        self._pending(request)
        self._commit(
            "request_failed",
            self.state,
            request=request,
            error_type=type(error).__name__,
            physical_requests=getattr(error, "physical_requests", None),
            provider_request_id=getattr(error, "provider_request_id", None),
        )

    def _pending(self, request: int) -> None:
        self._active()
        if (
            type(request) is not int
            or request != len(self.state["accounting"]["rows"]) - 1
            or not self.state["accounting"]["unknown"]
        ):
            raise PermissionError("Request has no pending reservation")

    def settle(self, request: int, usage: dict):
        self._pending(request)
        budget = self._budget()
        cost = budget.settle(usage)
        state = copy.deepcopy(self.state)
        state["accounting"] = self._snapshot(budget)
        self._commit("request_settled", state, request=request)
        return cost

    def finish(self, result: dict) -> None:
        order = self._active()
        state = copy.deepcopy(self.state)
        state["attempts"][order] = {"state": "finished", "result": copy.deepcopy(result)}
        self._commit("attempt_finished", state, order=order)


class ReservedProvider:
    """Reserve verified bounds before dispatch, retaining failed or unknown usage.

    Cost here covers generation only. Complete server/transport billing is a
    separate required live-run qualification; it must never be reported as zero.
    """

    def __init__(self, provider, cohort: Cohort, *, verified_input_bound: int, usage_adapter=None):
        if type(verified_input_bound) is not int or verified_input_bound <= 0:
            raise ValueError("An independently verified input bound is required")
        self.provider, self.cohort, self.bound = provider, cohort, verified_input_bound
        self.label = provider.label
        self.usage_adapter = usage_adapter

    def step(self, messages: list[dict], tools: list[dict], max_tokens: int) -> dict:
        request = self.cohort.reserve(self.bound, max_tokens)
        # Failure leaves the fsynced reservation pending, including after SIGKILL.
        try:
            response = self.provider.step(messages, tools, max_tokens)
        except Exception as error:  # Retain dispatch failures without retrying or releasing budget.
            self.cohort.failure(request, error)
            raise
        self.cohort.observe(request, response)
        usage = self.usage_adapter(response) if self.usage_adapter else response["usage"]
        cost = self.cohort.settle(request, usage)
        return {
            **response,
            "generation_cost_usd": float(cost),
            "generation_cost_status": "priced",
            "cost_usd": None,
            "cost_status": "unknown",
        }

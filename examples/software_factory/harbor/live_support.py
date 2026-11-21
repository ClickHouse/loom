"""Nonspending live-run preconditions, scope verification and usage reservations."""

from __future__ import annotations

import copy
import random
import re
import time
from decimal import Decimal

from .arms import ALL, CONTROLS, PRIMARY


def _request_id(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        return value
    return None


class TransportAudit:
    """Attach public httpx event hooks or botocore before-send/after-call hooks.

    Count dispatches before I/O so failures are retained. No request content,
    authorization headers or credential values enter this ledger.
    """

    def __init__(self, *, max_requests: int | None = None, record=None):
        if max_requests is not None and (type(max_requests) is not int or max_requests <= 0):
            raise ValueError("Physical request cap must be positive")
        self.rows: list[dict] = []
        self.max_requests, self.record = max_requests, record

    def emit(self, row: dict) -> None:
        if (
            row["phase"] == "dispatch"
            and self.max_requests is not None
            and self.count() >= self.max_requests
        ):
            raise TimeoutError("Physical transport request cap exhausted")
        if self.record:
            self.record(dict(row))
        self.rows.append(row)

    def count(self) -> int:
        return sum(row["phase"] == "dispatch" for row in self.rows)

    async def request(self, request) -> None:
        self.emit(
            {
                "phase": "dispatch",
                "time": time.monotonic(),
                "path": request.url.path,
                "request_id": _request_id(request.headers.get("x-request-id")),
            }
        )

    async def response(self, response) -> None:
        self.emit(
            {
                "phase": "response",
                "time": time.monotonic(),
                "status": response.status_code,
                "path": response.request.url.path,
                "request_id": _request_id(response.headers.get("x-request-id")),
            }
        )

    def before_send(self, **kwargs) -> None:
        self.emit({"phase": "dispatch", "time": time.monotonic(), "path": "Converse"})

    def after_call(self, **kwargs) -> None:
        response = kwargs.get("http_response")
        parsed = kwargs.get("parsed")
        metadata = parsed.get("ResponseMetadata") if isinstance(parsed, dict) else None
        request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
        self.emit(
            {
                "phase": "response",
                "time": time.monotonic(),
                "status": getattr(response, "status_code", None),
                "path": "Converse",
                "request_id": _request_id(request_id),
            }
        )


class Reservations:
    """Global pre-dispatch ceilings; failures keep their entire reservation.

    Rates are dollars per million tokens, frozen from an authoritative source.
    Every response usage field must have an explicit pricing interpretation.
    Unknown or incomplete billing stops subsequent requests rather than becoming 0.
    """

    def __init__(
        self,
        *,
        requests: int,
        input_tokens: int,
        output_tokens: int,
        dollars: str,
        input_rate: str,
        output_rate: str,
    ):
        self.limits = {
            "requests": requests,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        self.spent = {key: 0 for key in self.limits}
        self.dollars = Decimal(dollars)
        self.input_rate, self.output_rate = Decimal(input_rate), Decimal(output_rate)
        if any(type(v) is not int or v <= 0 for v in self.limits.values()) or any(
            not v.is_finite() or v <= 0 for v in (self.dollars, self.input_rate, self.output_rate)
        ):
            raise ValueError("Freeze positive request, token, cost ceilings and verified prices")
        self.reserved = Decimal(0)
        self.unknown = False
        self.rows: list[dict] = []

    def reserve(self, input_bound: int, output_bound: int) -> None:
        if self.unknown or any(type(v) is not int or v <= 0 for v in (input_bound, output_bound)):
            raise PermissionError("Billing or token bounds are unverified")
        delta = {"requests": 1, "input_tokens": input_bound, "output_tokens": output_bound}
        cost = (input_bound * self.input_rate + output_bound * self.output_rate) / 1000000
        if self.reserved + cost > self.dollars or any(
            self.spent[k] + delta[k] > self.limits[k] for k in delta
        ):
            raise TimeoutError("Global cohort reservation exhausted")
        for key in delta:
            self.spent[key] += delta[key]
        self.reserved += cost
        self.unknown = True  # Clear only after complete settlement; failed calls remain reserved.
        self.rows.append({"reserved": delta, "reserved_usd": str(cost), "settled": False})

    def settle(self, usage: dict) -> Decimal:
        expected = {"inputTokens", "outputTokens", "totalTokens"}
        if set(usage) != expected or any(type(v) is not int or v < 0 for v in usage.values()):
            raise ValueError("Unknown usage/cache fields require a frozen pricing rule")
        row = self.rows[-1]
        if (
            row["settled"]
            or not self.unknown
            or usage["totalTokens"] != usage["inputTokens"] + usage["outputTokens"]
        ):
            raise ValueError("Usage is not a complete pending response")
        if any(
            usage[field] > row["reserved"][key]
            for field, key in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"))
        ):
            raise ValueError("Provider usage exceeded the verified token bound")
        cost = (
            usage["inputTokens"] * self.input_rate + usage["outputTokens"] * self.output_rate
        ) / 1000000
        row.update(usage=copy.deepcopy(usage), cost_usd=str(cost), settled=True)
        self.unknown = False
        return cost


def schedule(
    task_ids: list[str], *, seed: int, repeats: int = 3, task_families: dict | None = None
) -> list[dict]:
    if (
        not task_ids
        or any(not isinstance(task, str) or not task for task in task_ids)
        or len(task_ids) != len(set(task_ids))
        or type(repeats) is not int
        or repeats != 3
        or type(seed) is not int
    ):
        raise ValueError("Freeze unique scenario ids, three repeats and an integer seed")
    rng, rows = random.Random(seed), []
    groups, task_order, arms = [], sorted(task_ids), list(PRIMARY)
    rng.shuffle(task_order)
    rng.shuffle(arms)
    if task_families is not None:
        if set(task_families) != set(task_ids) or any(
            not isinstance(family, str) or not family for family in task_families.values()
        ):
            raise ValueError("Declare every task family before scheduling")
        families = list(dict.fromkeys(task_families[task] for task in task_order))
        positions = [
            task for family in families for task in task_order if task_families[task] == family
        ]
    else:
        positions = task_order
    ordinals = {task: index for index, task in enumerate(positions)}
    for task in task_order:
        for repeat in range(repeats):
            # Three repeats cannot occupy all five positions. Consecutive rotations
            # minimize the position imbalance across the entire frozen scenario set.
            offset = (ordinals[task] * repeats + repeat) % len(arms)
            groups.append((task, repeat, arms[offset:] + arms[:offset]))
        aa = list(CONTROLS)
        rng.shuffle(aa)
        groups.append((task, "aa", aa))
    rng.shuffle(groups)
    for task, repeat, arms in groups:
        for arm in arms:
            rows.append(
                {
                    "order": len(rows),
                    "task_id": task,
                    "repeat": repeat,
                    "arm": arm,
                    **ALL[arm].identity(),
                }
            )
    return rows

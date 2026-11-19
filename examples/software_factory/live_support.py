"""Nonspending live-run preconditions, scope verification and usage reservations."""

from __future__ import annotations

import copy
import random
import time
from decimal import Decimal

from .model import digest


class ScopeVerifier:
    """Verify every lookup against an externally approved immutable index receipt.

    A matching watermark alone does not prove immutability or reader permission.
    The protocol must also pin an audited namespace/authorization certificate.
    Restricted counts are never interpreted as an empty index.
    """

    def __init__(
        self, scope: dict, *, index_repository: str, build_commit: str, certificate_sha256: str
    ):
        if not all(
            len(v) == n and all(c in "0123456789abcdef" for c in v)
            for v, n in (
                (scope.get("commit", ""), 40),
                (build_commit, 40),
                (certificate_sha256, 64),
            )
        ):
            raise ValueError("Freeze full source, server build and certificate hashes")
        if not index_repository or set(scope) != {"org", "namespace", "repository", "commit"}:
            raise ValueError("Complete explicit index scope is required")
        self.scope, self.repository, self.build = dict(scope), index_repository, build_commit
        self.certificate = certificate_sha256

    async def __call__(self, agent, body: dict) -> dict:
        if any(body.get(k) != self.scope[k] for k in ("org", "namespace")):
            raise PermissionError("Lookup scope differs from the frozen protocol")
        started = time.monotonic()
        result = await agent.call_with_meta(
            "code.index_status",
            {
                "org": self.scope["org"],
                "namespace": self.scope["namespace"],
                "brief": True,
            },
            retry={"max_attempts": 1},
            timeout_s=10,
        )
        data = result.data
        expected = {
            "org": self.scope["org"],
            "namespace": self.scope["namespace"],
            "repo": self.repository,
            "git_head": self.scope["commit"],
            "build_commit": self.build,
        }
        if not isinstance(data, dict) or any(data.get(k) != v for k, v in expected.items()):
            raise PermissionError("Index identity, watermark or build is unverified")
        if data.get("ignored_args") or data.get("restricted") or data.get("abstained"):
            raise PermissionError("Index scope was restricted or ignored")
        return {
            "scope": dict(self.scope),
            "permission": "allowed",
            "request_id": result.request_id,
            "duration_s": time.monotonic() - started,
            "response_sha256": digest(data),
            "certificate_sha256": self.certificate,
            "cost_usd": None,
            "cost_status": "unknown",
        }


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
        self.emit({"phase": "dispatch", "time": time.monotonic(), "path": request.url.path})

    async def response(self, response) -> None:
        self.emit(
            {
                "phase": "response",
                "time": time.monotonic(),
                "status": response.status_code,
                "path": response.request.url.path,
            }
        )

    def before_send(self, **kwargs) -> None:
        self.emit({"phase": "dispatch", "time": time.monotonic(), "path": "Converse"})

    def after_call(self, **kwargs) -> None:
        response = kwargs.get("http_response")
        self.emit(
            {
                "phase": "response",
                "time": time.monotonic(),
                "status": getattr(response, "status_code", None),
                "path": "Converse",
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


def schedule(task_ids: list[str], *, seed: int, repeats: int = 3) -> list[dict]:
    if len(task_ids) != len(set(task_ids)) or len(task_ids) != 3 or repeats != 3:
        raise ValueError("This proposed pilot freezes three tasks and three repeats")
    rng, rows = random.Random(seed), []
    groups = [
        (task, repeat, ["baseline", "orchestrated", "natural"])
        for task in task_ids
        for repeat in range(repeats)
    ]
    groups += [(task, "aa", ["baseline-a", "baseline-b"]) for task in task_ids]
    rng.shuffle(groups)
    for task, repeat, arms in groups:
        rng.shuffle(arms)
        for arm in arms:
            rows.append(
                {
                    "order": len(rows),
                    "task_id": task,
                    "repeat": repeat,
                    "arm": arm,
                    "mode": "baseline" if arm.startswith("baseline") else arm,
                }
            )
    return rows

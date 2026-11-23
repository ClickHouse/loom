"""Fsync each reservation before dispatch; unknown outcomes retain their ceiling."""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from examples.software_factory.factory.model import digest

from .pricing import MODELS, rates

ATTRIBUTION = {"org", "namespace", "feature", "order"}
USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
PRICE_BASIS = (
    "Frozen standard list prices including prompt caching; not an invoice or infrastructure cost"
)


class Spending:
    def __init__(
        self, path: Path, *, dollars: str | None, model: str, native_model="claude-haiku-5-5"
    ):
        try:
            self.limit = None if dollars is None else Decimal(dollars).normalize()
        except InvalidOperation:
            raise ValueError("Freeze a finite positive allowance") from None
        if (
            (self.limit is not None and (not self.limit.is_finite() or self.limit <= 0))
            or model not in MODELS
            or native_model not in MODELS
        ):
            raise ValueError("Freeze a positive allowance and the priced models")
        self.path = Path(path)
        self.model, self.native_model = model, native_model
        self.lock = threading.RLock()
        self._phase, self.blocked, self.closed, self.poisoned = "setup", False, False, False
        self.rows, self.pending, self.unknowns, self.request_ids = [], {}, set(), set()
        self.spent = Decimal(0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path, os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        self.file = os.fdopen(descriptor, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(self.file.fileno(), 0o600)
            self.file.seek(0)
            for line in self.file:
                if not line.endswith("\n"):
                    raise PermissionError("Incomplete spending journal requires reconciliation")
                row = json.loads(line)
                sha = row.pop("sha256")
                if (
                    sha != digest(row)
                    or type(row.get("sequence")) is not int
                    or row["sequence"] != len(self.rows)
                ):
                    raise PermissionError("Spending journal was altered")
                if (
                    row.get("model") != model
                    or row.get("native_model") != native_model
                    or row.get("limit_usd") != (None if self.limit is None else str(self.limit))
                ):
                    raise PermissionError("Spending model or allowance changed")
                self._apply(row)
            if not self.rows:
                self._append("opened", price_basis=PRICE_BASIS)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.blocked |= bool(self.pending)
        except BaseException:
            self.file.close()
            raise

    @property
    def phase(self):
        with self.lock:
            return self._phase

    @phase.setter
    def phase(self, value):
        self.set_phase(value)

    def set_phase(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value):
            raise ValueError("Use a bounded phase identity")
        with self.lock:
            self._phase = value

    def rates(self, model, incoming, *, ceiling=False):
        if model not in {self.model, self.native_model}:
            raise ValueError("Model differs from the frozen solver and native models")
        return rates(model, incoming, ceiling=ceiling)

    def _usage(self, reservation, response, request_id):
        if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
            raise ValueError("Provider usage is missing")
        usage, model = response["usage"], response.get("model")
        if (
            not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request_id)
            or request_id in self.request_ids
            or not isinstance(model, str)
            or not re.fullmatch(re.escape(reservation["request_model"]) + r"(?:-\d{8})?", model)
            or any(type(usage.get(key)) is not int or usage[key] < 0 for key in USAGE)
            or usage.get("service_tier", "standard") != "standard"
            or usage.get("inference_geo") not in {None, "global"}
            or usage["input_tokens"] > reservation["input_token_bound"]
            or usage["output_tokens"] > reservation["output_token_bound"]
        ):
            raise ValueError("Provider model, identity or standard usage is unverified")
        detail = usage.get(
            "cache_creation",
            {
                "ephemeral_5m_input_tokens": usage["cache_creation_input_tokens"],
                "ephemeral_1h_input_tokens": 0,
            },
        )
        if (
            not isinstance(detail, dict)
            or set(detail) - {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}
            or any(
                type(detail.get(key, 0)) is not int or detail.get(key, 0) < 0
                for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
            )
            or sum(detail.values()) != usage["cache_creation_input_tokens"]
            or sum(
                usage[key]
                for key in (
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )
            > reservation["input_token_bound"]
        ):
            raise ValueError("Provider cache usage is inconsistent or exceeds the input bound")
        server_tools = usage.get("server_tool_use")
        if server_tools is not None and (
            not isinstance(server_tools, dict)
            or any(type(v) is not int or v != 0 for v in server_tools.values())
        ):
            raise ValueError("Hosted tool usage has no frozen price")
        return {**{key: usage[key] for key in USAGE}, "cache_creation": detail}

    def usage_cost(self, model, usage):
        total = sum(
            usage[key]
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        incoming, outgoing = self.rates(model, total)
        detail = usage["cache_creation"]
        cache_read_multiplier = Decimal(
            "0.05" if model in {"claude-sonnet-5-5", "claude-opus-5-5"} else "0.1"
        )
        charged_input = (
            usage["input_tokens"]
            + cache_read_multiplier * usage["cache_read_input_tokens"]
            + Decimal("1.25") * detail.get("ephemeral_5m_input_tokens", 0)
            + Decimal("2") * detail.get("ephemeral_1h_input_tokens", 0)
        )
        return (
            incoming,
            outgoing,
            (charged_input * incoming + usage["output_tokens"] * outgoing) / 1000000,
        )

    def _apply(self, row):
        event = row["event"]
        if event == "opened":
            if self.rows or row.get("price_basis") != PRICE_BASIS:
                raise PermissionError("Invalid spending journal header")
        elif not self.rows:
            raise PermissionError("Spending journal header is missing")
        elif event == "reserved":
            ticket, role = row["ticket"], row["role"]
            if (
                type(ticket) is not int
                or ticket != row["sequence"]
                or ticket in self.pending
                or role not in {"solver", "judge", "loom"}
                or row["request_model"] != (self.native_model if role == "loom" else self.model)
                or type(row["input_bytes"]) is not int
                or not 0 < row["input_bytes"] <= 8 * 1024 * 1024
                or row["input_token_bound"] != row["input_bytes"] + 4096
                or type(row["output_token_bound"]) is not int
                or not 0 < row["output_token_bound"] <= 128000
                or not re.fullmatch(r"[0-9a-f]{64}", row["request_sha256"])
            ):
                raise PermissionError("Invalid reservation identity or bound")
            incoming, outgoing = self.rates(
                row["request_model"], row["input_token_bound"], ceiling=True
            )
            expected = (
                row["input_token_bound"] * incoming * 2 + row["output_token_bound"] * outgoing
            ) / 1000000
            if Decimal(row["reserved_usd"]) != expected or (
                self.limit is not None and self.spent + self._reserved() + expected > self.limit
            ):
                raise PermissionError("Reservation exceeds its frozen allowance")
            self.pending[ticket] = row
        elif event in {"settled", "unknown"}:
            reservation = self.pending.get(row["ticket"])
            if (
                type(row["ticket"]) is not int
                or reservation is None
                or any(
                    row.get(key) != reservation.get(key)
                    for key in ("phase", "role", "request_model", "attribution")
                )
            ):
                raise PermissionError("Receipt has no matching pending dispatch")
            if event == "unknown":
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", row["error_type"]):
                    raise PermissionError("Unknown outcome must not contain exception text")
                self.unknowns.add(row["ticket"])
                self.blocked = True
            else:
                if row["ticket"] in self.unknowns:
                    raise PermissionError("Unsettled requests require independent reconciliation")
                usage = self._usage(
                    reservation,
                    {"model": row["response_model"], "usage": row["usage"]},
                    row["request_id"],
                )
                incoming, outgoing, cost = self.usage_cost(reservation["request_model"], usage)
                if (
                    Decimal(row["cost_usd"]) != cost
                    or cost > Decimal(reservation["reserved_usd"])
                    or row.get("input_usd_per_million") != str(incoming)
                    or row.get("output_usd_per_million") != str(outgoing)
                ):
                    raise PermissionError("Receipt price differs from frozen standard usage")
                self.request_ids.add(row["request_id"])
                del self.pending[row["ticket"]]
                self.spent += cost
        else:
            raise PermissionError("Unknown spending journal event")
        self.rows.append(row)

    def _append(self, event, **fields):
        if self.closed or self.poisoned:
            raise PermissionError("Spending journal is closed or failed durability")
        row = {
            "sequence": len(self.rows),
            "event": event,
            "phase": self._phase,
            "model": self.model,
            "native_model": self.native_model,
            "limit_usd": None if self.limit is None else str(self.limit),
            **fields,
        }
        try:
            self.file.write(
                json.dumps({**row, "sha256": digest(row)}, sort_keys=True, allow_nan=False) + "\n"
            )
            self.file.flush()
            os.fsync(self.file.fileno())
            self._apply(row)
        except BaseException:
            self.blocked = self.poisoned = True
            raise

    def _reserved(self):
        return sum((Decimal(row["reserved_usd"]) for row in self.pending.values()), Decimal(0))

    def reserve(self, payload: dict, *, role: str, attribution=None, raw_input_bytes=None) -> int:
        with self.lock:
            output = payload.get("max_tokens") if isinstance(payload, dict) else None
            if (
                self.closed
                or self.blocked
                or self.unknowns
                or role not in {"solver", "judge", "loom"}
                or not isinstance(payload, dict)
                or payload.get("model") != (self.native_model if role == "loom" else self.model)
                or type(output) is not int
                or not 0 < output <= 128000
                or payload.get("stream") not in (None, False)
            ):
                raise PermissionError("Unsettled billing or unpriced inference request")
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            size = len(encoded) if raw_input_bytes is None else raw_input_bytes
            if type(size) is not int or not 0 < size <= 8 * 1024 * 1024:
                raise ValueError("Invalid raw request byte bound")
            attribution = dict(attribution or {})
            if set(attribution) - ATTRIBUTION or any(
                not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", value)
                for value in attribution.values()
            ):
                raise ValueError("Only bounded controller attribution is recorded")
            incoming = size + 4096
            rates = self.rates(payload["model"], incoming, ceiling=True)
            ceiling = (incoming * rates[0] * 2 + output * rates[1]) / 1000000
            if self.limit is not None and self.spent + self._reserved() + ceiling > self.limit:
                raise PermissionError("Provider allowance cannot cover this request")
            ticket = len(self.rows)
            self._append(
                "reserved",
                ticket=ticket,
                role=role,
                request_sha256=digest(payload),
                request_model=payload["model"],
                input_bytes=size,
                input_token_bound=incoming,
                output_token_bound=output,
                reserved_usd=str(ceiling),
                attribution=attribution,
            )
            return ticket

    def settle(self, ticket: int, response: dict, request_id: str):
        with self.lock:
            if self.closed or ticket not in self.pending or ticket in self.unknowns:
                raise PermissionError("Receipt has no open reservation")
            reservation = self.pending[ticket]
            try:
                usage = self._usage(reservation, response, request_id)
                incoming, outgoing, cost = self.usage_cost(reservation["request_model"], usage)
                if cost > Decimal(reservation["reserved_usd"]):
                    raise ValueError("Actual provider cost exceeds its reservation")
            except (KeyError, ValueError, TypeError) as error:
                self.unknown(ticket, type(error).__name__)
                raise
            self._append(
                "settled",
                ticket=ticket,
                request_id=request_id,
                usage=usage,
                cost_usd=str(cost),
                role=reservation["role"],
                phase=reservation["phase"],
                attribution=reservation["attribution"],
                request_model=reservation["request_model"],
                response_model=response["model"],
                input_usd_per_million=str(incoming),
                output_usd_per_million=str(outgoing),
            )
            return cost

    def unknown(self, ticket: int, error: str):
        with self.lock:
            self.blocked = True
            if self.closed or ticket not in self.pending:
                raise PermissionError("Unknown outcome has no open reservation")
            if ticket in self.unknowns:
                return
            reservation = self.pending[ticket]
            category = (
                error
                if isinstance(error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error)
                else "UnknownFailure"
            )
            self._append(
                "unknown",
                ticket=ticket,
                error_type=category,
                role=reservation["role"],
                phase=reservation["phase"],
                request_model=reservation["request_model"],
                attribution=reservation["attribution"],
            )

    def summary(self):
        with self.lock:
            settled = {row["ticket"]: row for row in self.rows if row["event"] == "settled"}
            groups = {name: defaultdict(list) for name in ("by_phase", "by_role", "by_model")}
            reservations = [row for row in self.rows if row["event"] == "reserved"]

            def total(rows):
                receipts = [settled[row["ticket"]] for row in rows if row["ticket"] in settled]
                pending = [row for row in rows if row["ticket"] in self.pending]
                return dict(
                    requests=len(rows),
                    known_requests=len(receipts),
                    pending_requests=len(pending),
                    spent_usd=float(
                        sum((Decimal(row["cost_usd"]) for row in receipts), Decimal(0))
                    ),
                    pending_reserved_usd=float(
                        sum((Decimal(row["reserved_usd"]) for row in pending), Decimal(0))
                    ),
                    input_tokens=sum(
                        row["usage"][key]
                        for row in receipts
                        for key in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    ),
                    cache_read_input_tokens=sum(
                        row["usage"]["cache_read_input_tokens"] for row in receipts
                    ),
                    cache_creation_input_tokens=sum(
                        row["usage"]["cache_creation_input_tokens"] for row in receipts
                    ),
                    output_tokens=sum(row["usage"]["output_tokens"] for row in receipts),
                )

            for row in reservations:
                for group, key in (
                    ("by_phase", "phase"),
                    ("by_role", "role"),
                    ("by_model", "request_model"),
                ):
                    groups[group][row[key]].append(row)
            return {
                **total(reservations),
                "limit_usd": None if self.limit is None else float(self.limit),
                "remaining_usd": (
                    None
                    if self.limit is None
                    else float(self.limit - self.spent - self._reserved())
                ),
                "blocked": self.blocked,
                "complete": not self.pending and not self.blocked,
                "unattributed_requests": sum(
                    row["role"] == "loom"
                    and any(
                        row["attribution"].get(key) in {None, "-", "*", "unknown"}
                        for key in ("org", "namespace")
                    )
                    for row in reservations
                ),
                "price_basis": PRICE_BASIS,
                **{
                    name: {key: total(rows) for key, rows in sorted(values.items())}
                    for name, values in groups.items()
                },
            }

    def close(self):
        with self.lock:
            if not self.closed:
                self.blocked |= bool(self.pending)
                self.closed = True
                self.file.close()

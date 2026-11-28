"""Finite JSONEachRow reads with limits enforced before decoding rows."""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any

from server.core.ch import own_write_reads, set_query_tag, statement_timeout


class BoundedReadError(ValueError):
    """The complete answer cannot be established within its read allowance."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BoundedReadError("Duplicate result field")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise BoundedReadError("Nonfinite result value")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise BoundedReadError("Nonfinite result value")
    return result


def decode_json(text: str) -> Any:
    """Decode already byte-bounded JSON without duplicate keys or nonfinite numbers."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_float,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise BoundedReadError("Malformed JSON value") from error


def _row(line: bytes) -> dict[str, Any]:
    try:
        row = decode_json(line.decode("utf-8"))
    except UnicodeError as error:
        raise BoundedReadError("Result row is not UTF-8") from error
    if not isinstance(row, dict):
        raise BoundedReadError("Result row is not an object")
    return row


async def read_rows(
    sql: str,
    params: dict[str, Any],
    *,
    ch: Any,
    deadline: float,
    row_bytes: int,
    total_bytes: int,
    max_rows: int,
    tag: str,
    max_read_rows: int = 1_000_000,
    max_read_bytes: int = 128 * 1024**2,
) -> list[dict[str, Any]]:
    """Read a caller's fixed SELECT with its explicit LIMIT and JSONEachRow format."""
    remaining = min(2.0, deadline - time.monotonic())
    if remaining <= 0 or min(row_bytes, total_bytes, max_rows, max_read_rows, max_read_bytes) <= 0:
        raise BoundedReadError("Read allowance exhausted")
    limits = {
        "max_execution_time": remaining,
        "max_memory_usage": 128 * 1024**2,
        "max_rows_to_read": min(max_read_rows, 1_000_000),
        "max_bytes_to_read": min(max_read_bytes, 128 * 1024**2),
        "max_result_rows": max_rows + 1,
        "max_result_bytes": total_bytes + 1,
        "read_overflow_mode": "throw",
        "result_overflow_mode": "throw",
        "timeout_overflow_mode": "throw",
        "output_format_json_quote_64bit_integers": 0,
    }
    rows: list[dict[str, Any]] = []
    pending = bytearray()
    consumed = 0
    set_query_tag(tag)
    async with asyncio.timeout(remaining):
        with own_write_reads(), statement_timeout(remaining):
            async with ch.raw_stream(sql, params=params, settings=limits) as chunks:
                async for chunk in chunks:
                    consumed += len(chunk)
                    if consumed > total_bytes:
                        raise BoundedReadError("Result exceeds its byte bound")
                    pending.extend(chunk)
                    while (end := pending.find(b"\n")) >= 0:
                        if end > row_bytes or len(rows) >= max_rows:
                            raise BoundedReadError("Result exceeds its row bound")
                        line = bytes(pending[:end])
                        del pending[: end + 1]
                        rows.append(_row(line))
                    if len(pending) > row_bytes:
                        raise BoundedReadError("Row exceeds its byte bound")
    if pending:
        raise BoundedReadError("Incomplete result row")
    return rows

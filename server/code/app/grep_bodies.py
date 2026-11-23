"""Optional enclosing bodies for grep hits, using the structural reader."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.code.api.models_grep import CodeGrepInput
from server.code.api.models_navigation import CodeEnclosingInput
from server.code.app.spans import code_span
from server.code.domain.deadline import Budget
from server.code.domain.units import estimate_tokens

logger = logging.getLogger(__name__)


async def definitions_for(
    input: CodeGrepInput, matches: list[dict[str, Any]], budget: Budget | None
) -> dict[str, Any]:
    if not input.include_body or not matches:
        return {}
    from server.code.app.symbol.enclosing import (
        _ENCLOSING_BODY_MAX_SYMBOLS,
        handle_code_enclosing,
    )

    locations = list(dict.fromkeys(f"{row['path']}:{row['line']}" for row in matches))
    rows: list[dict[str, Any]] = []

    async def read() -> None:
        # Serve every matched location without extending the structural reader's
        # per-call body bound to this whole response.
        for start in range(0, len(locations), _ENCLOSING_BODY_MAX_SYMBOLS):
            out = await handle_code_enclosing(
                CodeEnclosingInput(
                    org=input.org,
                    namespace=input.namespace,
                    agent=input.agent,
                    user_id=input.user_id,
                    consumer=input.consumer,
                    locations=locations[start : start + _ENCLOSING_BODY_MAX_SYMBOLS],
                    include_body=True,
                )
            )
            rows.extend(out.results)

    incomplete, note = False, ""
    try:
        with code_span("grep.bodies"):
            if budget is None:
                await read()
            elif budget.left_ms() <= 0:
                incomplete, note = True, "enclosing bodies skipped at the request deadline"
            else:
                async with asyncio.timeout(budget.left_ms() / 1000):
                    await read()
    except TimeoutError:
        incomplete, note = True, "enclosing body lookup timed out"
    except Exception:  # Matching lines remain useful when a body read fails.
        logger.warning("code.grep enclosing body lookup failed", exc_info=True)
        incomplete, note = True, "some enclosing bodies could not be read"
    definitions = _distinct(rows)
    if any(not row.get("body") for row in definitions) or len(rows) != len(locations):
        incomplete = True
        note = (
            note
            or "some hits have no retained enclosing body; inspect definitions or use code.symbol"
        )
    return {"definitions": definitions, "definitions_partial": incomplete, "definitions_note": note}


def _distinct(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key: tuple[Any, ...] = (
            row.get("path"),
            row.get("symbol_id"),
            row.get("start_line"),
            row.get("end_line"),
        )
        if not row.get("symbol_id"):
            key += (row.get("line"),)
        if key not in grouped:
            grouped[key] = {**row, "lines": []}
            grouped[key].pop("line", None)
        elif not grouped[key].get("body") and row.get("body"):
            grouped[key] = {**row, "lines": grouped[key]["lines"]}
            grouped[key].pop("line", None)
        grouped[key]["lines"].append(row["line"])
    return list(grouped.values())


def definitions_tokens(fields: dict[str, Any]) -> int:
    return sum(estimate_tokens(row.get("body") or "") for row in fields.get("definitions", []))

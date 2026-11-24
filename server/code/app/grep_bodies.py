"""Optional enclosing bodies for grep hits, using the structural reader."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.code.api.models_grep import CodeGrepInput
from server.code.api.models_navigation import CodeEnclosingInput
from server.code.app.grep_source import GREP_MATCH_TEXT_CHARS, match_revisions
from server.code.app.spans import code_span
from server.code.domain.deadline import Budget
from server.code.domain.units import estimate_tokens

logger = logging.getLogger(__name__)


async def definitions_for(
    input: CodeGrepInput,
    matches: list[dict[str, Any]],
    budget: Budget | None,
    *,
    match_commits: dict[tuple[str, int], str],
) -> dict[str, Any]:
    if not input.include_body or not matches:
        return {}
    from server.code.app.symbol.enclosing import (
        _ENCLOSING_BODY_MAX_SYMBOLS,
        handle_code_enclosing,
    )

    revisions = match_revisions(matches, match_commits)
    hits = {(row["path"], row["line"]): row for row in matches}
    locations = [f"{path}:{line}" for path, line in hits if path in revisions]
    if not locations:
        return {
            "definitions": [],
            "definitions_partial": True,
            "definitions_note": "no verified match revision for enclosing bodies",
        }
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
                ),
                body_revisions=revisions,
            )
            rows.extend(
                row
                for row in out.results
                if (hit := hits.get((row["path"], row["line"]))) is not None
                and _agrees_with_hit(row, hit)
            )

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
    if len(rows) != len(hits) or any(
        not row.get("body")
        or any(not row["start_line"] <= line <= row["end_line"] for line in row["lines"])
        for row in definitions
    ):
        incomplete = True
        note = (
            note
            or "some hits have no retained enclosing body; inspect definitions or use code.symbol"
        )
    return {"definitions": definitions, "definitions_partial": incomplete, "definitions_note": note}


def _agrees_with_hit(row: dict[str, Any], hit: dict[str, Any]) -> bool:
    """A returned body must contain the retained match at its exact line."""
    body = str(row.get("body") or "")
    if not body:
        return True
    index = int(hit["line"]) - int(row["start_line"])
    lines = body.split("\n")
    return 0 <= index < len(lines) and lines[index].strip()[:GREP_MATCH_TEXT_CHARS] == hit["text"]


def _distinct(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key: tuple[Any, ...] = (row.get("path"), row.get("symbol_id"))
        if not row.get("symbol_id"):
            key += (row.get("line"),)
        if key not in grouped:
            grouped[key] = {**row, "lines": []}
            grouped[key].pop("line", None)
        elif row.get("body") and (
            not grouped[key].get("body")
            or (
                row["start_line"] <= grouped[key]["start_line"]
                and row["end_line"] >= grouped[key]["end_line"]
            )
        ):
            grouped[key] = {**row, "lines": grouped[key]["lines"]}
            grouped[key].pop("line", None)
        grouped[key]["lines"].append(row["line"])
    return list(grouped.values())


def definitions_tokens(fields: dict[str, Any]) -> int:
    return sum(estimate_tokens(row.get("body") or "") for row in fields.get("definitions", []))

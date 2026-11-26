"""Lessons for forwarding callers whose source is part of an edit lookup."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from server.code.app import enrich
from server.code.app.spans import code_span, span_attrs
from server.identity.app.request_scope import request_narrowed
from server.memory.app.code_plane_reads import fit_symbol_lessons
from server.settings import settings

if TYPE_CHECKING:
    from server.code.api.models_symbol import CodeEditContextInput, CodeSymbolInput
    from server.code.domain.units import CodeUnit


def _verified_callers(definitions: list[CodeUnit]) -> dict[tuple[str, str, str], CodeUnit]:
    callers: dict[tuple[str, str, str], CodeUnit] = {}
    for unit in definitions:
        for example in unit.examples:
            if example.get("evidence") != "indirect_call":
                continue
            via = example.get("via") or {}
            context = via.get("context")
            if context and context.get("text") and context.get("commit"):
                callers.setdefault((via["path"], via["symbol"], context["commit"]), unit)
    return callers


def _key(note: dict[str, Any]) -> tuple[str, str, str]:
    return note["namespace"], note["memory_key"], note["excerpt"]


def _attach(
    unit: CodeUnit,
    caller: str,
    notes: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
) -> int:
    proposals = {
        _key(note): {**note, "linked_symbols": [caller]}
        for note in notes
        if note.get("code_state") == "fresh" and _key(note) not in seen
    }
    fitted = fit_symbol_lessons([*unit.lessons, *proposals.values()])
    primary = len(unit.lessons)
    if fitted[:primary] != unit.lessons:
        return 0
    kept = fitted[primary:]
    unit.lessons.extend(kept)
    seen.update(map(_key, kept))
    return len(kept)


async def attach_verified_caller_lessons(
    input: CodeEditContextInput | CodeSymbolInput,
    org: str,
    ns: str,
    definitions: list[CodeUnit],
    *,
    started: float,
) -> None:
    """Use the remaining enrichment time and add lessons beside the verified caller."""
    if not input.include_lessons or request_narrowed():
        return
    callers = _verified_callers(definitions)
    remaining = settings.code_enrich_timeout_s - (time.monotonic() - started)
    if not callers or remaining <= 0:
        return
    seen = {_key(note) for unit in definitions for note in unit.lessons}
    with code_span("enrich.indirect_caller_lessons", seeds=len(callers)) as sp:
        results = await asyncio.gather(
            *(
                enrich.lessons_for_symbol(
                    org,
                    ns,
                    symbol_name=caller,
                    paths=[path],
                    extra_namespaces=input.lesson_namespaces,
                    fresh_at_commit=revision,
                    seconds=remaining,
                )
                for path, caller, revision in callers
            )
        )
        attached = 0
        for ((_, caller, _), unit), notes in zip(callers.items(), results, strict=True):
            if notes:
                attached += _attach(unit, caller, notes, seen)
        span_attrs(sp, results=attached)

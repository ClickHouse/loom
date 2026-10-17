"""Record answers that need no synthesis: PR/issue numbers from the GitHub
items index, with the tracker record beside them."""

from __future__ import annotations

import logging
from typing import Any, cast

from server.memory.app.ask.result import MemoryAskResult

logger = logging.getLogger(__name__)


async def _append_tracker_facts(
    lines: list[str],
    keys: list[str],
    code_org: str,
    code_ns: str,
    numbers: list[int],
) -> None:
    """The tracker record beside the PR record — component,
    severity, bisect verdict, test presence, fixes / backports — from the QA
    namespace bound to the code namespace, as the current principal.
    Additive: an unbound or unauthorized binding adds nothing."""
    try:
        from server.tracker.app.binding import authorized_tracker_namespace
        from server.tracker.app.facts import tracker_fact_line, tracker_facts_for_numbers

        tracker_ns = await authorized_tracker_namespace(code_org, code_ns)
        facts = await tracker_facts_for_numbers(code_org, tracker_ns, numbers) if tracker_ns else {}
        for num in numbers:
            line = tracker_fact_line(num, facts.get(num) or {})
            if line:
                lines.append(line)
                keys.append(f"tracker_item:{tracker_ns}:{num}")
    except Exception as e:  # noqa: BLE001 — additive
        logger.debug("memory.ask qa facts skipped: %s", e)


async def _gh_items_fallback_answer(
    question: str,
    search_input: Any,
    settings: Any,
    results: list[Any],
    embedding_model: str,
    override: bool | None,
) -> MemoryAskResult | None:
    """When retrieval abstained on a question that names PR/issue numbers,
    resolve them in loom.code_gh_items (the bound code namespace) and answer
    from the items — kind, opener, open date, state as of the question's
    anchor — as a `record` answer keyed `gh_item:<ns>:<n>`. None when the
    call turns the fallback off, the question names no number, no code namespace is
    bound, or the table has no row (absence is UNKNOWN: the sync cursor can
    lag, so an unknown number still abstains rather than asserting "no such
    PR"). Never raises: a lookup failure is an abstention."""
    if override is not None and not override:
        return None
    from server.memory.domain.retrieval_calibration import _salient_identifiers

    numbers = [
        int(i) for i in _salient_identifiers(question or "") if i.isdigit() and 3 <= len(i) <= 7
    ]
    if not numbers:
        # No number, but a test, a release or a component —
        # the tracker record answers before synthesis, or nothing does.
        from server.tracker.app.ask_route import qa_route_answer

        routed = await qa_route_answer(question, search_input, settings, results, embedding_model)
        return cast(MemoryAskResult | None, routed)
    try:
        from server.memory.app.code_grounding import (
            _resolve_code_target,
            describe_item,
            describe_pr,
        )

        code_org, code_ns = _resolve_code_target(search_input, settings)
        if not code_ns:
            return None
        from server.code.app.reads import code_index

        repo = code_index()
        items = await repo.items_by_number(code_org, code_ns, numbers)
        # The merged-PR plane knows merge date and linked
        # issues the items table does not; a number found there answers from
        # that record first.
        try:
            prs = await repo.prs_by_number(code_org, code_ns, numbers)
        except Exception as e:  # noqa: BLE001 — items alone still answer
            logger.warning("memory.ask pr-history fallback failed (open): %s", e)
            prs = {}
    except Exception as e:  # noqa: BLE001 — the fallback never blocks an abstention
        logger.warning("memory.ask gh-items fallback failed (open): %s", e)
        return None
    from server.memory.domain.temporal_reads import reference_time

    as_of = getattr(search_input, "as_of", None) or reference_time(search_input)
    as_of_date = as_of.date() if as_of is not None else None
    lines: list[str] = []
    keys: list[str] = []
    for num in numbers:
        pr = (prs or {}).get(num)
        if pr:
            text = describe_pr(num, pr, as_of_date)
            if text:
                lines.append(text)
                keys.append(f"code_pr:{code_ns}:{num}")
                continue
        row = items.get(num)
        if not row:
            continue
        text = describe_item(num, row, as_of_date)
        if not text:
            continue
        title = str(row.get("title") or "").strip()
        if title:
            text = f"{text} Title: {title}."
        lines.append(text)
        keys.append(f"gh_item:{code_ns}:{num}")
    if not lines:
        return None
    await _append_tracker_facts(lines, keys, code_org, code_ns, numbers)
    return MemoryAskResult(
        answer="\n".join(lines),
        source="record",
        abstain=False,
        abstain_reason="",
        memory_keys=keys,
        results=results,
        embedding_model=embedding_model,
    )

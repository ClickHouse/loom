"""memory.import: items another memory system distilled, written through
`handle_memory_bulk_set` under keys derived from their text. What each entry answers is in
docs/features/memory/writing.md#importing-from-another-memory-system.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import NamedTuple

import server.memory.store as memory_repo
from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_value
from server.identity.app.request_scope import request_scope
from server.memory.api.models_bulk import (
    BLACKLISTED,
    LOCKED,
    PINNED,
    REFUSED,
    REJECTED,
    UNCHECKED,
    MemoryBulkSetInput,
    _BulkSetEntry,
    _BulkSetResult,
)
from server.memory.api.models_import import (
    MemoryImportInput,
    MemoryImportOutput,
    _MemoryImportEntry,
    _MemoryImportResult,
    _MemoryImportTotals,
)
from server.memory.app.bulk_set import handle_memory_bulk_set
from server.memory.app.provenance import writer_actor
from server.memory.domain.import_keys import import_key
from server.memory.domain.memory_import import (
    Outcome,
    Reason,
    category_note,
    repeat_outcome,
    skip_reasons,
)
from server.memory.store import categories
from server.observability import audit

log = logging.getLogger(__name__)

# The check behind each verdict of `models_bulk.REFUSED`, the entries a bulk write refuses.
_CHECK_OF: dict[str, Reason] = {
    BLACKLISTED: "blacklist",
    UNCHECKED: "blacklist_unchecked",
    PINNED: "pinned",
    LOCKED: "locked",
    REJECTED: "pinned_peer",
}
# The reason of a written entry whose verdict is not a plain write.
_STORED_AS: dict[str, Reason] = {
    "duplicate": "duplicate",
    "conflict": "conflicted",
    "refines": "refines",
}


class _Answer(NamedTuple):
    """An entry's outcome, its reason and, for an identity_duplicate, the key holding it."""

    outcome: Outcome
    reason: Reason
    held_by: str = ""


async def handle_memory_import(input: MemoryImportInput) -> MemoryImportOutput:
    """Write the first entry of each text through memory.bulk_set (a later one shares
    its key), unless its key's newest version leaves it as it is, then link the
    registered categories of the entries whose key holds the item. Answers each entry's
    outcome and the totals."""
    keys = [import_key(entry.value) for entry in input.entries]
    first: dict[str, int] = {}
    for i, key in enumerate(keys):
        first.setdefault(key, i)
    scope = request_scope()
    # A token bounded below the namespace links no category: a link has no user or session
    # axis, so it would reach every row under the key (memory.assign_category refuses it too).
    linkable = not scope.narrowed
    named = [entry.category for entry in input.entries if entry.category] if linkable else []
    # Every read runs before the write; only the new links' insert runs after it. If that
    # insert fails, a retry finds the rows unchanged and adds the links.
    category_ids = await categories.registered_ids(input.org, input.namespace, named)
    categorized = [
        k for k, e in zip(keys, input.entries, strict=True) if e.category in category_ids
    ]
    stored_links = await categories.link_states(input.org, input.namespace, categorized)
    heads = await memory_repo.key_heads_across_sessions(
        input.org, input.namespace, input.agent, input.user_id, list(first), scope=scope
    )
    content_shas = {key: sha256_value(input.entries[i].value) for key, i in first.items()}
    sensitivities = {key: input.entries[i].sensitivity for key, i in first.items()}
    skip = skip_reasons(heads, content_shas, sensitivities)
    unknown = [key for key, reason in skip.items() if reason == "unknown"]
    if unknown:
        log.warning(
            "memory.import: %d key(s) have an unknown head status, skipped: %s",
            len(unknown),
            unknown[:5],
        )
    answers = await _write(input, keys, first, skip)
    results: list[_MemoryImportResult] = []
    for i, key in enumerate(keys):
        outcome, reason, held_by = answers[key]
        if first[key] != i:
            outcome, reason = repeat_outcome(outcome, reason)
        results.append(
            _MemoryImportResult(index=i, key=key, outcome=outcome, reason=reason, held_by=held_by)
        )
    await _categorize(input, results, category_ids, stored_links, linkable=linkable)
    return _output(input, results)


async def _write(
    input: MemoryImportInput,
    keys: list[str],
    first: dict[str, int],
    skip: dict[str, Reason],
) -> dict[str, _Answer]:
    """Key → the answer of the first entry of its text: skipped with its `skip` reason,
    or written through memory.bulk_set."""
    answers: dict[str, _Answer] = {}
    pending: list[int] = []
    for key, i in first.items():
        reason = skip.get(key, "")
        if reason:
            answers[key] = _Answer("skipped", reason)
        else:
            pending.append(i)
    if not pending:
        return answers
    # The source exports in any order, and under newest_wins the later of two statements
    # of one attribute in a batch retires the earlier: oldest first, an undated item
    # counting as said now.
    now = naive_utc_now()
    pending.sort(key=lambda i: input.entries[i].valid_at or now)  # stable
    bulk = await handle_memory_bulk_set(
        _bulk_input(input, [(input.entries[i], keys[i]) for i in pending]),
        dry_run=input.dry_run,
        # Every entry is checked, whatever bulk_conflict_check_max_entries caps a bulk at.
        conflict_check_cap=len(pending),
        # A re-run must not reinforce the same rows again, so no run reinforces any.
        reinforce=False,
    )
    for i, result in zip(pending, bulk.written, strict=True):
        key = keys[i]
        answers[key] = _entry_outcome(key, result, bulk.identity_duplicates.get(key))
    return answers


def _entry_outcome(key: str, result: _BulkSetResult, held_by: str | None) -> _Answer:
    """The answer of an entry the bulk write answered. A result with no version that no
    check refused, no merge took and no other key holds is the skip gate's or identity
    dedup's answer for this key: the key holds the text already."""
    verdict = result.conflict_verdict
    if result.version:
        return _Answer("written", _STORED_AS.get(verdict, ""))
    if verdict in REFUSED:
        return _Answer("refused", _CHECK_OF[verdict])
    if verdict == "merge":
        return _Answer("held", "merge")
    if held_by is not None and held_by != key:
        return _Answer("held", "identity_duplicate", held_by)
    return _Answer("skipped", "unchanged")


def _bulk_input(
    input: MemoryImportInput, entries: list[tuple[_MemoryImportEntry, str]]
) -> MemoryBulkSetInput:
    """The bulk write of `entries`, each under its key, in the caller's identity."""
    return MemoryBulkSetInput(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        run_id=input.run_id,
        # The source already distilled these facts; the conflict verdict judges updates.
        detect_updates=False,
        entries=[
            _BulkSetEntry(
                memory_key=key,
                value=entry.value,
                tags=entry.tags,
                valid_at=entry.valid_at,
                # An item that names no source is traced by its key, `import:<digest>`.
                source_trace=entry.source_trace or key,
                # Copied from another system: a declared downgrade, never `asserted`.
                source_class="imported",
                sensitivity=entry.sensitivity,
            )
            for entry, key in entries
        ],
    )


async def _categorize(
    input: MemoryImportInput,
    results: list[_MemoryImportResult],
    category_ids: dict[str, str],
    stored: dict[tuple[str, str], bool],
    *,
    linkable: bool,
) -> None:
    """Note each entry's category (`category_note`) and link each key with no note and no
    stored link; the new links get one audit row of keys and category names, never the
    text."""
    links: set[tuple[str, str]] = set()
    for result, entry in zip(results, input.entries, strict=True):
        if not entry.category:
            continue
        link = (result.key, category_ids.get(entry.category, ""))
        note = category_note(
            result.outcome,
            result.reason,
            linkable=linkable,
            registered=entry.category in category_ids,
            stored_live=stored.get(link),
        )
        result.note = note
        if not note and link not in stored:
            links.add(link)
    new = sorted(links)
    if not new or input.dry_run:
        return
    await categories.link(input.org, input.namespace, new)
    names = {cid: name for name, cid in category_ids.items()}
    await audit.emit(
        "memory.import",
        org=input.org,
        namespace=input.namespace,
        actor=writer_actor("-"),
        subject={
            "count": len(new),
            "category_links": [{"memory_key": k, "category": names[c]} for k, c in new],
        },
    )


def _output(input: MemoryImportInput, results: list[_MemoryImportResult]) -> MemoryImportOutput:
    """The response: the totals and each entry's result."""
    counts = Counter(r.outcome for r in results)
    return MemoryImportOutput(
        totals=_MemoryImportTotals(
            written=counts["written"],
            held=counts["held"],
            skipped=counts["skipped"],
            refused=counts["refused"],
        ),
        results=results,
        dry_run=input.dry_run,
    )

"""memory.import: items another memory system distilled, written through
`handle_memory_bulk_set` under keys their text derives. What each entry answers is in
docs/features/memory/writing.md#importing-from-another-memory-system.
"""

from __future__ import annotations

import logging
from collections import Counter

import server.memory.store as memory_repo
from server.core.datetimes import naive_utc_now, to_naive_utc
from server.core.hashing import sha256_value
from server.identity.app.request_scope import request_scope
from server.memory.api.models_bulk import (
    BLACKLISTED,
    LOCKED,
    PINNED,
    REFUSED,
    REJECTED,
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
    holds_item,
    repeat_outcome,
    skip_reasons,
)
from server.memory.store import categories
from server.observability import audit

log = logging.getLogger(__name__)

# The check behind each verdict of `models_bulk.REFUSED`, the entries a bulk write refuses.
_CHECK_OF: dict[str, Reason] = {
    BLACKLISTED: "blacklist",
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
    outcomes, keepers = await _write(input, keys, first, skip_reasons(heads, content_shas))
    results: list[_MemoryImportResult] = []
    for i, key in enumerate(keys):
        outcome, reason = outcomes[key] if first[key] == i else repeat_outcome(outcomes[key])
        held_by = keepers.get(key, "") if reason == "identity_duplicate" else ""
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
) -> tuple[dict[str, tuple[Outcome, Reason]], dict[str, str]]:
    """Key → the outcome of the first entry of its text: skipped with its `skip` reason,
    or written through memory.bulk_set. Also key → the key identity dedup found holding
    the text."""
    outcomes: dict[str, tuple[Outcome, Reason]] = {}
    pending: list[int] = []
    for key, i in first.items():
        reason = skip.get(key, "")
        if reason:
            outcomes[key] = ("skipped", reason)
        else:
            pending.append(i)
    unknown = [key for key, reason in skip.items() if reason == "unknown"]
    if unknown:
        log.warning(
            "memory.import: %d key(s) have an unknown head status, skipped: %s",
            len(unknown),
            unknown[:5],
        )
    if not pending:
        return outcomes, {}
    # The source exports in any order, and under newest_wins the later of two statements
    # of one attribute in a batch retires the earlier: oldest first, an undated item
    # counting as said now.
    now = naive_utc_now()
    pending.sort(key=lambda i: (to_naive_utc(input.entries[i].valid_at) or now, i))
    written = await handle_memory_bulk_set(
        _bulk_input(input, [(input.entries[i], keys[i]) for i in pending]),
        dry_run=input.dry_run,
        # Every entry is checked, whatever bulk_conflict_check_max_entries caps a bulk at.
        conflict_check_cap=len(pending),
        # A re-run must not reinforce the same rows again, so no run reinforces any.
        reinforce=False,
    )
    for i, result in zip(pending, written.written, strict=True):
        key = keys[i]
        outcomes[key] = _entry_outcome(key, result, written.identity_duplicates.get(key))
    return outcomes, written.identity_duplicates


def _entry_outcome(key: str, result: _BulkSetResult, held_by: str | None) -> tuple[Outcome, Reason]:
    """The outcome of an entry the bulk write answered. A result with no version that no
    check refused, no merge took and no other key holds is the skip gate's or identity
    dedup's answer for this key: the key holds the text already."""
    verdict = result.conflict_verdict
    if result.version:
        return "written", _STORED_AS.get(verdict, "")
    if verdict in REFUSED:
        return "refused", _CHECK_OF[verdict]
    if verdict == "merge":
        return "held", "merge"
    if held_by is not None and held_by != key:
        return "held", "identity_duplicate"
    return "skipped", "unchanged"


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
                valid_at=to_naive_utc(entry.valid_at),
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
    """Note each entry's category, and link a registered one to the entry's key once the
    key holds the item. A link already `stored` (live or not) is left as it is, so a re-run
    never brings back one the owner rescinded. The new links get one audit row: keys and
    category names, never the text."""
    links: set[tuple[str, str]] = set()
    for result, entry in zip(results, input.entries, strict=True):
        if not entry.category:
            continue
        if not linkable:
            result.note = "category_refused"
        elif entry.category not in category_ids:
            result.note = "category_unregistered"
        elif not holds_item(result.outcome, result.reason):
            result.note = "category_not_linked"
        else:
            link = (result.key, category_ids[entry.category])
            if link not in stored:
                links.add(link)
            elif not stored[link]:
                result.note = "category_rescinded"
    new = sorted(links)
    if not new or input.dry_run:
        return
    await categories.link(input.org, input.namespace, new)
    names = {cid: name for name, cid in category_ids.items()}
    await audit.emit(
        "memory.import",
        org=input.org,
        namespace=input.namespace,
        actor=writer_actor() or "-",
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

"""memory.import: memory items another memory system distilled, written through the
bulk write path.

Each entry becomes a memory.bulk_set entry under the key its text derives
(`server.memory.domain.memory_import.import_key`), declared `source_class=imported`,
so an import makes every check a bulk write makes: the identity's content-hash dedup,
the skip gate, the lock and pin guards, the blacklist, the sensitivity rule, inline
embedding and the conflict verdicts; and it leaves the bulk write's hash-only audit
row. A dry run makes the same checks and writes nothing. A category the namespace has
registered is linked to the entry's key as memory.assign_category links one
(`server.memory.store.categories`).
"""

from __future__ import annotations

from collections import Counter

from server.core.datetimes import to_naive_utc
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.api.models_import import (
    MAX_IMPORT_ENTRIES,
    MemoryImportInput,
    MemoryImportOutput,
    _ImportEntry,
    _ImportEntryResult,
    _ImportTotals,
)
from server.memory.app.bulk_set import handle_memory_bulk_set
from server.memory.app.provenance import writer_actor
from server.memory.domain.memory_import import (
    CATEGORY_UNREGISTERED,
    REPEATED,
    entry_outcome,
    holds_item,
    import_key,
)
from server.memory.store import categories
from server.observability import audit


async def handle_memory_import(input: MemoryImportInput) -> MemoryImportOutput:
    """Write the first entry of each text through memory.bulk_set (a later one shares
    its key and is skipped), then link the registered categories of the entries whose
    key holds the item. Answers each entry's outcome and the totals."""
    keys = [import_key(entry.value) for entry in input.entries]
    first: dict[str, int] = {}
    for i, key in enumerate(keys):
        first.setdefault(key, i)
    kept = sorted(first.values())
    # Read before anything is written, so a failed read leaves nothing half done.
    category_ids = await categories.registered_ids(
        input.org, input.namespace, [entry.category for entry in input.entries if entry.category]
    )
    written = await handle_memory_bulk_set(
        _bulk_input(input, [(input.entries[i], keys[i]) for i in kept]),
        dry_run=input.dry_run,
        # Every entry is checked, whatever bulk_conflict_check_max_entries caps a bulk at.
        conflict_check_cap=MAX_IMPORT_ENTRIES,
    )
    outcomes = {
        keys[i]: entry_outcome(
            key=keys[i],
            version=result.version,
            verdict=result.conflict_verdict,
            held_by=written.identity_duplicates.get(keys[i]),
        )
        for i, result in zip(kept, written.written, strict=True)
    }
    results: list[_ImportEntryResult] = []
    for i, (entry, key) in enumerate(zip(input.entries, keys, strict=True)):
        outcome, reason = outcomes[key] if first[key] == i else REPEATED
        unregistered = bool(entry.category) and entry.category not in category_ids
        results.append(
            _ImportEntryResult(
                index=i,
                key=key,
                outcome=outcome,
                reason=reason,
                note=CATEGORY_UNREGISTERED if unregistered else "",
            )
        )
    if not input.dry_run:
        await _link_categories(input, results, category_ids)
    return _output(input, results)


def _bulk_input(
    input: MemoryImportInput, entries: list[tuple[_ImportEntry, str]]
) -> MemoryBulkSetInput:
    """The bulk write of `entries`, each under its key, in the caller's identity."""
    return MemoryBulkSetInput(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        run_id=input.run_id,
        entries=[
            _BulkSetEntry(
                memory_key=key,
                value=entry.value,
                tags=list(entry.tags),
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


async def _link_categories(
    input: MemoryImportInput, results: list[_ImportEntryResult], category_ids: dict[str, str]
) -> None:
    """Link each entry's registered category to its key once the key holds the item,
    and record the links in one audit row: keys and category names, never the text."""
    holding = {r.key for r in results if holds_item(r.outcome, r.reason)}
    links = sorted(
        {
            (result.key, entry.category)
            for result, entry in zip(results, input.entries, strict=True)
            if entry.category in category_ids and result.key in holding
        }
    )
    if not links:
        return
    await categories.link(
        input.org, input.namespace, [(key, category_ids[name]) for key, name in links]
    )
    await audit.emit(
        "memory.import",
        org=input.org,
        namespace=input.namespace,
        actor=writer_actor() or "-",
        subject={
            "count": len(links),
            "category_links": [{"memory_key": k, "category": c} for k, c in links][:200],
        },
    )


def _output(input: MemoryImportInput, results: list[_ImportEntryResult]) -> MemoryImportOutput:
    """The response: the totals and each entry's result."""
    counts = Counter(r.outcome for r in results)
    return MemoryImportOutput(
        totals=_ImportTotals(
            written=counts["written"],
            reinforced=counts["reinforced"],
            skipped=counts["skipped"],
            refused=counts["refused"],
        ),
        results=results,
        dry_run=input.dry_run,
    )

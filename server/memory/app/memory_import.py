"""memory.import: memory items another memory system distilled, written through the
bulk write path.

Each entry becomes a memory.bulk_set entry under the key its text derives
(`server.memory.domain.memory_import.import_key`), declared `source_class=imported`,
so an import makes every check a bulk write makes: the identity's content-hash dedup,
the skip gate, the lock and pin guards, the blacklist, the sensitivity rule, inline
embedding and the conflict verdicts; and it leaves the bulk write's hash-only audit
row. A key whose newest version, in any session of the identity, is a tombstone or a
superseded row is left as it is: a re-run never brings back what its owner deleted or a
newer fact retired. A dry run makes the same checks and writes nothing. A category the
namespace has registered is linked to the entry's key as memory.assign_category links
one (`server.memory.store.categories`).
"""

from __future__ import annotations

from collections import Counter

import server.memory.store as memory_repo
from server.core.datetimes import to_naive_utc
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
    CATEGORY_REFUSED,
    CATEGORY_UNREGISTERED,
    UNCHANGED,
    KeyState,
    Outcome,
    holds_item,
    import_key,
    key_states,
    repeat_outcome,
)
from server.memory.store import categories
from server.observability import audit

# The check behind each verdict of `models_bulk.REFUSED`, the entries a bulk write refuses.
_CHECK_OF: dict[str, str] = {
    BLACKLISTED: "blacklist",
    PINNED: "pinned",
    LOCKED: "locked",
    REJECTED: "conflict",
}
# The status a written entry's verdict stores its row with when it is not `active`.
_STORED_AS: dict[str, str] = {"duplicate": "duplicate", "conflict": "conflicted"}


async def handle_memory_import(input: MemoryImportInput) -> MemoryImportOutput:
    """Write the first entry of each text through memory.bulk_set (a later one shares
    its key), unless its key is deleted or superseded, then link the registered
    categories of the entries whose key holds the item. Answers each entry's outcome and
    the totals."""
    keys = [import_key(entry.value) for entry in input.entries]
    first: dict[str, int] = {}
    for i, key in enumerate(keys):
        first.setdefault(key, i)
    scope = request_scope()
    # A token bounded to one user links no category: a link has no user axis, so it would
    # reach every user's row under the key (memory.assign_category is refused such a token).
    linkable = not scope.narrowed
    named = [entry.category for entry in input.entries if entry.category] if linkable else []
    # Read before anything is written, so a failed read leaves nothing half done.
    category_ids = await categories.registered_ids(input.org, input.namespace, named)
    heads = await memory_repo.key_heads_across_sessions(
        input.org, input.namespace, input.agent, input.user_id, list(first), scope=scope
    )
    outcomes = await _write(input, keys, first, key_states(heads))
    results: list[_ImportEntryResult] = []
    for i, (entry, key) in enumerate(zip(input.entries, keys, strict=True)):
        outcome, reason = outcomes[key] if first[key] == i else repeat_outcome(outcomes[key])
        results.append(
            _ImportEntryResult(
                index=i,
                key=key,
                outcome=outcome,
                reason=reason,
                note=_category_note(entry.category, category_ids, linkable=linkable),
            )
        )
    if linkable and not input.dry_run:
        await _link_categories(input, results, category_ids)
    return _output(input, results)


def _category_note(category: str, registered: dict[str, str], *, linkable: bool) -> str:
    """The note on an entry's category: refused under a token bounded to one user,
    unregistered when the namespace holds no such category, else none."""
    if not category:
        return ""
    if not linkable:
        return CATEGORY_REFUSED
    return "" if category in registered else CATEGORY_UNREGISTERED


async def _write(
    input: MemoryImportInput,
    keys: list[str],
    first: dict[str, int],
    states: dict[str, KeyState],
) -> dict[str, tuple[Outcome, str]]:
    """Key → the outcome of the first entry of its text. A key whose newest version is a
    tombstone or superseded is skipped; the rest go through memory.bulk_set."""
    outcomes: dict[str, tuple[Outcome, str]] = {}
    pending: list[int] = []
    for key, i in first.items():
        retired = states.get(key, KeyState()).retired
        if retired:
            outcomes[key] = ("skipped", retired)
        else:
            pending.append(i)
    if not pending:
        return outcomes
    written = await handle_memory_bulk_set(
        _bulk_input(input, [(input.entries[i], keys[i]) for i in pending]),
        dry_run=input.dry_run,
        # Every entry is checked, whatever bulk_conflict_check_max_entries caps a bulk at.
        conflict_check_cap=MAX_IMPORT_ENTRIES,
        # A re-run reinforces nothing its own keys already hold.
        reinforce_own_key=False,
    )
    for i, result in zip(pending, written.written, strict=True):
        key = keys[i]
        outcomes[key] = _entry_outcome(
            key,
            sha256_value(input.entries[i].value),
            result,
            held_by=written.identity_duplicates.get(key),
            state=states.get(key, KeyState()),
        )
    return outcomes


def _entry_outcome(
    key: str,
    entry_sha: str,
    result: _BulkSetResult,
    *,
    held_by: str | None,
    state: KeyState,
) -> tuple[Outcome, str]:
    """The outcome and reason of an entry the bulk write answered: written (with the
    status its row was stored with when that is not `active`), refused by the check
    named, reinforced (a merge, or the same text under another key), or skipped as
    unchanged when its key holds its text in a live row of any session. Raises on any
    other result, so nothing unexpected is reported as an item already held."""
    verdict = result.conflict_verdict
    if result.version:
        return "written", _STORED_AS.get(verdict, "")
    if verdict in REFUSED:
        return "refused", _CHECK_OF[verdict]
    if verdict == "merge":
        return "reinforced", "merge"
    if entry_sha in state.held:
        return "skipped", UNCHANGED
    if held_by is not None and held_by != key:
        return "reinforced", "identity_duplicate"
    raise RuntimeError(f"memory.import: the bulk write's result for {key!r} matches no outcome")


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

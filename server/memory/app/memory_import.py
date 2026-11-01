"""memory.import: memory items another memory system distilled, written through the
bulk write path.

Each entry becomes a memory.bulk_set entry under the key its text derives
(`server.memory.domain.digest_keys.import_key`), declared `source_class=imported`,
so an import makes every check a bulk write makes: the identity's content-hash dedup,
the skip gate, the lock and pin guards, the blacklist, the sensitivity rule, inline
embedding and the conflict verdicts; and it leaves the bulk write's hash-only audit
row. A key whose newest version, in any session the caller's token may read, is a
tombstone or a superseded, rejected or proposed row is left as it is: a re-run never
brings back what its owner deleted, a newer fact retired or someone marked wrong, nor
decides a proposal on trial. A dry run makes the same checks and writes no row, link,
audit row or usage event (the embeddings are paid for and a blacklist hit is counted). A
category the namespace has registered is linked to the entry's key as
memory.assign_category links one (`server.memory.store.categories`).
"""

from __future__ import annotations

import logging
from collections import Counter

import server.memory.store as memory_repo
from server.core.datetimes import to_naive_utc
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
from server.memory.domain.digest_keys import import_key
from server.memory.domain.memory_import import (
    CATEGORY_NOT_LINKED,
    CATEGORY_REFUSED,
    CATEGORY_RESCINDED,
    CATEGORY_UNREGISTERED,
    UNCHANGED,
    UNKNOWN,
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
    REJECTED: "conflict",
}
# The status a written entry's verdict stores its row with when it is not `active`.
_STORED_AS: dict[str, Reason] = {"duplicate": "duplicate", "conflict": "conflicted"}


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
    # A token bounded to one user links no category: a link has no user axis, so it would
    # reach every user's row under the key (memory.assign_category is refused such a token).
    linkable = not scope.narrowed
    named = [entry.category for entry in input.entries if entry.category] if linkable else []
    # Read before anything is written, so a failed read leaves nothing half done.
    category_ids = await categories.registered_ids(input.org, input.namespace, named)
    heads = await memory_repo.key_heads_across_sessions(
        input.org, input.namespace, input.agent, input.user_id, list(first), scope=scope
    )
    outcomes = await _write(input, keys, first, skip_reasons(heads))
    results: list[_ImportEntryResult] = []
    for i, key in enumerate(keys):
        outcome, reason = outcomes[key] if first[key] == i else repeat_outcome(outcomes[key])
        results.append(_ImportEntryResult(index=i, key=key, outcome=outcome, reason=reason))
    await _categorize(input, results, category_ids, linkable=linkable)
    return _output(input, results)


async def _write(
    input: MemoryImportInput,
    keys: list[str],
    first: dict[str, int],
    skip: dict[str, Reason],
) -> dict[str, tuple[Outcome, Reason]]:
    """Key → the outcome of the first entry of its text. A key whose newest version leaves
    it as it is (a tombstone, a superseded, rejected or proposed row, or a state the import
    does not recognise) is skipped with that reason; the rest go through memory.bulk_set."""
    outcomes: dict[str, tuple[Outcome, Reason]] = {}
    pending: list[int] = []
    for key, i in first.items():
        reason = skip.get(key, "")
        if reason == UNKNOWN:
            log.warning("memory.import: %r's newest version is in no state it knows; left", key)
        if reason:
            outcomes[key] = ("skipped", reason)
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
        outcomes[key] = _entry_outcome(key, result, written.identity_duplicates.get(key))
    return outcomes


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
        return "reinforced", "merge"
    if held_by is not None and held_by != key:
        return "reinforced", "identity_duplicate"
    return "skipped", UNCHANGED


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


async def _categorize(
    input: MemoryImportInput,
    results: list[_ImportEntryResult],
    category_ids: dict[str, str],
    *,
    linkable: bool,
) -> None:
    """Note each entry's category, and link a registered one to the entry's key once the
    key holds the item. A link already stored is left as it is, so a re-run never brings
    back one the owner rescinded. The new links get one audit row: keys and category
    names, never the text."""
    wanted: list[tuple[_ImportEntryResult, tuple[str, str]]] = []
    for result, entry in zip(results, input.entries, strict=True):
        if not entry.category:
            continue
        if not linkable:
            result.note = CATEGORY_REFUSED
        elif entry.category not in category_ids:
            result.note = CATEGORY_UNREGISTERED
        elif not holds_item(result.outcome, result.reason):
            result.note = CATEGORY_NOT_LINKED
        else:
            wanted.append((result, (result.key, category_ids[entry.category])))
    if not wanted:
        return
    stored = await categories.link_states(input.org, input.namespace, [k for _, (k, _) in wanted])
    for result, link in wanted:
        if stored.get(link) is False:
            result.note = CATEGORY_RESCINDED
    new = sorted({link for _, link in wanted if link not in stored})
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

"""What `memory.import` decides with no I/O: what the stored versions of a key say
before the write, how an entry repeating an earlier one of the request answers, and why
an entry's category is not linked (`category_note`).
The key itself is `import_keys.import_key`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

from server.core.hashing import hash_text
from server.memory.domain.temporal_sql import LIVE_STATUSES, PROPOSED_STATUS, SUPERSEDED_STATUS

Outcome = Literal["written", "held", "skipped", "refused"]
# Why an entry was not plainly written; the values are listed in
# docs/features/memory/writing.md#importing-from-another-memory-system.
Reason = Literal[
    "",
    "blacklist",
    "blacklist_unchecked",
    "pinned",
    "locked",
    "pinned_peer",
    "merge",
    "identity_duplicate",
    "unchanged",
    "deleted",
    "superseded",
    "rejected",
    "proposed",
    "revised",
    "sensitivity_kept",
    "repeated",
    "unknown",
    "duplicate",
    "conflicted",
    "refines",
]

# Why an entry's category is not linked to its key ('' when it is): the same docs section.
Note = Literal[
    "", "category_unregistered", "category_refused", "category_not_linked", "category_rescinded"
]

# A head status that leaves the key as it is, skipped under the status's own name.
_LEFT_AS_IS: Final[dict[str, Reason]] = {
    "tombstoned": "deleted",
    SUPERSEDED_STATUS: "superseded",
    "rejected": "rejected",
    PROPOSED_STATUS: "proposed",
}


# The reasons of a skipped entry whose key holds its item.
_HOLDS_ITEM: Final[frozenset[Reason]] = frozenset({"unchanged", "sensitivity_kept", "repeated"})


def skip_reasons(
    heads: Iterable[Mapping[str, Any]],
    content_shas: Mapping[str, str],
    sensitivities: Mapping[str, str],
) -> dict[str, Reason]:
    """Key → why the import leaves it as it is, from its newest version across sessions
    (rows with `memory_key`, `deleted`, `head_status`, `head_sha256` and
    `head_sensitivity`), the `content_sha256` the bulk write would store for its item
    (`content_shas`, not the key's digest) and the sensitivity the item names ('' none).
    A key with no stored version is absent and is written."""
    out: dict[str, Reason] = {}
    for row in heads:
        key = str(row["memory_key"])
        status = str(row["head_status"] or "")
        reason: Reason
        if row["deleted"]:
            reason = "deleted"
        elif status in LIVE_STATUSES:  # holds its fact: the item's text, or other text
            if hash_text(row["head_sha256"]) != content_shas[key]:
                reason = "revised"
            elif sensitivities[key] and sensitivities[key] != row["head_sensitivity"]:
                reason = "sensitivity_kept"  # the stored level stands; memory.set changes it
            else:
                reason = "unchanged"
        else:
            reason = _LEFT_AS_IS.get(status, "unknown")
        out[key] = reason
    return out


def repeat_outcome(outcome: Outcome, reason: Reason) -> tuple[Outcome, Reason]:
    """How an entry repeating an earlier entry's text answers: skipped as `repeated`
    when the earlier one was written, else as the earlier one did (a refusal, another
    key holding it, a key left as it is)."""
    return ("skipped", "repeated") if outcome == "written" else (outcome, reason)


def holds_item(outcome: Outcome, reason: Reason) -> bool:
    """Whether an entry's key holds its item once the write is done: written now, there
    already, or written by an earlier entry of the request."""
    return outcome == "written" or (outcome == "skipped" and reason in _HOLDS_ITEM)


def category_note(
    outcome: Outcome,
    reason: Reason,
    *,
    linkable: bool,
    registered: bool,
    stored_live: bool | None,
) -> Note:
    """The note on an entry that names a category; '' when its key is linked or is about
    to be. `stored_live` is the stored link of the key to the category: True live, False
    rescinded, None never linked. A stored link is left as it is, so a re-run never brings
    back a rescinded one."""
    if not linkable:
        return "category_refused"
    if not registered:
        return "category_unregistered"
    if stored_live is False:
        return "category_rescinded"
    if stored_live is None and not holds_item(outcome, reason):
        return "category_not_linked"
    return ""

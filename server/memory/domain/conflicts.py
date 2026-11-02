"""The values a conflict check answers with (ConflictResult and its peers, the
per-namespace write config) and the pure rules it applies: canonical text,
same-content and templated-substitution tests and intra-batch supersession. The
reads are server/memory/app/conflicts.py."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ConflictPeer:
    memory_id: str
    memory_key: str
    similarity: float
    pinned: bool
    canonical: str
    # Embedding identity + raw value: a `refines` verdict supersedes the peer through
    # the embedding identity the same way same-key supersession does.
    agent: str = ""
    user_id: str = ""
    session_id: str = ""
    value: str = ""
    # The run that wrote the peer ('-' = none). A canonical match across run scopes is a
    # duplicate, not a merge.
    run_id: str = "-"
    # When the peer's fact held: a templated substitution under `newest_wins`
    # retires the peer only if this is not later than the new row's own valid_at —
    # arrival order is the clock only when the calendar cannot tell.
    valid_at: datetime | None = None


@dataclass(frozen=True)
class ConflictResult:
    """Outcome of check_on_commit. Caller branches on `verdict`:

    - "clear":     no conflict; normal insert
    - "merge":     canonical-form match; return existing_memory_id, skip insert
    - "duplicate": similarity ≥ duplicate_threshold, canonical differs; insert
                   with status='duplicate' + duplicate_of pointer
    - "conflict":  peer close-but-different; insert + flag both conflicted
    - "refines":   templated substitution under `newest_wins`; insert active,
                   supersede refined_peers
    - "rejected":  peer pinned and close-but-different; raise 409 PINNED_CONFLICT
    """

    verdict: str
    peers: list[ConflictPeer]
    existing_memory_id: str | None = None  # set when verdict=='merge'
    pinned_blocker_id: str | None = None  # set when verdict=='rejected'
    duplicate_of_id: str | None = None  # set when verdict=='duplicate'
    # verdict=='merge': which scope the restated peer belongs to — "same_run",
    # "unscoped" (no run on the write), or "cross_run" (must not happen; the
    # write path records it as a drop so a regression shows on the dashboard).
    merge_scope: str = ""
    # verdict=='refines': the peers the new row replaces; the caller inserts the row
    # active and supersedes these.
    refined_peers: list[ConflictPeer] = field(default_factory=list)
    # The conflict resolver owes this write a judgement: a `conflict`, or a `clear`
    # with a peer between the resolver floor and the conflict threshold.
    review: bool = False


# The namespace conflict policy under which the gate never reaches a verdict against a
# peer (see ConflictPolicyStr).
APPEND_ONLY_POLICY = "append_only"

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


_CONTENT_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "is",
        "are",
        "was",
        "were",
        "be",
        "and",
        "or",
        "for",
        "with",
        "by",
        "as",
        "it",
        "its",
        "that",
        "this",
        "has",
        "have",
        "had",
        "from",
        "into",
        "his",
        "her",
        "their",
        "he",
        "she",
        "they",
        "user",
        "users",
    }
)


_PARENTHETICAL_RE = re.compile(r"\s*\([^()]*\)")


def strip_parentheticals(text: str) -> str:
    """ "Basketball was created in the country of Soviet Union (updated from
    United States of America)" → the statement without its aside. The
    extractor sometimes writes the value it is replacing inside parentheses;
    with the aside kept, the new row's words are a superset of the old row's
    and the duplicate step files the update as a restatement (the original
    stays active, the update `duplicate`)."""
    return _PARENTHETICAL_RE.sub("", text or "").strip()


# Value tokens: clock times (27:12, 5:30pm), dates, money, percentages, plain
# numbers with optional unit suffix (5k, 10km, 3rd).
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_VALUE_RE = re.compile(
    r"(?<![A-Za-z])(?:\$|€|£)?\d+(?:[.,:]\d+)*(?:\s?(?:%|am|pm|k|km|kg|lb|lbs|mi|hrs?|min|s))?(?![A-Za-z])",
    re.IGNORECASE,
)


def value_tokens(text: str) -> set[str]:
    """Normalised value tokens in `text` ("27:12", "25:50", "$40", "5k")."""
    out: set[str] = set()
    for m in _VALUE_RE.finditer(text or ""):
        # lowercased, with no space before a unit ("5K" and "5 k" read as "5k") and no
        # thousands separator ("1,000" reads as "1000"); a currency sign stays
        token = m.group(0).strip().lower().replace(" ", "")
        out.add(_THOUSANDS.sub("", token))
    return out


_DATE_IN_TEXT_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")


def date_tokens(text: str) -> set[str]:
    """The value tokens that are only the pieces of an ISO day written into
    the text ("2023", "05", "15" of "on 2023-05-15"). An event's date is its
    anchor, not the value of an attribute."""
    out: set[str] = set()
    for m in _DATE_IN_TEXT_RE.finditer(text or ""):
        out.update(m.groups())
    return out


def values_differ(a: str, b: str) -> bool:
    """True when both texts state values and each holds one the other lacks ("27:10" /
    "26:30"). One side restating the other's value plus more ("27:12" / "27:12 in the 5k")
    does not differ, and the day written into a text is not a value."""
    va, vb = value_tokens(a) - date_tokens(a), value_tokens(b) - date_tokens(b)
    return bool(va and vb) and not (va <= vb or vb <= va)


def same_content_words(a: str, b: str) -> bool:
    """True when two values carry the same content words, one side possibly
    adding to the other (canonical form, function words dropped,
    singular/plural and -ing/-ed tails ignored).
    Cosine above the duplicate gate says "about the same thing"; this says
    "saying the same thing" — the difference between a restatement and a
    one-word update, which the duplicate step must not swallow. Two values that
    differ make an update however the words around them changed ("my PB is 27:10" /
    "beat my PB, 26:30")."""
    a, b = strip_parentheticals(a), strip_parentheticals(b)
    if values_differ(a, b):
        return False

    def words(t: str) -> set[str]:
        out: set[str] = set()
        for w in canonical(t or "").split():
            if (len(w) < 3 and not w.isdigit()) or w in _CONTENT_STOP:
                continue  # a number of any length is a content word ("26" vs "27")
            for suf in ("ing", "ed", "es", "s"):
                if len(w) > 4 and w.endswith(suf):
                    w = w[: -len(suf)]
                    break
            out.add(w)
        return out

    wa, wb = words(a), words(b)
    if not wa or not wb:
        return canonical(a or "") == canonical(b or "")
    # A restatement may add or drop words ("PR 1 has a bug" / "PR 1 has a bug:
    # the root cause is a missing null check"). A paraphrase shares few words
    # and the cosine gate is the better witness ("likes the beach" / "enjoys
    # spending time by the seashore"). The shape that is NOT a duplicate is
    # the templated substitution: most words shared, each side holding a
    # word the other lacks ("… field of politician" / "… field of soldier",
    # "… is 26" / "… is 27").
    if wa <= wb or wb <= wa:
        return True
    if templated_substitution(a, b):
        return False  # the same shape with a different fill is an update, not a restatement
    jaccard = len(wa & wb) / len(wa | wb)
    return jaccard < 0.5


def _content_word_set(text: str) -> set[str]:
    out: set[str] = set()
    for w in canonical(text or "").split():
        if (len(w) < 3 and not w.isdigit()) or w in _CONTENT_STOP:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def templated_substitution(a: str, b: str) -> bool:
    """Two statements of the same shape with different fills: most content
    words shared, each side holding at least one the other lacks, lengths
    within a word of each other — "basketball was created in the country of
    United States of America" / "… of Soviet Union", "the chairperson of
    Fatah is Mahmoud Abbas" / "… is Moshe Kahlon". This is the shape of an
    update to a single-valued attribute, and under a `newest_wins` namespace
    it needs no judge: the row being written is the newer statement (the
    judge calls the Soviet Union statement a restatement, which would file
    the update as a duplicate of the original)."""
    wa = _content_word_set(strip_parentheticals(a))
    wb = _content_word_set(strip_parentheticals(b))
    if not wa or not wb or wa <= wb or wb <= wa:
        return False
    shared = len(wa & wb)
    if shared < 2 or shared < min(len(wa), len(wb)) // 2:
        return False
    return abs(len(wa) - len(wb)) <= 2


def kept_versions(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The row FINAL keeps of each key a bulk writes. Its entries share one identity,
    so two rows of one key are two versions of one row: the higher version is kept."""
    kept: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = str(r.get("memory_key") or "")
        if key not in kept or int(r.get("version") or 0) >= int(kept[key].get("version") or 0):
            kept[key] = r
    return kept


def intra_batch_supersessions(
    values: list[str], types: list[str] | None = None
) -> list[tuple[int, int]]:
    """The templated substitutions INSIDE one write. A 35-statement chunk holds
    both "The capital of Soviet Union is Moscow" and "… is Russellville"; the
    verdicts compare each new row with the store, never with its batch
    siblings, and the value-update linker skips same-day pairs — so both would
    land active and the reader could cite Moscow. Within a batch the later row
    is the newer statement of the attribute (the extractor keeps reading
    order): returns (older index, newer index) pairs, same memory type only.
    Pure."""
    # Stricter than `templated_substitution`, which the cosine gate backs on
    # the store path: here nothing else vouches for the pair, so the two texts
    # must share their subject and predicate and differ only in the object
    # ("Basketball was created in the country of Soviet Union" / "Club Nouveau
    # was created in the country of Soviet Union" are two subjects, not an
    # update; a looser one-span rule would pair them and supersede the update).
    from server.memory.domain.triples_follow_rows import object_substitution

    out: list[tuple[int, int]] = []
    n = len(values)
    for j in range(n):
        vj = values[j] or ""
        if not vj:
            continue
        for i in range(j):
            if types and i < len(types) and j < len(types) and types[i] != types[j]:
                continue
            vi = values[i] or ""
            if vi and templated_substitution(vi, vj) and object_substitution(vi, vj):
                out.append((i, j))
    return out


def stamp_intra_batch_supersessions(
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    pairs: list[tuple[int, int]],
    now: datetime,
) -> list[tuple[str, str]]:
    """Apply `intra_batch_supersessions` pairs to the rows about to be
    inserted: the earlier row of each pair takes status `superseded`, `superseded_by` =
    the later row's id and `invalid_at` = the later row's `valid_at` (else `now`),
    mirrored on its own version's embedding row. A pair under one key is two versions
    of one row, which FINAL already settles: it is skipped (a guard; the bulk passes only
    the versions it keeps). Every memory row ends with
    the same key set — `ch.insert` derives the column list from row 0, and a key present
    on some rows only fails the whole batch. Returns the (older id, newer id) pairs
    stamped. Pure."""
    for row in memory_rows:
        row.setdefault("status", "active")
        row.setdefault("superseded_by", "")
        row.setdefault("invalid_at", None)
    # By key and version: a key written twice has an embedding row per version.
    emb_of = {(str(e.get("memory_key") or ""), e.get("version")): e for e in embedding_rows}
    out: list[tuple[str, str]] = []
    for i, j in pairs:
        if not (0 <= i < len(memory_rows) and 0 <= j < len(memory_rows)):
            continue
        older, newer = memory_rows[i], memory_rows[j]
        if older.get("memory_key") == newer.get("memory_key"):
            continue
        if str(older.get("status") or "active") == "superseded":
            continue
        inv = newer.get("valid_at")
        inv = inv if isinstance(inv, datetime) else now
        older["status"] = "superseded"
        older["superseded_by"] = str(newer.get("memory_id") or "")
        older["invalid_at"] = inv
        e = emb_of.get((str(older.get("memory_key") or ""), older.get("version")))
        if e is not None:
            e["status"] = "superseded"
            if "invalid_at" in e:
                e["invalid_at"] = inv
            if "superseded_by" in e:
                e["superseded_by"] = str(newer.get("memory_id") or "")
        out.append((str(older.get("memory_id") or ""), str(newer.get("memory_id") or "")))
    return out


def canonical(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation.

    Deliberately shallow: stemming/stopword removal would false-match
    "Postgres is great" with "Postgres is the worst".
    """
    s = text.lower()
    s = _PUNCT.sub("", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s


def query_max_distance(threshold: float) -> float:
    """The cosine-distance ceiling the peer statement filters on."""
    return 1.0 - threshold


def _as_datetime(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is not None else v
    return None


def later_valid_peers(
    peers: list[ConflictPeer], new_valid_at: datetime | None
) -> list[ConflictPeer]:
    """The contradicted peers whose fact is dated AFTER the new row's.
    Both dates must be known; a peer or a new row without a `valid_at` falls back to
    arrival order and is never in this list."""
    if new_valid_at is None:
        return []
    ref = _as_datetime(new_valid_at)
    if ref is None:
        return []
    return [p for p in peers if p.valid_at is not None and p.valid_at > ref]


def merge_or_run_duplicate(
    peer: ConflictPeer,
    peers: list[ConflictPeer],
    *,
    new_run_id: str = "-",
) -> ConflictResult:
    """The one place a restatement becomes a verdict:
    under a run, a restatement of a row another scope wrote — another run,
    or no run — is `duplicate` (insert, `duplicate_of` the peer) so the run's
    reads find what the run wrote; a same-run restatement, or any unscoped
    write, is `merge` (no insert)."""
    if new_run_id not in ("", "-") and peer.run_id != new_run_id:
        return ConflictResult(
            verdict="duplicate",
            peers=peers,
            duplicate_of_id=peer.memory_id,
        )
    return ConflictResult(
        verdict="merge",
        peers=peers,
        existing_memory_id=peer.memory_id,
        merge_scope=merge_scope(new_run_id, peer.run_id),
    )


def merge_scope(new_run_id: str, peer_run_id: str) -> str:
    """Which scope a merge folds into: "unscoped" when the write names no run,
    "same_run" when the peer is this run's, "cross_run" otherwise — a case
    that must not reach a merge, kept as a name so the write path can count it."""
    if new_run_id in ("", "-"):
        return "unscoped"
    return "same_run" if str(peer_run_id or "-") == new_run_id else "cross_run"


def substitution_result(
    subst: list[ConflictPeer],
    peers: list[ConflictPeer],
    *,
    new_valid_at: datetime | None = None,
) -> ConflictResult:
    """The verdict for peers that are templated substitutions of the new value
    under `newest_wins`: the row being written is the newest statement of the
    attribute, so the peers are superseded through `apply_refines`. This is what
    reaches the attribute stated under two coined keys (`x_module_assignment` vs
    `x_assigned_module`), which same-key supersession cannot bridge.

    The clock is the calendar before it is the arrival: when the new row and a
    peer both carry a `valid_at` and the peer's is later, the row being written
    is the OLDER statement, and retiring the peer would put the store back in
    time. Then the pair stays a `conflict` to review. A changed amount (a year,
    a count, a time) is what an update of this shape changes, so it never keeps
    the old row."""
    if later_valid_peers(subst, new_valid_at):
        return ConflictResult(verdict="conflict", peers=subst)
    return ConflictResult(verdict="refines", peers=peers, refined_peers=subst)


@dataclass(frozen=True)
class WriteConfig:
    """The per-namespace reads a point write makes before its statement,
    fetched together. None on any field means "not prefetched; read as before" — a
    failed prefetch costs nothing but the round trips it meant to save."""

    threshold: float | None = None
    duplicate_threshold: float | None = None
    policy: str | None = None
    allowlisted: set[str] | None = None
    type_config: Any = None
    memory_type: str = ""

    def check_kwargs(self) -> dict[str, Any]:
        return {
            "resolved_threshold": self.threshold,
            "resolved_duplicate_threshold": self.duplicate_threshold,
            "resolved_policy": self.policy,
            "allowlisted_keys": self.allowlisted,
        }

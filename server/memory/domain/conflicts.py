"""The values a conflict check answers with (ConflictResult and its peers, a
deferred judgement, the per-namespace write config) and the pure rules it
applies: canonical text, same-content and templated-substitution tests,
intra-batch supersession, quantity preservation, and folding a judge's
verdicts into a result. The reads and the judge call are
server/memory/app/conflicts.py."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.settings import settings


@dataclass(frozen=True)
class ConflictPeer:
    memory_id: str
    memory_key: str
    similarity: float
    pinned: bool
    canonical: str
    # Embedding identity + raw value: the judge reads the value, and a `refines` verdict
    # supersedes the peer through the embedding identity the same way same-key
    # supersession does.
    agent: str = ""
    user_id: str = ""
    session_id: str = ""
    value: str = ""
    # The run that wrote the peer ('-' = none). A canonical match across run scopes is a
    # duplicate, not a merge.
    run_id: str = "-"
    # When the peer's fact held: a judged contradiction under
    # `newest_wins` retires the peer only if this is not later than the new
    # row's own valid_at — arrival order is the clock only when the calendar
    # cannot tell.
    valid_at: datetime | None = None


@dataclass(frozen=True)
class PendingJudge:
    """The judge's input, captured at the point `check_on_commit` would have
    called it. A write that defers the judge inserts its row
    first and settles this afterwards through `settle_deferred_judgement`,
    which runs the very same call and fold the inline path runs — parity by
    construction, not by a second implementation.

    `candidates` are the peers the judge labels (flagged + band, closest
    first); `cosine_peers` the peers the cosine flag alone named ([] on a
    band-only check); `fallback_verdict` what the cosine path answers when the
    judge does not run or cannot be parsed: `clear` for a band-only check,
    `conflict` for a flagged pair."""

    org: str
    namespace: str
    new_value: str
    candidates: list[ConflictPeer]
    cosine_peers: list[ConflictPeer]
    fallback_verdict: str
    new_valid_at: datetime | None = None
    new_run_id: str = "-"
    # The namespace conflict policy when the caller prefetched it; None reads.
    policy: str | None = None


@dataclass(frozen=True)
class ConflictResult:
    """Outcome of check_on_commit. Caller branches on `verdict`:

    - "clear":     no conflict; normal insert
    - "merge":     canonical-form match; return existing_memory_id, skip insert
    - "duplicate": similarity ≥ duplicate_threshold, canonical differs; insert
                   with status='duplicate' + duplicate_of pointer
    - "conflict":  peer close-but-different; insert + flag both conflicted
    - "refines":   judged elaboration; insert active, supersede
                   refined_peers, link with a `refines` edge
    - "rejected":  peer pinned and close-but-different; raise 409 PINNED_CONFLICT
    - "pending":   the judge was deferred: insert the row active,
                   schedule `pending_judge`; the settled verdict lands
                   on the row's status / duplicate_of afterwards
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
    # verdict=='refines': the peers the new row elaborates; the caller inserts the row
    # active, supersedes these and links them.
    refined_peers: list[ConflictPeer] = field(default_factory=list)
    # The edge written from the new row to each refined peer: `refines` for a
    # judged elaboration, `supersedes` when a judged contradiction folds to
    # supersession under the namespace's newest_wins policy.
    refine_relation: str = "refines"
    # The judge's per-peer labels when it ran ({memory_id: label}); empty
    # when the verdict came from cosine alone.
    judged: dict[str, str] = field(default_factory=dict)
    # verdict=='pending': what the deferred judge will run.
    pending_judge: PendingJudge | None = None


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


def same_content_words(a: str, b: str) -> bool:
    """True when two values carry the same content words, one side possibly
    adding to the other (canonical form, function words dropped,
    singular/plural and -ing/-ed tails ignored).
    Cosine above the duplicate gate says "about the same thing"; this says
    "saying the same thing" — the difference between a restatement and a
    one-word update, which the duplicate step must not swallow."""
    a, b = strip_parentheticals(a), strip_parentheticals(b)

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
    """The cosine-distance ceiling the peer statement filters on. With the
    judge on, the statement reaches down to the judge's band
    floor so band peers (candidates for the judge only) are fetched too."""
    judge_on = bool(settings.memory_conflict_judge)
    band_floor = float(settings.memory_conflict_judge_min_similarity)
    query_floor = min(threshold, band_floor) if judge_on else threshold
    return 1.0 - query_floor


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
    judged: Any = None,
) -> ConflictResult:
    """The one place a restatement becomes a verdict:
    under a run, a restatement of a row another scope wrote — another run,
    or no run — is `duplicate` (insert, `duplicate_of` the peer) so the run's
    reads find what the run wrote; a same-run restatement, or any unscoped
    write, is `merge` (no insert). Every `merge` in this module comes through
    here, whichever tier noticed the restatement."""
    if new_run_id not in ("", "-") and peer.run_id != new_run_id:
        return ConflictResult(
            verdict="duplicate",
            peers=peers,
            duplicate_of_id=peer.memory_id,
            judged=judged or {},
        )
    return ConflictResult(
        verdict="merge",
        peers=peers,
        existing_memory_id=peer.memory_id,
        judged=judged or {},
        merge_scope=merge_scope(new_run_id, peer.run_id),
    )


def merge_scope(new_run_id: str, peer_run_id: str) -> str:
    """Which scope a merge folds into: "unscoped" when the write names no run,
    "same_run" when the peer is this run's, "cross_run" otherwise — a case
    that must not reach a merge, kept as a name so the write path can count it."""
    if new_run_id in ("", "-"):
        return "unscoped"
    return "same_run" if str(peer_run_id or "-") == new_run_id else "cross_run"


_QUANTITY_RE = re.compile(
    r"(?:\$|€|£)\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?(?:%|percent|k\b|dollars?|usd|eur|"
    r"miles?|km|kg|lbs?|hours?|hrs?|minutes?|mins?|days?|weeks?|months?|years?)\b|\b\d{2,}(?:\.\d+)?\b",
    re.IGNORECASE,
)


def quantities(text: str) -> set[str]:
    """The amounts a statement carries — currency, numbers with units, and
    bare numbers of two or more digits — normalised (no symbol, no thousands
    separators, lower-cased unit). Pure."""
    out: set[str] = set()
    for m in _QUANTITY_RE.finditer(text or ""):
        tok = m.group(0).lower().replace(",", "").replace(" ", "")
        tok = re.sub(r"^[$€£]", "", tok)
        num = re.match(r"\d+(?:\.\d+)?", tok)
        if num:
            out.add(num.group(0).rstrip("0").rstrip(".") if "." in num.group(0) else num.group(0))
    return out


def preserves_quantities(new_value: str, old_value: str) -> bool:
    """A refinement may retire a peer only if it keeps every
    amount the peer stated. Otherwise a fact loses its answer, as when
    "ordered from Thrive Market last month and spent around $150" is retired
    by "ordered from Thrive Market last month and got great deals": the judge
    calls the second an elaboration and the amount leaves the fact plane.
    Pure."""
    missing = quantities(old_value) - quantities(new_value)
    return not missing


def _fold_judged(
    judged: Any,
    candidates: list[ConflictPeer],
    cosine_peers: list[ConflictPeer] | None = None,
    policy: str = "",
    new_valid_at: datetime | None = None,
    new_run_id: str = "-",
    new_value: str = "",
) -> ConflictResult:
    """Turn a Judgement over `candidates` into a ConflictResult. `cosine_peers`
    are the ones the flag alone would have named; a `conflict` verdict with
    no contradicted peer falls back to them (never to band peers).

    Under the namespace's `newest_wins` policy a judged CONTRADICTION
    is not a conflict to review but a predecessor to retire: the row being
    written is the newest statement of the attribute, so the contradicted
    peers are superseded through the `refines` machinery with a
    `supersedes` edge. This is what reaches the attribute stated under two
    coined keys (`x_module_assignment` vs `x_assigned_module`), which
    same-key supersession cannot bridge.

    The clock is the calendar before it is the arrival: when
    the new row and a contradicted peer both carry a `valid_at` and the
    peer's is later, the row being written is the OLDER statement, and
    retiring the peer would put the store back in time. That pair stays a
    `conflict` to review; the other contradicted peers still fold."""
    by_id = {p.memory_id: p for p in candidates}
    kept = [by_id[i] for i in judged.keep if i in by_id]
    if judged.verdict == "conflict" and policy == "newest_wins" and kept:
        later = later_valid_peers(kept, new_valid_at)
        if later:
            return ConflictResult(verdict="conflict", peers=kept, judged=judged.labels)
        # A peer whose amounts the new row does not carry stays.
        kept = [p for p in kept if preserves_quantities(new_value, p.value)]
        if not kept:
            return ConflictResult(verdict="conflict", peers=candidates, judged=judged.labels)
        return ConflictResult(
            verdict="refines",
            peers=candidates,
            refined_peers=kept,
            judged=judged.labels,
            refine_relation="supersedes",
        )
    if judged.verdict == "clear":
        return ConflictResult(verdict="clear", peers=[], judged=judged.labels)
    if judged.verdict == "merge" and kept:
        # A judged restatement obeys the run rule the canonical match obeys;
        # merging across runs would lose a run's writes to other runs' rows.
        return merge_or_run_duplicate(
            kept[0], candidates, new_run_id=new_run_id, judged=judged.labels
        )
    if judged.verdict == "refines" and kept:
        # The same clock guards an elaboration: a row that holds on an earlier date
        # cannot retire a peer that holds on a later one, whatever the judge called the
        # pair ("considering attending" on 05-20 must not retire "attended" on 05-21).
        # Those peers stay; the rest refine.
        later_ids = {p.memory_id for p in later_valid_peers(kept, new_valid_at)}
        kept = [p for p in kept if p.memory_id not in later_ids]
        # An elaboration that drops a stated amount is not one;
        # the peer that carries the amount stays active beside the new row.
        kept = [p for p in kept if preserves_quantities(new_value, p.value)]
        if not kept:
            return ConflictResult(verdict="clear", peers=[], judged=judged.labels)
        return ConflictResult(
            verdict="refines", peers=candidates, refined_peers=kept, judged=judged.labels
        )
    fallback = list(cosine_peers or [])
    return ConflictResult(verdict="conflict", peers=kept or fallback, judged=judged.labels)


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

"""Every Postgres statement of the memory authority (the Postgres storage profile).

`loom.memory_current` holds the head of each key, the row `loom.memory FINAL` would
serve, stored without the fields at their default (`sparse_row`): a row replaces the
head only when its version is not older, as ReplacingMergeTree(version) decides. Each
commit also appends the rows to `loom.memory_revisions`, numbered per namespace by the
namespace's generation, and records what they owe ClickHouse in
`loom.memory_deliveries`. The transaction is short and does no other I/O.

Lock order inside a commit: the policy scopes' advisory locks (shared, sorted), then
the namespaces' generation rows by (org, scope), then heads by key, then deliveries
by seq, then the idempotency key's row.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from server.core import pg
from server.core.datetimes import to_naive_utc_second
from server.core.error_summary import error_summary
from server.core.errors import (
    LoomServerError,
    NotFoundError,
    ValidationError,
)
from server.core.numbers import FLOAT32_MAX, float32
from server.core.pg_lease import lease_deadline
from server.memory.domain import lock as _lock
from server.memory.domain.memory_types import is_embedded
from server.memory.domain.redaction_lineage import REDACTED_VALUE, Version, later_copies
from server.memory.store.rows import (
    FIRST_VERSION_DEFAULTS,
    IdentityKey,
    MemoryRow,
    _hydrate,
    identity_key,
    parse_memory_value,
)
from server.settings import settings

log = logging.getLogger(__name__)

# Bits of memory_deliveries.targets.
MEMORY_ROW = 1
SEARCH_DOC = 2
AUDIT_ROW = 4
DECISION_RECORD = 8  # a deleted decision's record to retire, once its delete is visible
OLD_VECTORS = 16  # the vectors of the version a revision that is not embedded replaced
SEARCHED = MEMORY_ROW | SEARCH_DOC  # the targets a search reads

# The lease token a delivery is written with: the committing writer's own.
COMMIT_TOKEN = 1

# loom.memory's DateTime and Nullable(DateTime) columns; a stored row keeps them as
# ISO text, whole seconds, naive UTC: what ClickHouse stores and reads back.
DATETIME_COLS = frozenset(
    {
        "tombstoned_at",
        "created_at",
        "expires_at",
        "redacted_at",
        "last_recalled_at",
        "temporal_anchor",
        "updated_at",
        "valid_at",
        "valid_to",
        "invalid_at",
        "last_reinforced_at",
        "mentioned_at",
        "verified_at",
        "lock_until",
        "locked_at",
    }
)
# Float32 columns, rounded as ClickHouse stores them.
_FLOAT32_COLS = frozenset({"confidence", "temporal_confidence"})


def encode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A loom.memory row as JSON, at ClickHouse's precision. Postgres text and jsonb
    hold no NUL character, so a row carrying one is refused here, before the commit."""
    out: dict[str, Any] = {}
    for col, val in row.items():
        if pg.unstorable(val):
            raise ValidationError(
                f"{col} holds a NUL character or a lone surrogate, which Postgres cannot store"
            )
        if isinstance(val, datetime):
            val = to_naive_utc_second(val).isoformat()
        elif isinstance(val, UUID):
            val = str(val)
        elif col in _FLOAT32_COLS and val is not None:
            if not math.isfinite(val) or abs(val) > FLOAT32_MAX:
                raise ValidationError(f"{col} is not a finite Float32 value")
            val = float32(val)
        out[col] = val
    return out


# The only fields a stored row may leave out, each at its loom.memory DDL default: those a
# first version may leave unset (`rows.FIRST_VERSION_DEFAULTS`). Every other field is
# always stored, a default-valued one included.
SPARSE_DEFAULTS: Mapping[str, Any] = FIRST_VERSION_DEFAULTS


def sparse_row(encoded: Mapping[str, Any]) -> dict[str, Any]:
    """An encoded row as it is stored: without the fields at their default
    (`SPARSE_DEFAULTS`), which every reader fills back (`decode_row`)."""
    return {c: v for c, v in encoded.items() if c not in SPARSE_DEFAULTS or v != SPARSE_DEFAULTS[c]}


def decode_row(stored: Mapping[str, Any]) -> dict[str, Any]:
    """The insertable loom.memory row back from its JSON, each field it leaves out at its
    default (`SPARSE_DEFAULTS`), as ClickHouse fills it."""
    out = {c: list(v) if isinstance(v, list) else v for c, v in SPARSE_DEFAULTS.items()}
    out.update(stored)
    for col in DATETIME_COLS:
        val = out.get(col)
        if isinstance(val, str):
            out[col] = datetime.fromisoformat(val)
    return out


def encode_audit(row: Mapping[str, Any]) -> dict[str, Any]:
    """An audit row as JSON at full precision: its hash covers `ts` to the millisecond."""
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in row.items()}


def decode_audit(stored: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(stored)
    if isinstance(out.get("ts"), str):
        out["ts"] = datetime.fromisoformat(out["ts"])
    return out


class IdempotencyFenced(LoomServerError):
    """503 — this request's idempotency claim is no longer its own: a retry with the key
    took it over, or an erasure in the org fenced it. This commit was not made.
    Retry after `retry_after_s`: the retry reads the answer of the one that took over,
    or runs the request again."""

    code = "IDEMPOTENT_TAKEN_OVER"
    status_code = 503

    def __init__(self) -> None:
        super().__init__(
            "this request's idempotency claim was taken over by a retry or fenced by an "
            "erasure; this commit was not made, retry to read the answer or run it again",
            retry_after_s=1,
        )


class HeadMoved(Exception):
    """A key's head changed after the write read it: the write starts over."""


def protection(row: Any) -> tuple[Any, ...] | None:
    """What a write built from `row` must not overwrite unseen: whether it is live, its
    pin, lock and lifecycle status, and a redaction of it. None for no live row. Takes a
    stored row (dict) or a MemoryRow."""
    if row is None or _field(row, "tombstoned_at") is not None:
        return None
    lock_until = _field(row, "lock_until")
    return (
        bool(_field(row, "pinned")),
        str(_field(row, "lock_mode") or ""),
        to_naive_utc_second(lock_until) if isinstance(lock_until, datetime) else None,
        str(_field(row, "status") or "active"),
        _field(row, "redacted_at") is not None,
    )


def _field(row: Any, col: str) -> Any:
    # Tolerates a column an older build did not store; `row_field` would raise.
    return row.get(col) if isinstance(row, dict) else getattr(row, col, None)


def heads_unchanged(
    read: Mapping[IdentityKey, Any], *, exact: bool = False
) -> Callable[[IdentityKey, dict[str, Any] | None], None]:
    """An `Admission.recheck` of each written key's head against the row the write read
    (`read[key]`, or None). By default the head may carry a value committed since (the
    newer version wins, as in ClickHouse) but not another protection (a delete, a pin, a
    lock, a status or a redaction since). `exact` (compare-and-set, a copy) also requires
    the same version, in the same redaction state, as the row read; a write that read no
    row is checked by protection alone. Raises HeadMoved."""

    def recheck(key: IdentityKey, head: dict[str, Any] | None) -> None:
        then = read.get(key)
        if exact and then is not None:
            if not unchanged(then, head):
                raise HeadMoved()
        elif protection(head) != protection(then):
            raise HeadMoved()

    return recheck


def unchanged(read: Any, head: Any) -> bool:
    """Whether `head` is still the very row `read` (`heads_unchanged(exact=True)`)."""
    return protection(head) == protection(read) and _version_seen(head) == _version_seen(read)


def _version_seen(row: Any) -> tuple[int, bool] | None:
    """A row's version and whether it is redacted: a redaction scrubs in place, a
    deleted head included."""
    if row is None:
        return None
    return int(_field(row, "version")), _field(row, "redacted_at") is not None


class AdmissionContendedError(LoomServerError):
    """503 — commits kept landing in this write's scope faster than it could
    re-check against them. Retry after `retry_after_s`."""

    code = "ADMISSION_CONTENDED"
    status_code = 503

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retry_after_s=1)


class MemoryErased(NotFoundError):
    """404 — an erasure removed this memory after the write read it: the write is not
    committed, and the memory stays erased."""

    def __init__(self) -> None:
        super().__init__("the memory was erased while this write ran")


class AdmissionMoved(Exception):
    """A commit in an admission's scope landed after the admission captured it."""

    def __init__(self, generations: dict[tuple[str, str], int]) -> None:
        super().__init__("scope generation moved")
        self.generations = generations


class PeerErased(AdmissionMoved):
    """The verdict links a row to a memory an erasure removed since: its peers are read
    anew. An erasure leaves no revision in the gap to show it."""

    def __init__(self) -> None:
        super().__init__({})


@dataclass
class Admission:
    """What a semantic write checked against, rechecked inside its commit.

    `generations`: each scope's generation the verdict was reached at. `recheck`
    is called with each written key and its current head (a decoded row, or None)
    under the head's row lock, and raises to refuse or restart the write."""

    generations: dict[tuple[str, str], int] = field(default_factory=dict)
    recheck: Callable[[IdentityKey, dict[str, Any] | None], None] | None = None
    # Called inside the commit, under the namespace's generation lock, with the rows
    # committed after the captured generation: True when none can change the
    # verdict, so the commit goes ahead at the new generation. It does no I/O.
    gap_check: Callable[[list[dict[str, Any]]], bool] | None = None
    # Revisions a copy brings back from its own namespace, (memory_id, version),
    # unredacted when it read them: one a redaction scrubbed since refuses the commit
    # (HeadMoved), under the namespace's generation lock, which a redaction takes too.
    sources: Sequence[tuple[str, int]] = ()
    # The sources must still be their keys' live heads (a merge's), not old revisions.
    live_sources: bool = False


@dataclass
class Committed:
    """The rows as stored and their place in the journal: (org, namespace, seq)."""

    rows: list[dict[str, Any]]
    audits: list[dict[str, Any]]
    seqs: list[tuple[str, str, int]]
    # What each delivery owes, as committed.
    targets: list[int]
    # time.monotonic() past which this holder's lease may have run out: it sends
    # nothing more (an erasure waits out only the leases it removed).
    deadline: float = 0.0


# The scope names of the policy generations, which no namespace may take.
POLICY_SCOPE_NAMES = frozenset({"*", ""})


def policy_scopes(org: str) -> list[tuple[str, str]]:
    """The policy generations every write in `org` is admitted against."""
    return [("", "*"), (org, "*")]


async def capture(org: str, namespace: str) -> dict[tuple[str, str], int]:
    """The generations an admission in (org, namespace) is judged at: the policy
    scopes' current ones and the namespace's published watermark, the last seq
    below which every revision's memory row and search document are in ClickHouse, a
    parked revision's left out and a stub's document counted as in. What committed
    after it is the gap the commit checks (`pg_admission`)."""
    if pg.unstorable([org, namespace]):
        # Refused as the commit's `encode_row` would refuse it, before Postgres does.
        raise ValidationError(
            "the org or namespace holds a NUL character or a lone surrogate, which Postgres "
            "cannot store"
        )
    scopes = [*policy_scopes(org), (org, namespace)]
    rows = await pg.fetch(
        "SELECT g.org, g.scope, g.generation, "
        "(SELECT min(d.seq) - 1 FROM loom.memory_deliveries d "
        f" WHERE d.org = g.org AND d.namespace = g.scope AND d.targets & {SEARCHED} <> 0 "
        " AND d.lease_until <> 'infinity') AS published "
        "FROM loom.scope_generations g "
        "WHERE (g.org, g.scope) IN (SELECT * FROM unnest($1::text[], $2::text[]))",
        [s[0] for s in scopes],
        [s[1] for s in scopes],
    )
    got = {
        (r["org"], r["scope"]): int(
            r["generation"] if r["published"] is None else min(r["published"], r["generation"])
        )
        for r in rows
    }
    return {s: got.get(s, 0) for s in scopes}


def policy_part(generations: Mapping[tuple[str, str], int], org: str) -> dict[tuple[str, str], int]:
    """The policy scopes' entries of an admission's generations."""
    scopes = set(policy_scopes(org))
    return {s: g for s, g in generations.items() if s in scopes}


async def policy_generations(org: str) -> dict[tuple[str, str], int]:
    """The policy generations a write in `org` is guarded at, alone (`capture` reads the
    namespace's watermark too)."""
    scopes = policy_scopes(org)
    rows = await pg.fetch(
        "SELECT org, scope, generation FROM loom.scope_generations "
        "WHERE (org, scope) IN (SELECT * FROM unnest($1::text[], $2::text[]))",
        [s[0] for s in scopes],
        [s[1] for s in scopes],
    )
    got = {(r["org"], r["scope"]): int(r["generation"]) for r in rows}
    return {s: got.get(s, 0) for s in scopes}


async def generation(org: str, scope: str) -> int:
    row = await pg.fetchrow(
        "SELECT generation FROM loom.scope_generations WHERE org = $1 AND scope = $2", org, scope
    )
    return int(row["generation"]) if row else 0


async def search_backlog(org: str, namespace: str, through: int, owed: int = SEARCHED) -> bool:
    """Whether a revision at or below `through` owes ClickHouse one of the `owed` targets
    (its memory row or search document): what a check reading ClickHouse would miss."""
    if pg.unstorable(org) or pg.unstorable(namespace):
        return False  # Postgres holds no such scope, so nothing of it is owed
    return bool(
        await pg.fetchval(
            "SELECT EXISTS (SELECT 1 FROM loom.memory_deliveries "
            "WHERE org = $1 AND namespace = $2 AND seq <= $3 AND targets & $4 <> 0 "
            "AND lease_until <> 'infinity')",
            org,
            namespace,
            through,
            owed,
        )
    )


@dataclass(frozen=True)
class ParkedHead:
    namespace: str
    seq: int
    memory_id: str
    value_text: str  # the stored `value`, as ClickHouse's match reads it


async def parked_heads(
    org: str, namespace: str | None, *, after: tuple[str, int] | None, limit: int
) -> list[ParkedHead]:
    """Up to `limit` live heads (in `namespace` when given) whose own memory row is parked,
    in the deliveries' key order after `after`, a (namespace, seq): ClickHouse keeps
    refusing that row, so it holds an older version of the key or none."""
    if pg.unstorable(org) or (namespace is not None and pg.unstorable(namespace)):
        return []
    args: list[Any] = [org, limit]
    where = f"d.org = $1 AND d.lease_until = 'infinity' AND d.targets & {MEMORY_ROW} <> 0"
    if namespace is not None:
        args.append(namespace)
        where += f" AND d.namespace = ${len(args)}"
    if after is not None:
        args += list(after)
        where += f" AND (d.namespace, d.seq) > (${len(args) - 1}, ${len(args)}::bigint)"
    rows = await pg.fetch(
        "SELECT d.namespace, d.seq, r.memory_id, c.row->>'value' AS value_text "
        "FROM loom.memory_deliveries d "
        "JOIN loom.memory_revisions r USING (org, namespace, seq) "
        "JOIN loom.memory_current c ON c.org = r.org AND c.memory_id = r.memory_id "
        "AND c.namespace = r.namespace AND c.seq = r.seq "
        f"WHERE {where} AND c.row->>'tombstoned_at' IS NULL "
        "ORDER BY d.namespace, d.seq LIMIT $2",
        *args,
    )
    return [
        ParkedHead(r["namespace"], int(r["seq"]), str(r["memory_id"]), r["value_text"])
        for r in rows
    ]


async def unsearchable_through(
    org: str, namespaces: Sequence[str] | None, owed: int = SEARCHED
) -> dict[str, int]:
    """The namespaces (any of the org's when None) holding a revision that owes ClickHouse
    one of the `owed` targets, each with its current generation: the barrier a search
    admitted now sees through (a later commit numbers its revision above it)."""
    if pg.unstorable(org):
        return {}  # Postgres holds no such org, so nothing of it is owed
    args: list[Any] = [org, owed]
    scoped = ""
    if namespaces is not None:
        args.append([n for n in namespaces if not pg.unstorable(n)])
        scoped = " AND d.namespace = ANY($3::text[])"
    rows = await pg.fetch(
        "SELECT g.scope, g.generation FROM loom.scope_generations g "
        "WHERE g.org = $1 AND g.scope IN ("
        "  SELECT d.namespace FROM loom.memory_deliveries d WHERE d.org = $1 "
        f"  AND d.targets & $2 <> 0 AND d.lease_until <> 'infinity'{scoped})",
        *args,
    )
    return {r["scope"]: int(r["generation"]) for r in rows}


# Evidence expires by age, so a refusal never waits on the sweep, which only reclaims it.
_ERASED = """
SELECT EXISTS (SELECT 1 FROM loom.erased_memories e
JOIN unnest($1::text[], $2::text[], $3::uuid[]) AS w(org, namespace, memory_id)
USING (org, namespace, memory_id)
WHERE e.erased_at > now() - make_interval(hours => $4))
"""

# Takes its rows' locks in the order given: the caller passes the scopes sorted, which is
# byte order (`COLLATE "C"`), the lock order the module docstring states.
_BUMP = """
INSERT INTO loom.scope_generations AS g (org, scope, generation)
SELECT * FROM unnest($1::text[], $2::text[], $3::bigint[])
ON CONFLICT (org, scope) DO UPDATE SET generation = g.generation + EXCLUDED.generation
RETURNING org, scope, generation
"""

# The heads of exact keys, bound as six arrays ($1..$6, `_key_arrays`).
_EXACT_KEYS = """
FROM loom.memory_current c
JOIN unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[])
  AS k(org, namespace, agent, user_id, session_id, memory_key)
  USING (org, namespace, agent, user_id, session_id, memory_key)
"""


def _key_arrays(keys: Sequence[IdentityKey]) -> list[list[str]]:
    """The six identity columns of `keys`, one array each, for `_EXACT_KEYS`."""
    return [[k[i] for k in keys] for i in range(6)]


# Every locker of heads takes them in this order, so no two of them deadlock (the
# erasure step, which cannot import this module, repeats it as `steps_pg._KEY_ORDER`).
_KEY_ORDER = "ORDER BY c.org, c.namespace, c.agent, c.user_id, c.session_id, c.memory_key"

_HEAD_VERSIONS = f"""
SELECT c.org, c.namespace, c.agent, c.user_id, c.session_id, c.memory_key, c.version
{_EXACT_KEYS}"""

_SETTLED_HEAD_VERSIONS = f"""{_HEAD_VERSIONS}
{_KEY_ORDER}
FOR SHARE OF c
"""

_HEADS_BY_KEYS = f"SELECT c.row {_EXACT_KEYS}"

_HEADS_OWING = f"""
SELECT c.row, coalesce(d.targets, 0) AS owed {_EXACT_KEYS}
LEFT JOIN loom.memory_deliveries d
  ON d.org = c.org AND d.namespace = c.namespace AND d.seq = c.seq
"""

_LOCK_HEADS = f"""
SELECT c.org, c.namespace, c.agent, c.user_id, c.session_id, c.memory_key, c.row
{_EXACT_KEYS}
{_KEY_ORDER}
FOR UPDATE OF c
"""

_UPSERT_HEADS = """
INSERT INTO loom.memory_current AS c
    (org, namespace, agent, user_id, session_id, memory_key,
     memory_id, version, seq, content_sha256, row)
SELECT * FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[],
                     $7::uuid[], $8::bigint[], $9::bigint[], $10::text[], $11::jsonb[])
ON CONFLICT (org, namespace, agent, user_id, session_id, memory_key) DO UPDATE
SET memory_id = EXCLUDED.memory_id, version = EXCLUDED.version, seq = EXCLUDED.seq,
    content_sha256 = EXCLUDED.content_sha256, row = EXCLUDED.row
WHERE EXCLUDED.version >= c.version
"""

_INSERT_REVISIONS = """
INSERT INTO loom.memory_revisions
    (org, namespace, seq, user_id, memory_id, version, content_sha256, actor, source_class,
     request_id, row, audit)
SELECT * FROM unnest($1::text[], $2::text[], $3::bigint[], $4::text[], $5::uuid[], $6::bigint[],
                     $7::text[], $8::text[], $9::text[], $10::text[], $11::jsonb[], $12::jsonb[])
"""

_INSERT_DELIVERIES = f"""
INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, lease_until)
SELECT org, namespace, seq, targets, {COMMIT_TOKEN}, now() + make_interval(secs => $5)
FROM unnest($1::text[], $2::text[], $3::bigint[], $4::smallint[]) AS d(org, namespace, seq, targets)
"""


async def _gap_cleared(conn: Any, admission: Admission, moved: dict[tuple[str, str], int]) -> bool:
    """Whether the admission's `gap_check` clears every row committed in `moved`'s
    scopes since it captured them; the rows are read in this transaction, which holds
    those scopes' generation locks, so no commit can join the gap meanwhile. A seq
    missing from the gap was erased (`pg_admission._gap`)."""
    if admission.gap_check is None:
        return False
    rows: list[dict[str, Any]] = []
    for (org, ns), through in moved.items():
        after = admission.generations[(org, ns)]
        if through - after > settings.pg_admission_gap_rows:
            return False
        gap = await conn.fetch(
            "SELECT row FROM loom.memory_revisions "
            "WHERE org = $1 AND namespace = $2 AND seq > $3 AND seq <= $4",
            org,
            ns,
            after,
            through,
        )
        if any(r["row"] is None for r in gap):
            return False  # a payload cleared after its delivery: judged on ClickHouse's copy
        rows += [decode_row(r["row"]) for r in gap]
    return admission.gap_check(rows)


async def _refuse_moved_sources(
    conn: Any, scopes: Sequence[tuple[str, str]], admission: Admission | None
) -> None:
    """Refuse a copy of a revision a redaction scrubbed after the copy read it, or, with
    `live_sources`, one no longer its key's live head (deleted or rewritten since). It
    runs under the namespace's generation lock, which a redaction and a commit take too,
    so every one committed before this commit is seen."""
    if admission is None or not admission.sources:
        return
    sources = admission.sources
    [(org, namespace)] = scopes  # a copy commits within its one namespace
    moved = await conn.fetchval(
        "WITH s(memory_id, version) AS (SELECT * FROM unnest($3::uuid[], $4::bigint[])) "
        "SELECT EXISTS (SELECT 1 FROM loom.memory_revisions r JOIN s "
        "ON r.memory_id = s.memory_id AND r.version = s.version "
        f"WHERE r.org = $1 AND r.namespace = $2 AND {_meta('redacted_at', 'r')} IS NOT NULL) "
        "OR ($5 AND EXISTS (SELECT 1 FROM s WHERE NOT EXISTS ("
        "SELECT 1 FROM loom.memory_current c WHERE c.org = $1 AND c.namespace = $2 "
        "AND c.memory_id = s.memory_id AND c.version = s.version "
        "AND c.row->>'tombstoned_at' IS NULL)))",
        org,
        namespace,
        [UUID(mid) for mid, _v in sources],
        [int(v) for _mid, v in sources],
        admission.live_sources,
    )
    if moved:
        raise HeadMoved()


async def _refuse_stale_rows(
    conn: Any, stored: list[dict[str, Any]], admission: Admission | None
) -> None:
    """Inside the commit, refuse rows built from what an erasure removed since their
    read: MemoryErased for a memory of the rows; PeerErased for a verdict linking one."""
    if await conn.fetchval(
        _ERASED,
        [r["org"] for r in stored],
        [r["namespace"] for r in stored],
        [UUID(str(r["memory_id"])) for r in stored],
        settings.pg_erasure_evidence_hours,
    ):
        raise MemoryErased()
    if admission is not None and admission.gap_check is not None:
        linked = [(r["org"], r["namespace"], mid) for r in stored for mid in _links(r)]
        if linked and await conn.fetchval(
            _ERASED,
            [org for org, _, _ in linked],
            [ns for _, ns, _ in linked],
            [mid for _, _, mid in linked],
            settings.pg_erasure_evidence_hours,
        ):
            raise PeerErased()


def _links(row: Mapping[str, Any]) -> list[UUID]:
    """The memories a verdict linked the row to: its canonical and its conflicts."""
    ids = [str(row.get("duplicate_of") or ""), *(str(i) for i in row.get("conflict_with") or [])]
    return [UUID(i) for i in ids if i]


def _cols(rows: Sequence[Mapping[str, Any]], *names: str) -> list[list[Any]]:
    return [[r[n] for r in rows] for n in names]


async def _check_policy(conn: Any, admission: Admission, per_scope: Mapping[Any, int]) -> None:
    """Hold the admission's policy scopes shared to the commit, and refuse it
    (AdmissionMoved) when one moved since its capture."""
    policy = sorted(s for s in admission.generations if s not in per_scope)
    if policy:
        # Shared with every writer of the scope; `pg.bump_policy` takes it exclusively.
        # The read is its own statement, after the wait.
        await conn.execute(
            f"SELECT pg_advisory_xact_lock_shared({pg.POLICY_LOCK_KEY}) "
            "FROM unnest($1::text[], $2::text[]) WITH ORDINALITY AS k(org, scope, n) "
            "ORDER BY k.n",
            [s[0] for s in policy],
            [s[1] for s in policy],
        )
        got = await conn.fetch(
            "SELECT org, scope, generation FROM loom.scope_generations "
            "WHERE (org, scope) IN (SELECT * FROM unnest($1::text[], $2::text[]))",
            [s[0] for s in policy],
            [s[1] for s in policy],
        )
        now = {(g["org"], g["scope"]): int(g["generation"]) for g in got}
        moved = {s: now.get(s, 0) for s in policy if now.get(s, 0) != admission.generations[s]}
        if moved:
            raise AdmissionMoved(moved)


def deleted_decision(row: Mapping[str, Any]) -> bool:
    """A decision row's tombstone, whose record goes once it is visible."""
    return row.get("memory_type") == "decision" and row.get("tombstoned_at") is not None


async def commit_versions(
    rows: Sequence[Mapping[str, Any]],
    audits: Sequence[Mapping[str, Any]],
    *,
    targets: int | Sequence[int] = MEMORY_ROW | AUDIT_ROW,
    admission: Admission | None = None,
    request_id: str = "",
    idempotency: tuple[str, str, int] | None = None,
    publisher_lease: bool = True,
) -> Committed:
    """Commit `rows` (full loom.memory version rows): heads, revisions with
    `audits[i]` (row i's hash-only loom.audit_log row), deliveries (`targets`: one
    for every row, or row i's; a deleted decision's also owes DECISION_RECORD) and the
    idempotency key's committed state, in one transaction.

    The publisher of these rows holds their deliveries for
    LOOM_PG_REQUEST_LEASE_SECONDS under `COMMIT_TOKEN`; with `publisher_lease` False
    nobody does, and the delivery worker may claim them at once. Raises AdmissionMoved when
    a generation the admission captured has moved; the admission's `recheck` raises
    whatever refusal the head at commit calls for."""
    started = time.monotonic()
    stored = [encode_row(r) for r in rows]
    if any(r["namespace"] in POLICY_SCOPE_NAMES for r in stored):
        # A namespace's generation shares its key with the policy generations.
        raise ValidationError("'*' and '' are not namespace names")
    sparse = [sparse_row(r) for r in stored]
    per_row = [targets] * len(stored) if isinstance(targets, int) else list(targets)
    per_row = [
        t | (DECISION_RECORD if deleted_decision(r) else 0)
        for t, r in zip(per_row, stored, strict=True)
    ]
    per_scope: dict[tuple[str, str], int] = {}
    for r in stored:
        s = (r["org"], r["namespace"])
        per_scope[s] = per_scope.get(s, 0) + 1
    scopes = sorted(per_scope)
    # The newest version of each key is its head; a batch may carry a key twice.
    # Each key's head, as its index in `stored`.
    heads: dict[IdentityKey, int] = {}
    for i, r in enumerate(stored):
        k = identity_key(r)
        if k not in heads or int(r["version"]) >= int(stored[heads[k]]["version"]):
            heads[k] = i
    keys = sorted(heads)
    async with pg.transaction() as conn:
        await _bound_lock_waits(conn)
        if admission is not None:
            await _check_policy(conn, admission, per_scope)
        bumped = await conn.fetch(
            _BUMP, [s[0] for s in scopes], [s[1] for s in scopes], [per_scope[s] for s in scopes]
        )
        last = {(b["org"], b["scope"]): int(b["generation"]) for b in bumped}
        # Right after the generation lock, which an erasure of the scope holds while it
        # runs, and before the head recheck: an erased memory is a 404, not a moved head.
        await _refuse_stale_rows(conn, stored, admission)
        await _refuse_moved_sources(conn, scopes, admission)
        if admission is not None:
            moved = {
                s: last[s] - per_scope[s]
                for s in scopes
                if s in admission.generations and last[s] - per_scope[s] != admission.generations[s]
            }
            if moved and not await _gap_cleared(conn, admission, moved):
                raise AdmissionMoved(moved)
        # Each written key's head, locked: checked against what the write read, and a row
        # not embedded over one that was owes its vectors' retirement (`_replaces_vectors`).
        recheck = admission.recheck if admission is not None else None
        locked = await conn.fetch(_LOCK_HEADS, *_key_arrays(keys))
        current = {identity_key(h): decode_row(h["row"]) for h in locked}
        for k in keys:
            head = current.get(k)
            if recheck is not None:
                recheck(k, head)
            if _replaces_vectors(stored[heads[k]], head):
                per_row[heads[k]] |= OLD_VECTORS
        seqs: list[tuple[str, str, int]] = []
        next_seq = {s: last[s] - per_scope[s] for s in scopes}
        for r in stored:
            s = (r["org"], r["namespace"])
            next_seq[s] += 1
            seqs.append((s[0], s[1], next_seq[s]))
        head_rows = [stored[heads[k]] for k in keys]
        await conn.execute(
            _UPSERT_HEADS,
            *_cols(head_rows, "org", "namespace", "agent", "user_id", "session_id", "memory_key"),
            [UUID(str(r["memory_id"])) for r in head_rows],
            [int(r["version"]) for r in head_rows],
            [seqs[heads[k]][2] for k in keys],
            [str(r.get("content_sha256") or "") for r in head_rows],
            [sparse[heads[k]] for k in keys],
        )
        await conn.execute(
            _INSERT_REVISIONS,
            [q[0] for q in seqs],
            [q[1] for q in seqs],
            [q[2] for q in seqs],
            [str(r["user_id"]) for r in stored],
            [UUID(str(r["memory_id"])) for r in stored],
            [int(r["version"]) for r in stored],
            [str(r.get("content_sha256") or "") for r in stored],
            [str(r.get("actor") or "") for r in stored],
            [str(r.get("source_class") or "") for r in stored],
            [request_id] * len(stored),
            sparse,
            [encode_audit(a) for a in audits],
        )
        await conn.execute(
            _INSERT_DELIVERIES,
            [q[0] for q in seqs],
            [q[1] for q in seqs],
            [q[2] for q in seqs],
            per_row,
            # No publisher of its own: a lease that ended a send's bound ago, so the
            # delivery worker claims it at once and nothing waits for a send of it.
            float(settings.pg_request_lease_seconds) if publisher_lease else -pg.insert_bound_s(),
        )
        if idempotency is not None:
            held = await conn.fetchval(
                "UPDATE loom.idempotency SET state = 'committed', revisions = revisions || $4 "
                "WHERE org = $1 AND key_hash = $2 AND lease_token = $3 "
                "AND state IN ('running', 'committed') RETURNING 1",
                *idempotency,
                [[q[0], q[1], q[2]] for q in seqs],
            )
            if held is None:
                raise IdempotencyFenced()
    return Committed(
        rows=[decode_row(r) for r in stored],
        audits=[dict(a) for a in audits],
        seqs=seqs,
        targets=per_row,
        deadline=lease_deadline(settings.pg_request_lease_seconds, started)
        if publisher_lease
        else started,
    )


def _replaces_vectors(row: dict[str, Any], head: dict[str, Any] | None) -> bool:
    """Whether `row`, not embedded, replaces a live head that was: its commit owes the
    retirement of that head's vectors, whatever version the write read."""
    if head is None or head.get("tombstoned_at") is not None:
        return False
    return not embedded(row) and embedded(head)


def embedded(row: Mapping[str, Any]) -> bool:
    """Whether a stored row gets a search document (`is_embedded`): a redacted one serves
    none, so one written over an embedded head owes its vectors' retirement too."""
    if row.get("redacted_at") is not None:
        return False
    return is_embedded(str(row.get("memory_type")), str(row.get("sensitivity") or ""))


async def complete(
    seqs: Sequence[tuple[str, str, int]], done: int, *, tokens: Sequence[int] | int = COMMIT_TOKEN
) -> None:
    """Clear `done` from these deliveries, each held under its token (one for all, or
    one per seq); a delivery whose last target clears is deleted. A delivery a newer
    holder took (another token) is left to it.

    A delivery taken over (a token other than `COMMIT_TOKEN`) is not deleted at once: a
    holder it replaced may still have an insert in flight. It stays settled, its lease
    ending now, and the delivery pass sweeps it once that publisher is waited out
    (`sweep_settled`)."""
    if not seqs:
        return
    async with pg.transaction() as conn:
        await _complete_in(conn, seqs, done, tokens)


async def complete_each(landed: Sequence[tuple[tuple[str, str, int], int]]) -> None:
    """`complete` for deliveries that each landed other targets, (seq, done) apiece,
    held under `COMMIT_TOKEN`, in one transaction: all locked first, in seq order, as
    every holder locks them, then each `done` cleared."""
    by_done: dict[int, list[tuple[str, str, int]]] = {}
    for seq, done in landed:
        by_done.setdefault(done, []).append(seq)
    if not by_done:
        return
    async with pg.transaction() as conn:
        await _lock_held(conn, _held_keys([seq for seq, _ in landed], COMMIT_TOKEN))
        for done, seqs in by_done.items():
            await _complete_in(conn, seqs, done, COMMIT_TOKEN)


async def hand_over(seqs: Sequence[tuple[str, str, int]]) -> None:
    """End the commit's lease (`COMMIT_TOKEN`) on these deliveries, which still owe a
    target their publisher will not send: the delivery worker, or an admission waiting
    on them, takes them at once."""
    if not seqs:
        return
    await pg.execute(
        f"UPDATE loom.memory_deliveries d SET lease_until = LEAST(d.lease_until, now()) "
        f"FROM {_HELD_LIST} WHERE {_HELD_MATCH} AND NOT d.settled",
        *_held_keys(seqs, COMMIT_TOKEN),
    )


# The claims ($1..$4: org, namespace, seq, token) and the delivery each holds under its
# token.
_HELD_LIST = (
    "unnest($1::text[], $2::text[], $3::bigint[], $4::bigint[]) AS k(org, namespace, seq, token)"
)
_HELD_MATCH = "(d.org, d.namespace, d.seq, d.lease_token) = (k.org, k.namespace, k.seq, k.token)"


async def _lock_held(conn: Any, keys: tuple[list[Any], ...]) -> None:
    """Lock the deliveries these claims still hold, in seq order: a second completer of
    one (a row and its search document landing apart) waits, then reads what is left."""
    await conn.execute(
        f"SELECT 1 FROM loom.memory_deliveries d, {_HELD_LIST} WHERE {_HELD_MATCH} "
        "ORDER BY d.org, d.namespace, d.seq FOR UPDATE OF d",
        *keys,
    )


def _held_keys(
    seqs: Sequence[tuple[str, str, int]], tokens: Sequence[int] | int
) -> tuple[list[Any], ...]:
    held = [tokens] * len(seqs) if isinstance(tokens, int) else list(tokens)
    return [q[0] for q in seqs], [q[1] for q in seqs], [q[2] for q in seqs], held


async def _complete_in(
    conn: Any,
    seqs: Sequence[tuple[str, str, int]],
    done: int,
    tokens: Sequence[int] | int,
) -> None:
    """`complete` in the caller's transaction, the deliveries locked first (a lock the
    transaction already holds is taken again at no cost)."""
    keys = _held_keys(seqs, tokens)
    await _lock_held(conn, keys)
    # Deleted when nothing is left, else updated: one dead row per delivery, not two.
    await conn.execute(
        f"DELETE FROM loom.memory_deliveries d USING {_HELD_LIST} WHERE {_HELD_MATCH} "
        f"AND d.targets & ~$5::smallint = 0 AND d.lease_token = {COMMIT_TOKEN}",
        *keys,
        done,
    )
    await conn.execute(
        "UPDATE loom.memory_deliveries d SET targets = d.targets & ~$5::smallint, "
        "lease_until = CASE WHEN d.targets & ~$5::smallint = 0 THEN now() "
        f"ELSE d.lease_until END FROM {_HELD_LIST} WHERE {_HELD_MATCH} "
        # No dead row for a delivery that owed none of them.
        "AND d.targets & $5::smallint <> 0",
        *keys,
        done,
    )


@dataclass(frozen=True)
class Claimed:
    org: str
    namespace: str
    seq: int
    targets: int
    lease_token: int
    attempts: int
    row: dict[str, Any]
    audit: dict[str, Any]
    deadline: float
    # Taken over from a publish that stopped the process: published on its own.
    taken_over: bool = False
    # Seconds from the claim to the last moment the holder it replaced may send (its
    # lease, or its fence when later; negative: passed).
    replaced_lease_s: float = -math.inf


async def claim_due(limit: int, *, scope: tuple[str, str] | None = None) -> list[Claimed]:
    """Take up to `limit` deliveries whose lease ran out, oldest first, under a new
    lease token each, skipping locked and parked ones; one out of attempts is parked
    here or comes back for its stub (storage-authority.md, "Delivery"). `scope`: only
    those of one (org, namespace), in journal order."""
    started = time.monotonic()
    # Any delivery still owing a target; one out of attempts is parked here or comes back
    # for its stub (`pg_delivery._last_attempt`).
    where = "lease_until < now() AND NOT settled"
    order = "lease_until"
    args: list[Any] = [
        limit,
        float(settings.pg_delivery_lease_seconds),
        settings.pg_delivery_max_attempts,
    ]
    if scope is not None:
        where += " AND org = $4 AND namespace = $5"
        order = "seq"
        args += list(scope)
    rows = await pg.fetch(
        f"""
        WITH due AS (
            SELECT org, namespace, seq, held, {order} AS claim_order,
                   {pg.LEASE_LEFT_S} AS replaced_lease_s,
                   attempts + held::int >= $3
                   AND (targets & {SEARCHED} <> {SEARCH_DOC} OR (held AND attempts >= $3)) AS parks
            FROM loom.memory_deliveries d
            WHERE {where}
            ORDER BY {order}
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        , claimed AS (
        UPDATE loom.memory_deliveries d
        SET lease_token = d.lease_token + 1,
            attempts = d.attempts + d.held::int,
            held = false,
            -- Parked: the last holder's send may still land within its lease.
            fenced_until = CASE WHEN due.parks THEN GREATEST(d.fenced_until, d.lease_until)
                                ELSE d.fenced_until END,
            lease_until = CASE WHEN due.parks THEN 'infinity'::timestamptz
                               ELSE now() + make_interval(secs => $2) END,
            last_error = CASE WHEN d.held THEN 'stopped mid-publish' ELSE d.last_error END
        FROM due, loom.memory_revisions r
        WHERE (d.org, d.namespace, d.seq) = (due.org, due.namespace, due.seq)
          AND (r.org, r.namespace, r.seq) = (d.org, d.namespace, d.seq)
        RETURNING d.org, d.namespace, d.seq, d.targets, d.lease_token, d.attempts,
                  due.parks AS parked, due.held AS taken_over, r.row, r.audit,
                  due.claim_order, due.replaced_lease_s
        )
        -- An UPDATE returns its rows in no order: the claims come back in the order taken.
        SELECT * FROM claimed ORDER BY claim_order, org, namespace, seq
        """,
        *args,
    )
    for r in rows:
        if r["parked"]:
            log.error(
                "pg_delivery: %s/%s seq %s parked after %s attempts%s",
                r["org"],
                r["namespace"],
                r["seq"],
                r["attempts"],
                ", the last a publish that stopped the worker" if r["taken_over"] else "",
            )
    return [
        Claimed(
            org=r["org"],
            namespace=r["namespace"],
            seq=int(r["seq"]),
            targets=int(r["targets"]),
            lease_token=int(r["lease_token"]),
            attempts=int(r["attempts"]),
            row=decode_row(r["row"]),
            audit=decode_audit(r["audit"]),
            deadline=lease_deadline(settings.pg_delivery_lease_seconds, started),
            taken_over=bool(r["taken_over"]),
            replaced_lease_s=float(r["replaced_lease_s"]),
        )
        for r in rows
        if not r["parked"]
    ]


async def defer_audits(claims: Sequence[Claimed], bound_s: float) -> None:
    """Move these claims' audit rows to the outbox, each due `bound_s` past the lease its
    claim replaced (until then the replaced holder's own insert of it may still land, which
    the outbox's read then finds), and clear their AUDIT_ROW, in one transaction."""
    seqs = [(c.org, c.namespace, c.seq) for c in claims]
    keys = _held_keys(seqs, [c.lease_token for c in claims])
    async with pg.transaction() as conn:
        # Locked before they are read, so a fence (a new token) cannot fall between the
        # move and the clear: only a delivery the claim still holds moves.
        await _lock_held(conn, keys)
        await conn.execute(
            "INSERT INTO loom.audit_outbox (org, namespace, row_hash, audit, due_at) "
            "SELECT r.org, r.namespace, r.audit->>'row_hash', r.audit, "
            "  now() + make_interval(secs => greatest(k.wait, 0)) "
            "FROM unnest($1::text[], $2::text[], $3::bigint[], $4::bigint[], $5::float8[]) "
            "  AS k(org, namespace, seq, token, wait) "
            "JOIN loom.memory_revisions r USING (org, namespace, seq) "
            f"JOIN loom.memory_deliveries d ON {_HELD_MATCH} "
            "ON CONFLICT DO NOTHING",
            *keys,
            [c.replaced_lease_s + bound_s for c in claims],
        )
        await _complete_in(conn, seqs, AUDIT_ROW, [c.lease_token for c in claims])


# The deliveries ($1..$4: org, namespace, seq, lease token) a claimer still holds under
# their tokens, locked by seq as a commit, a redaction and an erasure lock them.
_MINE = (
    "WITH mine AS ("
    "  SELECT d.org, d.namespace, d.seq, d.targets FROM loom.memory_deliveries d "
    "  JOIN unnest($1::text[], $2::text[], $3::bigint[], $4::bigint[]) "
    "    AS k(org, namespace, seq, token) "
    "    ON (d.org, d.namespace, d.seq, d.lease_token) = (k.org, k.namespace, k.seq, k.token) "
    "  WHERE {where} "
    "  ORDER BY d.org, d.namespace, d.seq FOR UPDATE OF d) "
)


async def release(claims: Sequence[Claimed]) -> None:
    """Forget that these claims' publishes started (a pass that stopped, or one cut
    short): taken over again, they count no attempt. Their leases run out as they are,
    since an insert may still be in flight. A settled or taken-over one is left alone."""
    if not claims:
        return
    await pg.execute(
        _MINE.format(where="d.held AND NOT d.settled")
        + "UPDATE loom.memory_deliveries d SET held = false "
        "FROM mine WHERE (d.org, d.namespace, d.seq) = (mine.org, mine.namespace, mine.seq)",
        [c.org for c in claims],
        [c.namespace for c in claims],
        [c.seq for c in claims],
        [c.lease_token for c in claims],
    )


async def renew(claims: Sequence[Claimed]) -> list[Claimed]:
    """These claims with a full lease again, under their own tokens, just before their
    publish, and with what they still owe: the ones still owing a target (another holder
    may have taken one over, an erasure deleted it, or a redaction fenced it). Marks their
    publishes started (`held`) until an outcome is recorded or they are released. Locked
    by seq, as a commit, a redaction and an erasure lock them."""
    if not claims:
        return []
    started = time.monotonic()
    rows = await pg.fetch(
        _MINE.format(where="NOT d.settled") + "UPDATE loom.memory_deliveries d "
        "SET lease_until = now() + make_interval(secs => $5), held = true "
        "FROM mine WHERE (d.org, d.namespace, d.seq) = (mine.org, mine.namespace, mine.seq) "
        "RETURNING d.org, d.namespace, d.seq, d.targets",
        [c.org for c in claims],
        [c.namespace for c in claims],
        [c.seq for c in claims],
        [c.lease_token for c in claims],
        float(settings.pg_delivery_lease_seconds),
    )
    owed = {(r["org"], r["namespace"], int(r["seq"])): int(r["targets"]) for r in rows}
    deadline = lease_deadline(settings.pg_delivery_lease_seconds, started)
    return [
        replace(c, targets=owed[c.org, c.namespace, c.seq], deadline=deadline)
        for c in claims
        if (c.org, c.namespace, c.seq) in owed
    ]


async def parked_count() -> int:
    """Deliveries past LOOM_PG_DELIVERY_MAX_ATTEMPTS: revisions ClickHouse keeps refusing
    (or, owing only DECISION_RECORD or OLD_VECTORS, a retirement), left out of claims and
    watermarks; an operator reads `last_error`."""
    return int(
        await pg.fetchval(
            # Parked is an endless lease (`claim_due`, `record_failure`) on a delivery still
            # owing a target, on the claims' index.
            "SELECT count(*) FROM loom.memory_deliveries "
            "WHERE lease_until = 'infinity' AND NOT settled"
        )
    )


async def sweep_settled(limit: int) -> int:
    """Delete up to `limit` settled deliveries (`complete`) whose lease and fence
    (`fenced_until`) are both older than the insert bound: no publisher they replaced can
    still land a row."""
    n = await pg.fetchval(
        "WITH gone AS (DELETE FROM loom.memory_deliveries WHERE ctid IN ("
        "  SELECT ctid FROM loom.memory_deliveries "
        "  WHERE lease_until < now() - make_interval(secs => $1) AND settled "
        "  AND (fenced_until IS NULL OR fenced_until < now() - make_interval(secs => $1)) "
        "  ORDER BY lease_until LIMIT $2 FOR UPDATE SKIP LOCKED) RETURNING 1) "
        "SELECT count(*) FROM gone",
        pg.insert_bound_s(),
        limit,
    )
    return int(n)


async def parked_audits_count() -> int:
    """Outbox audit rows past LOOM_PG_DELIVERY_MAX_ATTEMPTS (`fail_outbox`); an operator
    reads `last_error`."""
    return int(
        await pg.fetchval("SELECT count(*) FROM loom.audit_outbox WHERE due_at = 'infinity'")
    )


async def claim_outbox(limit: int) -> tuple[list[dict[str, Any]], float]:
    """Take up to `limit` audit rows of the outbox past their `due_at`, of the namespace
    whose unclaimed row is due first, each pushed out by the claim's bound: its dedup read, then
    an insert that lands within the insert bound. Returns them with the monotonic time
    their read and insert must start by, so no later claimer finds them unpublished."""
    started = time.monotonic()
    # 0 is no bound on a read: the claim allows it the insert bound.
    read_s = float(settings.ch_timeout_read_s) or pg.insert_bound_s()
    rows = await pg.fetch(
        "WITH first AS (SELECT org, namespace FROM loom.audit_outbox "
        "  WHERE due_at <= now() ORDER BY due_at LIMIT 1 FOR UPDATE SKIP LOCKED), "
        "due AS (SELECT o.org, o.namespace, o.row_hash FROM loom.audit_outbox o "
        "  JOIN first USING (org, namespace) WHERE o.due_at <= now() "
        "  ORDER BY o.due_at LIMIT $1 FOR UPDATE OF o SKIP LOCKED) "
        "UPDATE loom.audit_outbox o SET due_at = now() + make_interval(secs => $2) FROM due "
        "WHERE (o.org, o.namespace, o.row_hash) = (due.org, due.namespace, due.row_hash) "
        "RETURNING o.audit",
        limit,
        read_s + pg.insert_bound_s(),
    )
    return [decode_audit(r["audit"]) for r in rows], started + read_s


def _outbox_keys(audits: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    return [
        [str(a["org"]) for a in audits],
        [str(a["namespace"]) for a in audits],
        [str(a["row_hash"]) for a in audits],
    ]


async def drop_outbox(audits: Sequence[Mapping[str, Any]]) -> None:
    """Delete these published audit rows from the outbox."""
    await pg.execute(
        "DELETE FROM loom.audit_outbox o "
        "USING unnest($1::text[], $2::text[], $3::text[]) AS k(org, namespace, row_hash) "
        "WHERE (o.org, o.namespace, o.row_hash) = (k.org, k.namespace, k.row_hash)",
        *_outbox_keys(audits),
    )


async def fail_outbox(audits: Sequence[Mapping[str, Any]], error: BaseException) -> None:
    """Count a refused publish on these outbox rows, stored as the error's type and code
    (`error_summary`); one out of LOOM_PG_DELIVERY_MAX_ATTEMPTS is parked."""
    await pg.execute(
        "UPDATE loom.audit_outbox o SET attempts = o.attempts + 1, last_error = $4, "
        "due_at = CASE WHEN o.attempts + 1 >= $5 THEN 'infinity'::timestamptz ELSE o.due_at END "
        "FROM unnest($1::text[], $2::text[], $3::text[]) AS k(org, namespace, row_hash) "
        "WHERE (o.org, o.namespace, o.row_hash) = (k.org, k.namespace, k.row_hash)",
        *_outbox_keys(audits),
        error_summary(error),
        settings.pg_delivery_max_attempts,
    )


async def sweep_erasure_evidence(limit: int) -> int:
    """Delete up to `limit` erased memories' ids older than LOOM_PG_ERASURE_EVIDENCE_HOURS."""
    n = await pg.fetchval(
        "WITH gone AS (DELETE FROM loom.erased_memories WHERE ctid IN ("
        "  SELECT ctid FROM loom.erased_memories "
        "  WHERE erased_at < now() - make_interval(hours => $1) "
        "  ORDER BY erased_at LIMIT $2 FOR UPDATE SKIP LOCKED) RETURNING 1) "
        "SELECT count(*) FROM gone",
        settings.pg_erasure_evidence_hours,
        limit,
    )
    return int(n)


async def record_failure(
    claims: Sequence[Claimed], error: BaseException, *, park: bool = False
) -> int:
    """Count a failed attempt on the deliveries these claims still owe a target, with
    the error's type and code (`error_summary`), never its text, and return how many.
    `park`: no claim takes it again; the failure's time moves to `fenced_until`, which
    a wait still covers (`pg.LEASE_LEFT_S`)."""
    if not claims:
        return 0
    done = await pg.execute(
        # A settled delivery is never parked out of its sweep.
        _MINE.format(where="NOT d.settled")
        + "UPDATE loom.memory_deliveries d SET attempts = d.attempts + 1, held = false, "
        "last_error = $5, "
        # Parked: the send that failed started before now, so the insert bound from now
        # covers it.
        "fenced_until = CASE WHEN $6 THEN GREATEST(d.fenced_until, LEAST(d.lease_until, now())) "
        "ELSE d.fenced_until END, "
        "lease_until = CASE WHEN $6 THEN 'infinity'::timestamptz ELSE d.lease_until END "
        "FROM mine WHERE (d.org, d.namespace, d.seq) = (mine.org, mine.namespace, mine.seq)",
        [c.org for c in claims],
        [c.namespace for c in claims],
        [c.seq for c in claims],
        [c.lease_token for c in claims],
        error_summary(error),
        park,
    )
    return int(done.split()[-1])


# ---- reads -------------------------------------------------------------------


def _scope_sql(scope: Any, args: list[Any]) -> str:
    """The read scope (`identity.domain.read_scope`) as SQL over memory_current."""
    if not scope.narrowed:
        return ""
    args.append(scope.user_id)
    sql = f" AND user_id = ${len(args)}"
    if scope.scope == "own_session":
        args.append(scope.session_id)
        sql += f" AND session_id = ${len(args)}"
    return sql


# A head past its `expires_at`; false for one with none.
_EXPIRED = "coalesce((row->>'expires_at')::timestamp <= (now() AT TIME ZONE 'UTC'), false)"


def _live_sql(include_tombstoned: bool, include_expired: bool) -> str:
    sql = ""
    if not include_tombstoned:
        sql += " AND row->>'tombstoned_at' IS NULL"
    if not include_expired:
        sql += f" AND NOT {_EXPIRED}"
    return sql


async def key_heads_across_sessions(
    org: str, namespace: str, agent: str, user_id: str, memory_keys: Sequence[str], *, scope: Any
) -> list[dict[str, Any]]:
    """`reads.key_heads_across_sessions` from the authority: the newest head of each key
    across the identity's sessions in `scope`, tombstones and every status included."""
    keys = sorted({k for k in memory_keys if not pg.unstorable(k)})
    if (
        not keys
        or pg.unstorable(org)
        or pg.unstorable(namespace)
        or pg.unstorable([agent, user_id])
    ):
        return []
    args: list[Any] = [org, namespace, agent, user_id, keys]
    rows = await pg.fetch(
        "SELECT DISTINCT ON (memory_key) memory_key, "
        "row->>'tombstoned_at' IS NOT NULL AS deleted, "
        "coalesce(row->>'status', 'active') AS head_status, content_sha256 AS head_sha256, "
        "coalesce(row->>'sensitivity', '') AS head_sensitivity "
        "FROM loom.memory_current WHERE org = $1 AND namespace = $2 AND agent = $3 "
        f"AND user_id = $4 AND memory_key = ANY($5::text[]){_scope_sql(scope, args)} "
        "ORDER BY memory_key, version DESC",
        *args,
    )
    return [dict(r) for r in rows]


async def head_by_key(
    key: IdentityKey,
    *,
    scope: Any,
    include_tombstoned: bool = False,
    include_expired: bool = True,
) -> dict[str, Any] | None:
    """The head of one key, or None. An agent of '*' reads the namespace's newest
    head of the key under any agent, as `memory_repo.get_by_key` does."""
    if pg.unstorable(key):
        return None  # Postgres holds no such text, so no such key
    org, ns, agent, uid, sid, memory_key = key
    args: list[Any] = [org, ns, memory_key]
    where = "org = $1 AND namespace = $2 AND memory_key = $3"
    if agent != "*":
        args += [agent, uid, sid]
        where += " AND agent = $4 AND user_id = $5 AND session_id = $6"
    where += _scope_sql(scope, args) + _live_sql(include_tombstoned, include_expired)
    row = await pg.fetchrow(
        f"SELECT row FROM loom.memory_current WHERE {where} "
        "ORDER BY version DESC, row->>'created_at' DESC LIMIT 1",
        *args,
    )
    return decode_row(row["row"]) if row else None


async def heads_for_identity(
    identity: tuple[str, str, str, str, str],
    memory_keys: Sequence[str],
    *,
    scope: Any,
    exclude_high_sensitivity: bool = False,
    retained_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """The live head of each key, by memory_key, with the identity's axes opened as
    `memory_repo.get_many_by_keys` opens them: '*' opens an axis, and under the wildcard
    agent so does a user or session of '-'. Where several identities hold a key, the
    newest head answers; `retained_only` leaves out a 'high' head past its retention
    window first, as ClickHouse's bulk read does."""
    memory_keys = [k for k in memory_keys if not pg.unstorable(k)]
    if not memory_keys or pg.unstorable(identity):
        return {}  # Postgres holds no such text, so no such key
    org, ns, agent, uid, sid = identity
    args: list[Any] = [org, ns, memory_keys]
    where = "org = $1 AND namespace = $2 AND memory_key = ANY($3::text[])"
    for column, value in (("agent", agent), ("user_id", uid), ("session_id", sid)):
        if value == "*" or (agent == "*" and value == "-"):
            continue
        args.append(value)
        where += f" AND {column} = ${len(args)}"
    where += _scope_sql(scope, args) + _live_sql(include_tombstoned=False, include_expired=True)
    if exclude_high_sensitivity:
        where += " AND row->>'sensitivity' IS DISTINCT FROM 'high'"
    if retained_only:
        where += f" AND NOT (row->>'sensitivity' IS NOT DISTINCT FROM 'high' AND {_EXPIRED})"
    rows = await pg.fetch(
        "SELECT DISTINCT ON (memory_key) memory_key, row FROM loom.memory_current "
        f"WHERE {where} ORDER BY memory_key, version DESC, row->>'created_at' DESC",
        *args,
    )
    return {r["memory_key"]: decode_row(r["row"]) for r in rows}


async def head_versions(keys: Sequence[IdentityKey]) -> dict[IdentityKey, int]:
    """The head version of each exact key, a deleted head's too; a key with none (erased)
    is absent."""
    keys = [k for k in keys if not pg.unstorable(k)]
    if not keys:
        return {}
    rows = await pg.fetch(_HEAD_VERSIONS, *_key_arrays(keys))
    return {identity_key(r): int(r["version"]) for r in rows}


async def settled_head_versions(keys: Sequence[IdentityKey]) -> dict[IdentityKey, int]:
    """`head_versions` once no commit still holds a head's row: a COMMIT waiting on the
    synchronous standby keeps its rows locked and is not yet visible to a plain read. The
    heads are locked in the commit's order and the read rolled back. A key with no head is
    absent, a first write still in flight included. A head's lock wait past
    `pg_commit_lock_timeout_ms` raises."""
    keys = [k for k in keys if not pg.unstorable(k)]
    if not keys:
        return {}
    async with pg.rolled_back() as conn:
        await _bound_lock_waits(conn)
        rows = await conn.fetch(_SETTLED_HEAD_VERSIONS, *_key_arrays(keys))
    return {identity_key(r): int(r["version"]) for r in rows}


async def heads_by_keys(
    keys: Sequence[IdentityKey], *, include_tombstoned: bool = False
) -> dict[IdentityKey, dict[str, Any]]:
    """The heads, expired ones included, of exact keys (a deleted one too with
    `include_tombstoned`); a key with none is absent."""
    keys = [k for k in keys if not pg.unstorable(k)]
    if not keys:
        return {}
    sql = _HEADS_BY_KEYS
    if not include_tombstoned:
        sql += " WHERE c.row->>'tombstoned_at' IS NULL"
    rows = await pg.fetch(sql, *_key_arrays(keys))
    return {identity_key(r["row"]): decode_row(r["row"]) for r in rows}


async def heads_owing(keys: Sequence[IdentityKey]) -> dict[IdentityKey, tuple[dict[str, Any], int]]:
    """The heads of exact keys, deleted ones included, each with the targets its own
    delivery still owes (0 once settled); a key with none is absent."""
    keys = [k for k in keys if not pg.unstorable(k)]
    if not keys:
        return {}
    rows = await pg.fetch(_HEADS_OWING, *_key_arrays(keys))
    return {identity_key(r["row"]): (decode_row(r["row"]), int(r["owed"])) for r in rows}


async def head_by_id(
    memory_id: str,
    *,
    scope: Any,
    org: str,
    namespace: str,
    include_tombstoned: bool = False,
) -> dict[str, Any] | None:
    """The head of (`org`, `namespace`) that carries `memory_id`, or None."""
    try:
        mid = UUID(memory_id)
    except ValueError:
        return None
    if pg.unstorable(org) or pg.unstorable(namespace):
        return None
    args: list[Any] = [mid, org, namespace]
    where = "memory_id = $1 AND org = $2 AND namespace = $3"
    where += _scope_sql(scope, args) + _live_sql(include_tombstoned, include_expired=True)
    row = await pg.fetchrow(
        f"SELECT row FROM loom.memory_current WHERE {where} ORDER BY version DESC LIMIT 1", *args
    )
    return decode_row(row["row"]) if row else None


async def erased_ids(org: str, namespace: str, memory_ids: Sequence[str]) -> set[str]:
    """Which of these memories (canonical ids) an erasure removed from (org, namespace)
    while their deletion evidence is kept."""
    ids = [UUID(i) for i in memory_ids]
    if not ids or pg.unstorable(org) or pg.unstorable(namespace):
        return set()
    rows = await pg.fetch(
        "SELECT memory_id FROM loom.erased_memories "
        "WHERE org = $1 AND namespace = $2 AND memory_id = ANY($3::uuid[]) "
        "AND erased_at > now() - make_interval(hours => $4)",
        org,
        namespace,
        ids,
        settings.pg_erasure_evidence_hours,
    )
    return {str(r["memory_id"]) for r in rows}


async def live_decision_writers(
    org: str, namespace: str, keys: Sequence[str]
) -> list[tuple[str, str, str, str]]:
    """(memory_key, agent, user_id, session_id) of each live decision head of these keys."""
    keys = [k for k in keys if not pg.unstorable(k)]  # Postgres holds no such key
    if not keys or pg.unstorable(org) or pg.unstorable(namespace):
        return []
    rows = await pg.fetch(
        "SELECT memory_key, agent, user_id, session_id FROM loom.memory_current "
        "WHERE org = $1 AND namespace = $2 AND memory_key = ANY($3::text[]) "
        "AND row->>'memory_type' = 'decision' AND row->>'tombstoned_at' IS NULL",
        org,
        namespace,
        keys,
    )
    return [(r["memory_key"], r["agent"], r["user_id"], r["session_id"]) for r in rows]


async def profile_keys_built_from(
    org: str, namespace: str, user_id: str, memory_id: str
) -> list[dict[str, Any]]:
    """The user's profile keys with a revision built from `memory_id`, redacted or not,
    each with the newest such version: what a redaction of it reaches, delivered to
    ClickHouse or not (`redaction.profile_keys_built_from`)."""
    try:
        mid = UUID(memory_id)
    except ValueError:
        return []
    if pg.unstorable(org) or pg.unstorable(namespace) or pg.unstorable(user_id):
        return []
    rows = await pg.fetch(
        "SELECT row->>'agent' AS agent, row->>'session_id' AS session_id, "
        f"row->>'memory_key' AS memory_key, max(version) AS newest {_PROFILE_REVISIONS} "
        "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
        org,
        namespace,
        user_id,
        mid,
    )
    return [{**dict(r), "newest": int(r["newest"])} for r in rows]


# A revision's metadata column, or for one written before the columns existed (NULL
# there) its payload's field, with the loom.memory column's default
# (`003_revision_metadata.sql`). Read as `_meta(col)`, or `_meta(col, "r")` aliased.
_META_DEFAULTS = {
    "agent": "''",
    "session_id": "''",
    "memory_key": "''",
    "status": "'active'",
    "redaction_reason": "''",
}


def _meta(col: str, alias: str = "") -> str:
    """The journal's `col` for every revision, its typed column or its payload's field."""
    a = f"{alias}." if alias else ""
    if col == "redacted_at":
        return f"coalesce({a}redacted_at, {a}row->>'redacted_at')"
    return f"coalesce({a}{col}, {a}row->>'{col}', {_META_DEFAULTS[col]})"


async def history_target(org: str, memory_id: str, version: int) -> dict[str, Any] | None:
    """The revision `(memory_id, version)`, or None: the key a redaction of it acts on,
    delivered to ClickHouse or not. A namespace restored from another, or into itself
    under another agent, keeps the id under other keys and fresh versions, so the version
    names one row."""
    try:
        mid = UUID(memory_id)
    except ValueError:
        return None
    if pg.unstorable(org):
        return None
    r = await pg.fetchrow(
        f"SELECT org, memory_id::text AS memory_id, namespace, {_meta('agent')} AS agent, "
        f"user_id, {_meta('session_id')} AS session_id, {_meta('memory_key')} AS memory_key, "
        "version, content_sha256 "
        "FROM loom.memory_revisions WHERE org = $1 AND memory_id = $2 AND version = $3 LIMIT 1",
        org,
        mid,
        version,
    )
    return None if r is None else dict(r)


async def status_before(key: IdentityKey, memory_id: str, version: int) -> str:
    """`history.status_before` on the authority's revisions of `key` holding `memory_id`."""
    org, namespace, agent, user_id, session_id, memory_key = key
    status = await pg.fetchval(
        f"SELECT {_meta('status')} FROM loom.memory_revisions WHERE org = $1 "
        f"AND namespace = $2 AND {_meta('agent')} = $3 AND user_id = $4 "
        f"AND {_meta('session_id')} = $5 AND {_meta('memory_key')} = $6 AND memory_id = $7 "
        f"AND version < $8 AND {_meta('status')} <> 'superseded' ORDER BY version DESC LIMIT 1",
        org,
        namespace,
        agent,
        user_id,
        session_id,
        memory_key,
        UUID(memory_id),
        version,
    )
    return status or ""


async def revisions_at(
    org: str, namespace: str, read: Sequence[MemoryRow]
) -> dict[tuple[str, int], MemoryRow]:
    """These versions of `namespace`, read from ClickHouse's history, as the authority
    holds them, by `(memory_id, version)`: a redaction scrubs the journal before
    ClickHouse's copies, and an erasure removes the revision. A revision whose payload was
    cleared after its delivery serves ClickHouse's row of the same key, scrubbed when the
    journal says it is redacted."""
    by_version = {(str(r.memory_id), int(r.version)): r for r in read}
    if not by_version or pg.unstorable([org, namespace]):
        return {}
    rows = await pg.fetch(
        f"SELECT r.memory_id::text AS memory_id, r.version, r.row, {_meta('agent', 'r')} AS agent, "
        f"r.user_id, {_meta('session_id', 'r')} AS session_id, "
        f"{_meta('memory_key', 'r')} AS memory_key, {_meta('redacted_at', 'r')} AS redacted_at, "
        f"{_meta('redaction_reason', 'r')} AS redaction_reason "
        "FROM loom.memory_revisions r "
        "JOIN unnest($3::uuid[], $4::bigint[]) AS w(memory_id, version) "
        "ON r.memory_id = w.memory_id AND r.version = w.version "
        "WHERE r.org = $1 AND r.namespace = $2",
        org,
        namespace,
        [UUID(m) for m, _v in by_version],
        [v for _m, v in by_version],
    )
    out: dict[tuple[str, int], MemoryRow] = {}
    for r in rows:
        at = (r["memory_id"], int(r["version"]))
        if r["row"] is not None:
            out[at] = _hydrate(decode_row(r["row"]))
            continue
        copy = by_version[at]
        if (copy.agent, copy.user_id, copy.session_id, copy.memory_key) != (
            r["agent"],
            r["user_id"],
            r["session_id"],
            r["memory_key"],
        ):
            continue
        out[at] = copy if r["redacted_at"] is None else _scrubbed(copy, r)
    return out


def _scrubbed(row: MemoryRow, journal: Mapping[str, Any]) -> MemoryRow:
    """`row` with the text a redaction clears (`_SCRUB_VALUES`) cleared, and the
    redaction's time and reason from its revision's columns."""
    return row.model_copy(
        update={
            "value": parse_memory_value(REDACTED_VALUE),
            "value_text": REDACTED_VALUE,
            "value_json": "{}",
            "evidence": "",
            "entities": [],
            "concepts": [],
            "redacted_at": datetime.fromisoformat(journal["redacted_at"]),
            "redaction_reason": journal["redaction_reason"],
        }
    )


async def namespaces_of_id(org: str, memory_id: str) -> list[str]:
    """The namespaces of `org` whose live head carries `memory_id`."""
    try:
        mid = UUID(memory_id)
    except ValueError:
        return []
    if pg.unstorable(org):
        return []
    rows = await pg.fetch(
        "SELECT namespace FROM loom.memory_current WHERE org = $1 AND memory_id = $2"
        + _live_sql(include_tombstoned=False, include_expired=True)
        + " GROUP BY namespace ORDER BY namespace",
        org,
        mid,
    )
    return [str(r["namespace"]) for r in rows]


# A head search may show: not rejected, and not labelled 'high' (no document of it is
# served, whatever ClickHouse still holds).
_SERVABLE = (
    " AND coalesce(c.row->>'status', 'active') <> 'rejected'"
    " AND coalesce(c.row->>'sensitivity', '') <> 'high'"
    # A scrubbed head keeps its hash, so a search document of its old text would still
    # match it.
    " AND c.row->>'redacted_at' IS NULL"
)


async def chunk_heads(
    org: str, ids: Sequence[tuple[str, str, str, str]], *, servable: bool = True
) -> dict[tuple[str, str, str, str], list[tuple[Any, str]]]:
    """For these (namespace, agent, user_id, memory_key), the (value, memory_type) of each
    live head under any session (by default only the unexpired ones search serves): what a
    chunk of that writer is checked by."""
    if not ids:
        return {}
    if servable:
        live = _live_sql(include_tombstoned=False, include_expired=False) + _SERVABLE
    else:
        live = _live_sql(include_tombstoned=False, include_expired=True)
    rows = await pg.fetch(
        "SELECT h.namespace, h.agent, h.user_id, h.memory_key, "
        "c.row->>'value' AS value, c.row->>'memory_type' AS memory_type "
        "FROM unnest($2::text[], $3::text[], $4::text[], $5::text[]) "
        "AS h(namespace, agent, user_id, memory_key) "
        "JOIN loom.memory_current c ON c.org = $1 AND c.namespace = h.namespace "
        "AND c.memory_key = h.memory_key AND c.agent = h.agent AND c.user_id = h.user_id" + live,
        org,
        *[[i[k] for i in ids] for k in range(4)],
    )
    out: dict[tuple[str, str, str, str], list[tuple[Any, str]]] = {}
    for r in rows:
        key = (r["namespace"], r["agent"], r["user_id"], r["memory_key"])
        value = parse_memory_value(r["value"])  # as the writer cut it into chunks
        out.setdefault(key, []).append((value, str(r["memory_type"] or "")))
    return out


async def live_versions(
    org: str, ids: Sequence[tuple[str, str, str, str, str, str, int]]
) -> set[tuple[str, str, str, str, str, str, int]]:
    """Of these (namespace, agent, user_id, session_id, memory_key, memory_type, version)
    of search documents, the ones whose key has a live head of that type ('': any)
    showing the document's content: the revision a document was built from is the newest
    at or below its version (a writer lifts a document's version above a delete's), and
    a change of tags, confidence or expiry writes a revision with the same content. A
    version of 0 (a leg that did not read it) matches no revision: refused."""
    if not ids:
        return set()
    rows = await pg.fetch(
        "SELECT h.namespace, h.agent, h.user_id, h.session_id, h.memory_key, h.memory_type, "
        "h.version FROM unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::text[], "
        "$7::text[], $8::bigint[]) "
        "AS h(namespace, agent, user_id, session_id, memory_key, memory_type, version) "
        "JOIN loom.memory_current c ON c.org = $1 AND c.namespace = h.namespace "
        "AND c.agent = h.agent AND c.user_id = h.user_id AND c.session_id = h.session_id "
        "AND c.memory_key = h.memory_key"
        f"{_live_sql(include_tombstoned=False, include_expired=False)}{_SERVABLE} "
        "AND (h.memory_type = '' OR c.row->>'memory_type' = h.memory_type) "
        "WHERE c.content_sha256 = (SELECT r.content_sha256 "
        "  FROM loom.memory_revisions r WHERE r.org = c.org AND r.memory_id = c.memory_id "
        "  AND r.namespace = c.namespace AND r.user_id = c.user_id "
        # The id may sit under another key too (a namespace restored into itself).
        f"  AND {_meta('agent', 'r')} = c.agent AND {_meta('session_id', 'r')} = c.session_id "
        f"  AND {_meta('memory_key', 'r')} = c.memory_key "
        "  AND r.version <= h.version ORDER BY r.version DESC LIMIT 1)",
        org,
        *[[i[k] for i in ids] for k in range(6)],
        [int(i[6]) for i in ids],
    )
    return {
        (
            r["namespace"],
            r["agent"],
            r["user_id"],
            r["session_id"],
            r["memory_key"],
            r["memory_type"],
            int(r["version"]),
        )
        for r in rows
    }


async def fold_usage(
    targets: Sequence[tuple[str, str, str, int, int, datetime | None]],
) -> None:
    """The usage fold's counters on the heads, as it folds them onto loom.memory:
    (org, namespace, memory_id, evidence_count, recall_count, last_recalled_at), each raised
    and never lowered, the version kept. A later revision built from the head
    carries them, as it does in ClickHouse. The heads are locked in key order, as a
    commit locks them. An org or namespace Postgres cannot store holds no head."""
    targets = [t for t in targets if not pg.unstorable(t[:2])]
    if not targets:
        return
    await pg.execute(
        f"""
        WITH locked AS (
            SELECT c.org, c.namespace, c.agent, c.user_id, c.session_id, c.memory_key,
                   u.ev, u.rc, u.lra
            FROM loom.memory_current c
            JOIN unnest($1::text[], $2::text[], $3::uuid[], $4::bigint[], $5::bigint[],
                        $6::text[])
                AS u(org, namespace, memory_id, ev, rc, lra)
              ON c.org = u.org AND c.namespace = u.namespace AND c.memory_id = u.memory_id
            WHERE c.row->>'tombstoned_at' IS NULL
            {_KEY_ORDER}
            FOR UPDATE OF c
        )
        UPDATE loom.memory_current c SET row = c.row || jsonb_build_object(
            'evidence_count', greatest(coalesce((c.row->>'evidence_count')::bigint, 1), l.ev),
            'recall_count', greatest((c.row->>'recall_count')::bigint, l.rc),
            'last_recalled_at', CASE
                WHEN l.lra IS NULL THEN c.row->'last_recalled_at'
                WHEN c.row->>'last_recalled_at' IS NULL THEN to_jsonb(l.lra)
                ELSE to_jsonb(greatest(c.row->>'last_recalled_at', l.lra)) END)
        FROM locked l
        WHERE (c.org, c.namespace, c.agent, c.user_id, c.session_id, c.memory_key)
            = (l.org, l.namespace, l.agent, l.user_id, l.session_id, l.memory_key)
        """,
        [t[0] for t in targets],
        [t[1] for t in targets],
        [UUID(str(t[2])) for t in targets],
        [int(t[3]) for t in targets],
        [int(t[4]) for t in targets],
        [to_naive_utc_second(t[5]).isoformat() if t[5] is not None else None for t in targets],
    )


async def live_heads_in(
    org: str,
    namespace: str,
    *,
    user_id: str | None = None,
    agent: str | None = None,
    after: IdentityKey | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """A page of a namespace's live heads (of one user and/or one agent when given) in
    key order, the next after the key `after`."""
    if pg.unstorable((org, namespace, user_id or "", agent or "")):
        return []  # no head holds what Postgres cannot store
    args: list[Any] = [org, namespace, limit]
    where = "org = $1 AND namespace = $2 AND row->>'tombstoned_at' IS NULL"
    if user_id is not None:
        args.append(user_id)
        where += f" AND user_id = ${len(args)}"
    if agent is not None:
        args.append(agent)
        where += f" AND agent = ${len(args)}"
    if after is not None:
        args += list(after[2:])
        n = len(args)
        where += (
            f" AND (agent, user_id, session_id, memory_key) > (${n - 3}, ${n - 2}, ${n - 1}, ${n})"
        )
    rows = await pg.fetch(
        f"SELECT row FROM loom.memory_current WHERE {where} "
        "ORDER BY org, namespace, agent, user_id, session_id, memory_key LIMIT $3",
        *args,
    )
    return [decode_row(r["row"]) for r in rows]


async def unpublished_keepers(
    org: str, namespace: str, agent: str, user_id: str, shas: Sequence[str], *, scope: Any
) -> list[dict[str, Any]]:
    """The keys of one identity (any session) committed with one of the content hashes
    `shas` whose memory row ClickHouse is still owed, in `scope`: the keepers an identity
    dedup that reads ClickHouse cannot see yet (a write answered at its commit).
    `head_sha`, `session_id`, `memory_key` and `memory_id`; the caller still checks each
    against its head. Read through the namespace's deliveries still owing the row; a
    parked one (ClickHouse kept refusing it) is left out, and the connection's statement
    timeout bounds the read."""
    if not shas or pg.unstorable((org, namespace, agent, user_id)):
        return []
    args: list[Any] = [org, namespace, agent, user_id, list(shas), MEMORY_ROW]
    scoped = ""
    if scope.narrowed:
        args.append(scope.user_id)
        scoped += f" AND r.user_id = ${len(args)}"
        if scope.scope == "own_session":
            args.append(scope.session_id)
            scoped += f" AND {_meta('session_id', 'r')} = ${len(args)}"
    rows = await pg.fetch(
        "SELECT DISTINCT r.content_sha256 AS head_sha, "
        f"{_meta('session_id', 'r')} AS session_id, {_meta('memory_key', 'r')} AS memory_key, "
        "r.memory_id::text AS memory_id "
        "FROM loom.memory_deliveries d JOIN loom.memory_revisions r USING (org, namespace, seq) "
        "WHERE d.org = $1 AND d.namespace = $2 AND d.targets & $6 <> 0 "
        "AND d.lease_until <> 'infinity' "
        f"AND r.user_id = $4 AND {_meta('agent', 'r')} = $3 AND r.content_sha256 = ANY($5::text[])"
        f"{scoped}",
        *args,
    )
    return [dict(r) for r in rows]


async def live_keys(org: str, namespace: str, keys: Sequence[str]) -> set[str]:
    """Which of `keys` have a live head in `namespace`, in any identity."""
    wanted = [k for k in keys if not pg.unstorable(k)]  # no head holds one Postgres cannot store
    if not wanted or pg.unstorable((org, namespace)):
        return set()
    rows = await pg.fetch(
        "SELECT DISTINCT memory_key FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
        "AND memory_key = ANY($3::text[]) AND row->>'tombstoned_at' IS NULL",
        org,
        namespace,
        wanted,
    )
    return {str(r["memory_key"]) for r in rows}


async def consolidated_heads(org: str, namespace: str, source_key: str) -> list[dict[str, Any]]:
    """The key and tags of each live consolidated head of `namespace` built from
    `source_key` (`src:<key>`)."""
    if pg.unstorable((org, namespace, source_key)):
        return []
    rows = await pg.fetch(
        "SELECT org, namespace, agent, user_id, session_id, memory_key, row->'tags' AS tags "
        "FROM loom.memory_current WHERE org = $1 AND namespace = $2 AND consolidated "
        "AND row->'tags' ? $3 AND row->>'tombstoned_at' IS NULL",
        org,
        namespace,
        f"src:{source_key}",
    )
    return [dict(r) for r in rows]


async def live_key_heads(
    org: str, namespace: str, keys: Sequence[str], *, scope: Any
) -> list[dict[str, Any]]:
    """The live heads of `keys` in `namespace`, of every identity `scope` may read: what a
    write's same-key read decides on, current at the commit, where ClickHouse may not
    hold a write that answered at it yet."""
    wanted = sorted({k for k in keys if not pg.unstorable(k)})
    if not wanted or pg.unstorable((org, namespace)):
        return []  # no head holds what Postgres cannot store
    args: list[Any] = [org, namespace, wanted]
    rows = await pg.fetch(
        "SELECT row FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
        "AND memory_key = ANY($3::text[]) AND row->>'tombstoned_at' IS NULL"
        f"{_scope_sql(scope, args)}",
        *args,
    )
    return [decode_row(r["row"]) for r in rows]


async def live_heads_of(
    org: str, namespace: str, agent: str, user_id: str, memory_key: str
) -> list[dict[str, Any]]:
    """Every session's live head of one user's key."""
    if pg.unstorable((org, namespace, agent, user_id, memory_key)):
        return []  # no head holds what Postgres cannot store
    rows = await pg.fetch(
        "SELECT row FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
        "AND agent = $3 AND user_id = $4 AND memory_key = $5 "
        "AND row->>'tombstoned_at' IS NULL",
        org,
        namespace,
        agent,
        user_id,
        memory_key,
    )
    return [decode_row(r["row"]) for r in rows]


# The fields a redaction matches on: the heads' key columns and `row` fields, and the
# journal's metadata (`_meta`), which holds whether or not its payload does.
_HEAD_FIELDS = {
    "agent": "agent",
    "session_id": "session_id",
    "memory_key": "memory_key",
    "memory_type": "row->>'memory_type'",
    "source_trace": "row->>'source_trace'",
    "redacted_at": "row->>'redacted_at'",
}
# The key arm's fields (the profile arm reads `_PROFILE_JOURNAL_FIELDS`).
_JOURNAL_FIELDS = {f: _meta(f) for f in ("agent", "session_id", "memory_key", "redacted_at")}
# A profile revision keeps its payload for good, so the journal's profile reach matches
# on `row`, which `memory_revisions_profile` indexes.
_PROFILE_JOURNAL_FIELDS = {f: f"row->>'{f}'" for f in _HEAD_FIELDS}


def _profile_built_from(f: Mapping[str, str]) -> str:
    """A profile row of the user ($3) built from the memory ($4): what a redaction of it
    reaches."""
    return (
        f"user_id = $3 AND {f['agent']} = '-' AND {f['memory_type']} = 'profile' "
        f"AND starts_with({f['source_trace']}, 'profile.fold:') "
        f"AND $4::uuid::text = ANY (regexp_split_to_array({f['source_trace']}, '[:,]'))"
    )


# The user's profile revisions built from the memory, redacted or not.
_PROFILE_REVISIONS = (
    "FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 AND "
    + _profile_built_from(_PROFILE_JOURNAL_FIELDS)
)


# Every statement of `redact` binds a prefix of one layout: $1 org, $2 namespace, $3
# user_id, $4 memory_id, $5 agent, $6 session_id, $7 memory_key, $8 version, $9 the
# copies, $10 the redaction's time, $11 its reason.
def _redacted_in(*, journal: bool, scrubbed_too: bool = False) -> str:
    """The redacted version of the key and the later versions of the key that copy its
    text (`$9`, `redaction_lineage.later_copies`), and the user's profile versions folded
    from the memory: the rows `redaction._REDACTED_ROWS` chooses, in the heads or in the
    journal (`journal`). The named version takes the latest call's time and reason; a copy
    or a profile version already redacted keeps its first, and is left out unless
    `scrubbed_too` (a retry waits out its publishers all the same)."""
    f = _JOURNAL_FIELDS if journal else _HEAD_FIELDS
    p = _PROFILE_JOURNAL_FIELDS if journal else _HEAD_FIELDS
    key = (
        f"user_id = $3 AND {f['agent']} = $5 AND {f['session_id']} = $6 AND {f['memory_key']} = $7"
    )
    unscrubbed = "" if scrubbed_too else f" AND {f['redacted_at']} IS NULL"
    profile_unscrubbed = "" if scrubbed_too else f" AND {p['redacted_at']} IS NULL"
    return (
        f"org = $1 AND namespace = $2 AND (({key} AND memory_id = $4::uuid AND (version = $8 "
        f"OR (version = ANY($9::bigint[]){unscrubbed}))) "
        f"OR ({_profile_built_from(p)}{profile_unscrubbed}))"
    )


_REDACTED_HEADS = _redacted_in(journal=False)
_REDACTED = _redacted_in(journal=True)
_REDACTED_EVER = _redacted_in(journal=True, scrubbed_too=True)
# The cleared value of each column the ClickHouse scrub clears (`redaction._MEMORY_SCRUB`).
_SCRUB_VALUES = {
    "value": f"'{REDACTED_VALUE}'",
    "value_json": "'{}'",
    "evidence": "''",
    "entities": "'[]'::jsonb",
    "concepts": "'[]'::jsonb",
}
_SCRUBBED = (
    "row || jsonb_build_object("
    + "".join(f"'{col}', {value}, " for col, value in _SCRUB_VALUES.items())
    + "'redacted_at', $10::text, 'redaction_reason', $11::text)"
)


async def _bound_lock_waits(conn: Any) -> None:
    """A lock this transaction waits for past `pg_commit_lock_timeout_ms` answers a retryable
    503, so a long erasure does not hold every pooled connection it blocks."""
    await conn.execute(f"SET LOCAL lock_timeout = {int(settings.pg_commit_lock_timeout_ms)}")


async def redact(
    key: IdentityKey,
    memory_id: str,
    version: int,
    *,
    when: datetime,
    reason_blob: str,
    head: tuple[str, int] | None,
) -> tuple[list[float], list[int]]:
    """`memory.redact` on the authority: the same rows' text, in the heads and in the
    revision journal, in one transaction. `key` is the redacted version's key and `head`
    the (memory_id, version) of its live head the caller checked, None for none: a head
    written or restored there since answers 503, and the retry checks it again. Returns
    the seconds left on the leases of the scrubbed revisions' deliveries, whose holders
    may still be sending the old text (`pg.wait_out`), and the later copies of the version
    on the key, redacted now or before."""
    org, namespace, agent, user_id, session_id, memory_key = key
    mid = UUID(str(memory_id))
    checked = None if head is None else (UUID(str(head[0])), int(head[1]))
    async with pg.transaction() as conn:
        await _bound_lock_waits(conn)  # the namespace's commits queue behind this one
        # The namespace's generation first, as a commit takes it, so no commit lands
        # between the statements below; then the heads in key order.
        await conn.execute(
            "SELECT 1 FROM loom.scope_generations WHERE org = $1 AND scope = $2 FOR UPDATE",
            org,
            namespace,
        )
        # The key's head and the heads of the profile keys with a revision built from the
        # memory (`profile_keys_built_from`, the handler's check), locked: a write or a
        # lock since that check refuses the redact here, under the row lock.
        key_args = (org, namespace, user_id, mid, agent, session_id, memory_key)
        heads = await conn.fetch(
            "SELECT c.row FROM loom.memory_current c WHERE c.org = $1 AND c.namespace = $2 "
            "AND ((c.agent, c.user_id, c.session_id, c.memory_key) = ($5, $3, $6, $7) "
            "OR (c.agent = '-' AND c.user_id = $3 AND (c.session_id, c.memory_key) "
            f"IN (SELECT row->>'session_id', row->>'memory_key' {_PROFILE_REVISIONS}))) "
            f"{_KEY_ORDER} FOR UPDATE OF c",
            *key_args,
        )
        live = None
        for stored in heads:
            row = decode_row(stored["row"])
            _lock.assert_unlocked(_hydrate(row), "delete", op="memory.redact")
            if identity_key(row) == key and row.get("tombstoned_at") is None:
                live = (UUID(str(row["memory_id"])), int(row["version"]))
        if live != checked:
            raise AdmissionContendedError("the memory was written while it was redacted")
        # The key's versions from the redacted one on: a namespace restored into itself
        # under another agent holds the same id under a second key.
        history = await conn.fetch(
            "SELECT version, content_sha256 FROM loom.memory_revisions "
            "WHERE org = $1 AND namespace = $2 AND user_id = $3 AND memory_id = $4 "
            f"AND {_meta('agent')} = $5 AND {_meta('session_id')} = $6 "
            f"AND {_meta('memory_key')} = $7 AND version >= $8 ORDER BY version",
            *key_args,
            int(version),
        )
        copies = later_copies(
            [Version(int(r["version"]), r["content_sha256"]) for r in history],
            int(version),
            live=live[1] if live and live[0] == mid else None,
        )
        redacted_args = (*key_args, int(version), copies)
        args = (*redacted_args, to_naive_utc_second(when).isoformat(), reason_blob)
        await conn.execute(
            f"UPDATE loom.memory_current SET row = {_SCRUBBED} WHERE {_REDACTED_HEADS}", *args
        )
        # Then the deliveries, before the revisions, as an erasure takes them: locked
        # before their leases are read; fenced with a new token, the ended lease kept in
        # `fenced_until` for a retry and an erasure to wait out (storage-authority.md).
        leases = await conn.fetch(
            "WITH locked AS ("
            f"  SELECT d.org, d.namespace, d.seq, {pg.LEASE_LEFT_S} AS left_s "
            "  FROM loom.memory_deliveries d WHERE (d.org, d.namespace, d.seq) IN ("
            f"    SELECT org, namespace, seq FROM loom.memory_revisions WHERE {_REDACTED_EVER}) "
            "  ORDER BY d.org, d.namespace, d.seq FOR UPDATE OF d), "
            "fenced AS ("
            "  UPDATE loom.memory_deliveries d SET lease_token = d.lease_token + 1, held = false, "
            "    fenced_until = GREATEST(d.fenced_until, NULLIF(d.lease_until, 'infinity')), "
            "    lease_until = CASE WHEN d.lease_until = 'infinity' THEN d.lease_until "
            "    ELSE LEAST(d.lease_until, now()) END FROM locked "
            "  WHERE (d.org, d.namespace, d.seq) = (locked.org, locked.namespace, locked.seq)) "
            "SELECT left_s FROM locked",
            *redacted_args,
        )
        # A payload cleared after its delivery has nothing to scrub here: its columns
        # say it is redacted, and ClickHouse's scrub reaches its text.
        await conn.execute(
            f"UPDATE loom.memory_revisions SET row = {_SCRUBBED}, redacted_at = $10, "
            f"redaction_reason = $11 WHERE {_REDACTED}",
            *args,
        )
    return [float(r["left_s"]) for r in leases], copies


async def revisions_since(
    org: str, namespace: str, after: int, through: int
) -> list[dict[str, Any]] | None:
    """The revisions of seqs `after` (excluded) through `through`, or None when one of
    their payloads was cleared after its delivery: a gap read that cannot see a row."""
    rows = await pg.fetch(
        "SELECT seq, row FROM loom.memory_revisions "
        "WHERE org = $1 AND namespace = $2 AND seq > $3 AND seq <= $4 ORDER BY seq",
        org,
        namespace,
        after,
        through,
    )
    if any(r["row"] is None for r in rows):
        return None
    return [{"seq": int(r["seq"]), **decode_row(r["row"])} for r in rows]

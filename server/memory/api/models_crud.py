"""memory.* CRUD + key-addressed lifecycle ops.

get / set / pin / delete / restore / clear / list / touch and the opaque-UUID
addressing pair (get_by_id / update_by_id) plus redact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from server.memory.api.models_common import FileRefs, MemorySetPeer, MemoryType, _Identity
from server.memory.domain.code_change import CodeState
from server.memory.domain.delete_marks import caller_trace
from server.memory.domain.expire_reasons import caller_reason
from server.memory.domain.graph_quality import (
    MAX_ENTITY_CHARS,
    MAX_TRIPLES_PER_ROW,
    canonical_entity_text,
    canonical_relation,
    fits_entity,
)
from server.memory.domain.search_text import canonicalize_verb
from server.memory.domain.sensitivity import Sensitivity
from server.operations.base import MCP_HIDDEN
from server.operations.read_projection import ProjectedRow
from server.operations.read_projection import project as _project_fields


class MemoryGetInput(_Identity):
    # The key to read: tried under this identity, then the same user's row under any
    # agent. A `chunk:<session_id>:<index>` key returns that chunk and its neighbours.
    memory_key: str
    # Version precondition: the version the caller already
    # holds. When the live row still carries it, the response is
    # `unchanged: true` with the version and no body — a poll that holds the
    # row pays one point read and no payload. None = always return the body.
    if_version: int | None = None
    # Opt-in: the per-agent read breakdown and served_count / last_served_at; off by
    # default to keep get cheap.
    include_recall_breakdown: bool = False
    # As-of replay filter: restrict to rows with coalesce(valid_at, created_at) <= as_of.
    # None = current row.
    as_of: datetime | None = None
    # A whitelist of output field names (`value`, `tags`,
    # `status`, `memory_type`, `recall_count`, `created_at`, …). The response
    # carries only those plus `found` / `unchanged` (and `resolved_from` when set);
    # an unknown name is a 422.
    # Empty (the default) = every field.
    fields: list[str] = Field(default_factory=list, max_length=64)
    # Serve the live canonical a `status='duplicate'` row
    # points to (its `duplicate_of`, same org and namespace, up to three
    # hops) instead of the exact row; the response then carries
    # `resolved_from` = the key asked for and `canonical_key` = the key
    # served. Off by default: the exact row. A duplicate whose
    # canonical is gone, tombstoned or not live is served as itself.
    follow: bool = False
    # Not on the wire. An in-process read-modify-write (procedure.evolve,
    # record_run, revise) sets this so the point lookup reads with sequential
    # consistency and never sees a previous version as current.
    _strong_read: bool = False


class _RecallByAgent(BaseModel):
    agent: str
    count: int
    last_at: datetime | None = None


class CodeLink(BaseModel):
    """A memory's link to code.

    `state`: `fresh`, `moved` (followed to a new place), `changed` (its code changed
    since commit `changed_since`), `unresolved` (renamed or removed and not followed)
    or `missing` (the code is gone)."""

    code_namespace: str
    path: str
    symbol: str = ""  # '' for a file link
    state: CodeState
    changed_since: str = ""
    # the `revise` policy's corrected claim; '' when none
    suggestion: str = ""


class MemoryGetOutput(ProjectedRow):
    # `_projection` (a ProjectedRow) is the requested field
    # whitelist (plus `found` / `unchanged`, and `resolved_from` when set), or None =
    # every field. This row's own serializer drops two unset optionals first.
    @model_serializer(mode="wrap")
    def _omit_unset_canonical_and_project(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        # `canonical_key` is on the wire only for a row whose
        # status is `duplicate`; every other read's payload is unchanged.
        data: dict[str, Any] = handler(self)
        if self.canonical_key is None:
            data.pop("canonical_key", None)
        # `resolved_from` only on an answer given by another row.
        if self.resolved_from is None:
            data.pop("resolved_from", None)
        # Then the caller's field whitelist, when one was sent.
        return _project_fields(data, self._projection)

    @model_validator(mode="after")
    def _version_text(self) -> MemoryGetOutput:
        self.version_text = None if self.version is None else str(self.version)
        return self

    # False when no live, unexpired row of this user holds the key.
    found: bool
    # True when `if_version` matched the live row: version below, no body.
    unchanged: bool = False
    # Stable row id, surfaced so a key-recalled row can record an outcome
    # without a second lookup.
    memory_id: str = ""
    # The stored value as written (text, object, list or scalar).
    value: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    # The row's memory type; None when not found.
    memory_type: MemoryType | None = None
    # The writer's confidence in the fact, 0..1 (a `demote` expiry lowers it).
    confidence: float | None = None
    # Nanosecond version of the live row; send it back as `if_version` to poll.
    version: int | None = None
    # The same version as a decimal string, exact for a JavaScript caller (the number is
    # above 2^53, which JSON.parse rounds): send this one to memory.delete_version. Set
    # from `version`, so every answer carrying one carries both.
    version_text: str | None = None
    # When the key was first written; kept across later versions.
    created_at: datetime | None = None
    # Last write to this key; distinct from created_at, which is preserved across updates.
    updated_at: datetime | None = None
    # When the row expires (memory.get hides it after that, with `as_of` only a high one;
    # memory.list serves it until the expiry sweep, and memory.bulk_get does too, except a
    # high one); None = no expiry.
    expires_at: datetime | None = None
    # Post-action on expiry ('' = tombstone) and its reason.
    on_expire: str = ""
    # Why the expiry was set, as the writer gave it; '' when none.
    expire_reason: str = ""
    # Real-world time the fact became true (distinct from created_at = when recorded).
    # NULL when neither caller nor extractor supplied one.
    valid_at: datetime | None = None
    # Closing bound of the fact's stated validity ("until 2024"); NULL = open
    # interval, the common case. Read paths drop a row past its valid_to the
    # way they drop one past expires_at.
    valid_to: datetime | None = None
    # Date the fact is ABOUT (distinct from valid_at) and how precisely the
    # source pinned it (0..1 per the extraction rubric; 0 = unscored).
    temporal_anchor: datetime | None = None
    # How precisely the source pinned `temporal_anchor`, 0..1; 0 = unscored.
    temporal_confidence: float = 0.0
    # True when the row was saved with `memory.pin`: a write that does not pin never
    # replaces it (409 PINNED_CONFLICT).
    pinned: bool = False
    # Who wrote the live version (`human:<subject>` / `process:<subject>`) and how
    # the content entered memory (asserted | extracted | observed | derived |
    # imported | external).
    actor: str = ""
    # How the content entered memory: asserted | extracted | observed | derived |
    # imported | external; '' when not found.
    source_class: str = ""
    # The writer's provenance pointer, e.g. "conversation:<thread_id>"; '' when none.
    # A read-then-write sends it back to keep it on the new version.
    source_trace: str = ""
    # The resource the fact was lifted from (memory.provenance cites it); '' when none.
    source_resource_id: str = ""
    # Writer agent of the resolved row. Surfaced so a cross-agent read
    # (agent="*") can tell callers which agent owns the row — the dashboard's search
    # re-fetch needs it to render + re-open a hit found outside the preload.
    agent: str = ""
    # The resolved row's user and session: a write back to this row (restore, verify)
    # names them, since a key repeats across users and sessions.
    user_id: str = ""
    session_id: str = ""
    # Lifecycle state from loom.memory.status.
    status: str | None = None
    # The code this memory is linked to: each link's path, symbol, state (`fresh`,
    # `moved`, `changed`, `unresolved` or `missing`) and the `revise` policy's suggestion.
    code: list[CodeLink] = Field(default_factory=list)
    # For a `status='superseded'` row, the memory_id of the row
    # that replaced it ('' otherwise) — the bundle turns it into a
    # `supersedes` edge on the newer row.
    superseded_by: str = ""
    # The row this one duplicates (status='duplicate'), and the
    # verdict the row shows on read, derived from `status` (active → clear,
    # conflicted → conflict, duplicate, superseded).
    duplicate_of: str = ""
    # clear | conflict | duplicate | superseded, as described above.
    conflict_verdict: str = ""
    # For a `status='duplicate'` row, the memory_key of the
    # canonical that answers for it ("" when that row is no longer active —
    # the orphan case `memory.resolve_orphan_duplicates` repairs). Absent
    # from the payload for every other status. On a `follow: true` read that
    # served the canonical it is the key served.
    canonical_key: str | None = None
    # On an answer given by another row, the memory_key the caller asked for: a
    # `follow: true` read that served a duplicate's canonical, or a memory.bulk_get
    # chunk id answered by its parent fact. Absent otherwise.
    resolved_from: str | None = None
    # Surfaced so an in-place tag edit can preserve existing tags/entities
    # rather than clobbering them.
    tags: list[str] = Field(default_factory=list)
    # Entity names stored on the row (lowercased), internal sentinels left out.
    entities: list[str] = Field(default_factory=list)
    # SHA-256 of the live value (canonical JSON); pass back as if_match_sha256
    # for a no-clobber update against the value THIS read saw.
    content_sha256: str = ""
    # Server-enforced char cap on `value` (NULL = no cap).
    char_limit: int | None = None
    # Recall credits: reads by a credited reader (memory.search with auto_touch, from a
    # client that posts outcomes), folded from loom.memory_usage_* into the ranking signal.
    # A context pack, a memory.ask or an uncredited search adds none: see served_count.
    recall_count: int = 0
    # When a credited read last returned the row; None = no credit yet.
    last_recalled_at: datetime | None = None
    # Agent reads that recalled the key, credited or not, never below recall_count; only with
    # include_recall_breakdown=True, else None. Counted over the key's live rows inside the
    # reader's scope; which ops count: docs/features/observability/cost-and-usage.md.
    served_count: int | None = None
    # When an agent read last returned the key; None = none recorded, or not asked for.
    last_served_at: datetime | None = None
    # Attestation (memory.verify). `verification` is derived against the live
    # content hash: human | process | stale (content changed since) | none.
    verified_by: str = ""
    # When the attestation in `verified_by` was made; None = never attested, or revoked.
    verified_at: datetime | None = None
    # human | process: attested and unchanged since; stale: the content changed
    # after the attestation; none.
    verification: str = "none"
    # Lock in force (memory.lock). '' = unlocked or lapsed.
    lock_mode: str = ""
    # Why the lock was set, as the locker gave it; '' when unlocked.
    lock_reason: str = ""
    # When the lock lapses; None = open-ended, or no lock.
    lock_until: datetime | None = None
    # Who set the lock (`human:<subject>` / `process:<subject>` under auth); '' when unlocked.
    locked_by: str = ""
    # Agent reads of the key per named agent (last 30 days, credited or not); populated
    # only when include_recall_breakdown=True.
    recall_by_agent: list[_RecallByAgent] = Field(default_factory=list)
    # '' | low | medium | high. A 'high' read is audited.
    sensitivity: str = ""


# A (subject, relation, object) fact a point write names: at most MAX_TRIPLES_PER_ROW a
# row, each part capped at 256, a subject or object as written and once normalised. The
# lengths the triple writer needs are checked on the canonical form it stores, so "I"
# (stored as "user") is a subject and "  " is refused rather than dropped. A bulk entry's
# `relations` stay unchecked: an extractor's surplus is cut, not refused. When the
# canonical form dropped something, the refusal says what it was stored as and why, since
# "has" and "is a" look long enough until the copula is dropped; a part that is only too
# short ("x") just says so.
def _canonical_at_least(min_chars: int, canonical: Any, dropped: str, needs: str) -> AfterValidator:
    def check(raw: str) -> str:
        stored = canonical(raw)
        if len(stored) >= min_chars:
            return raw
        if stored == raw.strip().lower():  # nothing dropped, only lowercased
            raise ValueError(f"{raw!r} is too short: {needs}")
        raise ValueError(f"{raw!r} is stored as {stored!r} ({dropped} are dropped); {needs}")

    return AfterValidator(check)


def _refuse_long_normalised(raw: str) -> str:
    if not fits_entity(raw):
        raise ValueError(f"{raw!r} is longer than {MAX_ENTITY_CHARS} characters once normalised")
    return raw


def _relation_verb(raw: str) -> str:
    return canonical_relation(canonicalize_verb(raw)).verb


_ENTITY_DROPS = "articles, possessives and punctuation"
_FactSubject = Annotated[
    str,
    Field(max_length=MAX_ENTITY_CHARS),
    AfterValidator(_refuse_long_normalised),
    _canonical_at_least(
        2,
        canonical_entity_text,
        _ENTITY_DROPS,
        "a subject needs at least 2 characters, such as a name",
    ),
]
_FactRelation = Annotated[
    str,
    Field(max_length=256),
    _canonical_at_least(
        2,
        _relation_verb,
        "copulas, auxiliaries and punctuation",
        "a relation needs at least 2 characters: name it with a content verb such as 'owns'",
    ),
]
_FactObject = Annotated[
    str,
    Field(max_length=MAX_ENTITY_CHARS),
    AfterValidator(_refuse_long_normalised),
    _canonical_at_least(
        1, canonical_entity_text, _ENTITY_DROPS, "an object needs at least 1 character"
    ),
]
Fact = tuple[_FactSubject, _FactRelation, _FactObject]


class MemorySetInput(_Identity):
    # The key to write; a write to a key that already holds a row adds a new version.
    memory_key: str
    # Coarse classification axis (drives procedural memory + tag_registry
    # policy lookups). Prefer `tags` for finer-grained discrimination.
    memory_type: MemoryType = "semantic"
    # The fact itself: plain text, or a JSON object, list or scalar.
    value: dict[str, Any] | list[Any] | str | int | float | bool
    # Flexible classification, filterable at recall via MemorySearchInput.tags;
    # per-tag retention/dedup/conflict policy via NamespaceRegisterInput.tag_registry.
    tags: list[str] = Field(default_factory=list)
    # Explicit entities for a point write. Stored (lowercased) on the embedding
    # row so memory.timeline / entity_filter can find it without waiting on LLM
    # extraction — which never tags a point write under a fake/keyless provider.
    entities: list[str] = Field(default_factory=list)
    # The (subject, relation, object) facts the value states, written to the fact
    # graph and linked to the row as a `memory.bulk_set` entry's are: a single-valued
    # fact closes the older one it replaces and, under `newest_wins`, retires the rows
    # behind it. A revision that names a fact keeps it however it is worded; one naming
    # none keeps a fact only while it repeats the old words. A 'high' row's facts, and a
    # proposal's, are not graphed. Not in the MCP listing.
    relations: list[Fact] = Field(
        default_factory=list, max_length=MAX_TRIPLES_PER_ROW, json_schema_extra=MCP_HIDDEN
    )
    # The repo files this memory is about (repo-relative path
    # or bare filename); memory.for_file serves it before an edit of them.
    # Unset = detected from the value; a rewrite that detects none keeps the
    # row's; [] clears.
    files: FileRefs | None = None
    # The writer's own confidence in the fact, 0..1 (1.0 = certain).
    confidence: float = 1.0
    # Provenance tag of the write (`conversation:<id>`, `curated_doc://…`); a known
    # prefix decides `source_class` (`conversation:` → extracted), and one starting with
    # `memory.delete_version:` is refused. Not in the MCP listing.
    source_trace: str = Field(default="", json_schema_extra=MCP_HIDDEN)
    # Declare that this content came from outside (a tool result, a page, a
    # document): `external`, or `imported`. The server decides every other
    # class from the token and the operation; a body may only downgrade.
    source_class: str | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # Provenance pointer to the primary source this fact was cited from — a file
    # path, document id, or URL. Stored on the embedding row (bloom-indexed), so
    # it's filterable and surfaces in memory.provenance.
    source_resource_id: str = ""
    # Expire the row this many seconds from now. None keeps a live expiry of the row,
    # else applies the type's retention default (no expiry when it has none).
    ttl_seconds: int | None = None
    # Force expires_at=NULL, ignoring per-type/namespace retention defaults.
    clear_expiry: bool = False
    # What happens when expires_at passes: '' keeps the row's
    # current action (tombstone for a new row); 'demote' clears the expiry and
    # scales confidence down; 'anonymize' replaces the value with a marker
    # and drops the embeddings, keeping the shell row. Applied by the sweep.
    on_expire: Literal["", "tombstone", "demote", "anonymize"] = ""
    # Why the expiry is set (at most 512 chars, not starting with `blacklist.sweep:`); ''
    # keeps the row's current reason.
    expire_reason: str = Field(default="", max_length=512)
    # Real-world time the fact became true (distinct from created_at and from
    # temporal_anchor). NULL = unknown; as-of reads fall through to created_at.
    valid_at: datetime | None = None
    # Closing bound of the fact's stated validity ("until 2024"); NULL = open
    # interval. Read paths drop a row past its valid_to the way they drop one
    # past expires_at.
    valid_to: datetime | None = None
    pinned: bool = False  # True when written via memory.pin (explicit-save)
    # Call the namespace's classify_endpoint to derive memory_type from `value`.
    # Classifier wins iff non-empty and inside tag_registry; else caller's value. Fail-open.
    classify: bool = Field(default=False, json_schema_extra=MCP_HIDDEN)
    # Optimistic-concurrency precondition: 409 PRECONDITION_FAILED if the live
    # row's SHA-256 doesn't match. Empty = skip the check.
    if_match_sha256: str = ""
    # Server-enforced char cap on the stringified value (422 on exceed). Persists
    # on the row so a later set without char_limit reuses the stored cap. NULL = no cap.
    char_limit: int | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # Sensitivity tier. 'high' = never embedded, searched or in a recency pack;
    # read by key only, audited. '' = detect from the value: credentials, card
    # numbers, SSNs, PINs high; email, phone, IBAN medium.
    sensitivity: Sensitivity = ""
    # Run the conflict check and return its verdict, peers and
    # `next` WITHOUT writing — no loom.memory row, no embedding row, no audit
    # hash, no usage event, no supersession. `version` is 0, `noop` is true.
    # For "would this be a new fact, a restatement or a contradiction?" before
    # committing to a key.
    dry_run: bool = False
    # Write the fact as a PROPOSAL (`status='proposed'`): not
    # served by default, served to a read that passes `include_proposed`, and
    # accepted or rejected on the graded outcomes of the runs that used it
    # (`run.grade`, `memory.proposals`, `memory.proposal_decide`). A proposal
    # never retires, supersedes or conflict-flags an established fact; a key
    # that already holds one is refused (409 KEY_HOLDS_FACT) — propose under a
    # new key.
    proposed: bool = False

    @field_validator("expire_reason")
    @classmethod
    def _expire_reason(cls, v: str) -> str:
        return caller_reason(v)

    @field_validator("source_trace")
    @classmethod
    def _source_trace(cls, v: str) -> str:
        return caller_trace(v)


class MemorySetOutput(BaseModel):
    version: int  # nanosecond version assigned by the server
    # True when the stored row is pinned: a `memory.pin`, or a restatement of a
    # pinned row.
    pinned: bool = False
    memory_id: str = ""  # UUID assigned to this insert; usable with memory.get_by_id
    # SHA-256 of the just-written value; usable as if_match_sha256 on next write.
    content_sha256: str = ""
    # The written row's lifecycle status (`active`, `duplicate`, `proposed`);
    # "" on a write that stored no row (merge, dry run).
    status: str = ""
    # Set when this write was tagged status='duplicate': memory_id of the
    # canonical semantically-identical row. Empty for non-duplicate writes.
    duplicate_of: str = ""
    # Echoed so a tool-call response carries citable factual data.
    namespace: str = ""
    # The key the request named; on a `merge` see `merged_into_key`.
    memory_key: str = ""
    # On a `merge` the store already held this content under
    # ANOTHER key; nothing was written at `memory_key`, so a get/lock/update of
    # it finds no row. This names the key that holds it ("" when it is this one).
    merged_into_key: str = ""
    # Providers that fell back on this write and what the response did instead
    # (`classify: caller_type_kept (ReadTimeout)`). Empty when nothing degraded.
    degraded: list[str] = []
    # Set when the declared memory_type did not fit the value and the row was
    # stored under another type (currently: 'procedural' on a value that is not
    # a procedure body → 'semantic', tagged retyped_from:procedural). Empty when the
    # declared type was kept.
    retyped_from: str = ""
    # What the write-time conflict check decided about THIS
    # write — clear | merge | duplicate | conflict | refines | rejected (the
    # last only on a dry run; a real write answers 409). Empty when no check
    # ran (a non-searchable type, a 'high' sensitivity row).
    conflict_verdict: str = ""
    # The rows the verdict was reached against (closest first, at most five):
    # the key is the handle for memory.update, the excerpt what the store
    # holds. Empty on `clear`.
    peers: list[MemorySetPeer] = Field(default_factory=list)
    # A short machine hint naming the op to reach for: memory.update(key) on a
    # conflict, memory.pin on a merge the store has seen before, "" otherwise.
    next: str = ""
    # True when this call wrote nothing: a `merge` (the store already held the
    # fact; its row was reinforced instead) or a dry run.
    noop: bool = False
    # Echo of the request's dry_run so a tool-call transcript is unambiguous.
    dry_run: bool = False
    # One line per thing the server noticed about the VALUE
    # that the agent should fix on its side — currently only "base64-encoded".
    # The write succeeded regardless; this is advice, not a verdict.
    advisories: list[str] = Field(default_factory=list)
    # Verbatim chunk rows this write added to the chunk plane
    # for a long searchable value (0 for a short value, a non-searchable type,
    # a merge or a dry run). Chunks are a second way to find the row, not a
    # condition of the write: a chunk failure leaves the row in and this 0.
    chunks_written: int = 0


class MemoryDeleteInput(_Identity):
    # The key to delete: tried under this identity, then the same user's row under
    # any agent.
    memory_key: str


class MemoryDeleteVersionInput(_Identity):
    # The key to delete, under this exact identity: another agent's row of it is never read.
    # `agent` must be concrete, not "*".
    memory_key: str
    # The version to delete, as memory.get answered it. A row rewritten since is kept. A
    # decimal string is accepted: a nanosecond version is above 2^53, which a JavaScript
    # number rounds.
    version: int | str

    @field_validator("version")
    @classmethod
    def _decimal(cls, v: int | str) -> int:
        if isinstance(v, str):
            if not (v.isascii() and v.isdigit()):
                raise ValueError("`version` is a version number")
            v = int(v)
        if not 0 <= v < 2**64:
            raise ValueError("`version` is a version number")
        return v


class MemoryUnpinInput(_Identity):
    """Remove the write-time pin from one live memory.

    This is deliberately a separate operation from ``memory.pin``: pin stays
    idempotently reinforcing, while unpin is the privileged loosening action.
    """

    # The key to unpin: tried under this identity, then the same user's row
    # under any agent, matching memory.delete's key resolution.
    memory_key: str


class MemoryUnpinOutput(BaseModel):
    unpinned: bool  # False when the key is absent or already unpinned.
    reason: str = ""  # "not_found" | "not_pinned" on an idempotent no-op.
    memory_id: str = ""
    version: int | None = None


class MemoryDeleteOutput(BaseModel):
    """Soft-deleted via tombstone (new row with `tombstoned_at = now()`).

    The 14-day grace period during which the tombstone can be reversed is
    enforced by retention TTL, not by application code.
    """

    # True when a live row was found and a tombstone version written over it.
    tombstoned: bool
    # Nanosecond version of the tombstone row; when nothing was found, a fresh
    # number that no row carries.
    version: int
    # False when no live row of this key exists in the
    # namespace under ANY identity — already deleted, or never written. The
    # delete looked past the caller's own (agent, user, session) first.
    found: bool = True
    # Consolidated summaries built from this row (tag `src:<key>`): retired
    # when none of their sources survive, else downgraded to `conflicted`.
    dependents_tombstoned: int = 0
    # Summaries built from this row that keep a live source: set to `conflicted`.
    dependents_downgraded: int = 0


class MemoryDeleteVersionOutput(BaseModel):
    # True when the row still had `version` and a tombstone was written over it.
    tombstoned: bool
    # Nanosecond version of the tombstone row; 0 when nothing was deleted.
    version: int = 0
    # False when no live row of this key exists under this identity.
    found: bool = True
    # "version_changed" when the live row no longer carries `version`, or moved on while
    # the delete ran (its tombstone then stays in the key's history, under the newer row).
    reason: str = ""
    # Consolidated summaries built from this row (tag `src:<key>`): retired
    # when none of their sources survive, else downgraded to `conflicted`.
    dependents_tombstoned: int = 0
    # Summaries built from this row that keep a live source: set to `conflicted`.
    dependents_downgraded: int = 0


class MemoryRestoreInput(_Identity):
    """Bring back a deleted memory (within 14 days) or one Loom retired on its own."""

    # The key to restore, under this exact identity (no namespace-wide fallback).
    memory_key: str
    # Precondition: restore only a row still retired behind this memory, as a
    # `memory.auto_supersede` record names it; a row deleted, revived or retired
    # behind another memory since is left as it is.
    if_superseded_by: str = ""
    # Precondition: restore only a row still retired (`superseded`), never undo a
    # delete. For a caller that cannot see which memory the row retired behind;
    # `if_superseded_by` implies it, so either is enough and both are harmless.
    only_if_superseded: bool = False


class MemoryRestoreOutput(BaseModel):
    # True when a live version was written back; False on a no-op, a miss or `expired`.
    restored: bool
    # Empty / None when nothing was tombstoned or superseded for this key (no-op).
    memory_id: str | None = None
    # Version of the restored row, or of the already-live row on a no-op.
    version: int | None = None
    # Surfaces "you missed the window" cleanly rather than 404-ing.
    expired: bool = False
    # What the row was restored FROM: "tombstoned" (a delete undone) or
    # "superseded" (an automatic retirement reversed). Empty on a no-op.
    from_status: str = ""


class MemoryStatusesInput(BaseModel):
    """The current status of some memories, by id: which automatic retirements still
    stand (the Review queue's Auto-resolved tab reads it)."""

    org: str
    namespace: str
    # Ids this namespace does not hold are left out, including one replaced by a racing
    # first write of the same key. An id past 64 characters refuses the request; a
    # malformed one is left out.
    memory_ids: list[Annotated[str, Field(max_length=64)]] = Field(
        ..., min_length=1, max_length=5000
    )


class MemoryStatus(BaseModel):
    memory_id: str
    # The row's status now (`active`, `conflicted`, `superseded`, ...); `deleted` when
    # tombstoned.
    status: str
    # The memory a `superseded` row is retired behind; empty otherwise.
    superseded_by: str = ""


class MemoryStatusesOutput(BaseModel):
    # One entry per id the namespace holds inside the read scope.
    rows: list[MemoryStatus] = Field(default_factory=list)


class MemoryAutoRetirementsInput(BaseModel):
    """The memories Loom retired on its own (the conflict resolver, `newest_wins`),
    newest record first, inside the request's read scope."""

    org: str
    namespace: str
    # Only records written at or after this time; unset, the last 30 days.
    since: datetime | None = None
    # Retired memories per page, 1 to 200 (default 50). A record's entries are never split
    # across pages, so a page can hold more; under a narrowed read scope it can hold fewer,
    # even none, with `next_cursor` set.
    limit: int = Field(default=50, ge=1, le=200)
    # The `next_cursor` of the previous page; unset, the newest page.
    cursor: str | None = None


class MemoryAutoRetirement(BaseModel):
    # When the retirement was recorded.
    ts: datetime
    # Why it retired: `conflict_resolver`, `same_key_newest_wins`,
    # `newest_wins_contradiction` or `value_update`.
    reason: str
    memory_id: str
    memory_key: str
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"
    # The memory it retired behind, and that memory's key.
    superseded_by: str = ""
    superseded_by_key: str = ""
    # The conflict resolver's label for the pair (`update`); empty on other paths.
    label: str = ""
    # The re-read after the write could not confirm the retirement took effect.
    unconfirmed: bool = False
    # The conflict resolver's judgement; empty (None for the confidence) on other paths.
    judge_verdict: str = ""
    judge_confidence: float | None = None
    judge_model: str = ""
    judge_prompt_version: str = ""


class MemoryAutoRetirementsOutput(BaseModel):
    # One row per retired memory, newest record first. `memory.statuses` tells which
    # still stand; `memory.restore` undoes one.
    rows: list[MemoryAutoRetirement] = Field(default_factory=list)
    # Pass back as `cursor` for the next, older page; None when this page is the last.
    next_cursor: str | None = None


class MemoryClearInput(BaseModel):
    """Bulk-tombstone memories in one scope.

    At least one of `user_id` or `agent` must name a concrete value other than `'-'`;
    anything wider is refused at the handler (use erasure.request_namespace).
    `confirm_subject` must equal one of those concrete values — guards against UI-dropdown
    accidents.
    """

    org: str
    namespace: str
    # The user whose rows are cleared (`'-'` for rows with no user); `'*'` or unset clears
    # every user's.
    user_id: str | None = None
    # The agent whose rows are cleared; `'*'` clears every agent's, and unset clears every
    # agent's over HTTP but only the calling client's over MCP.
    agent: str | None = None
    # Must equal the concrete `user_id` or `agent` being cleared (`'*'` and `'-'` never
    # confirm); anything else is a 400.
    confirm_subject: str = Field(..., min_length=1, max_length=256)


class MemoryClearOutput(BaseModel):
    """Tombstone (not hard-delete) across loom.memory + loom.memory_embeddings.
    Same 14-day grace window as memory.delete; cleared keys revive by re-writing
    the same key within the window."""

    # UTC time the clear ran.
    cleared_at: datetime
    rows_affected: dict[str, int]  # {table_name: rows_tombstoned}
    total: int  # sum across all tables
    # Live rows in scope that carried a lock in force and were left alone.
    locked_skipped: int = 0


class MemoryTouchInput(_Identity):
    """Change only a memory's expiry: set a new TTL (`ttl_seconds`) or clear
    it (`clear_expiry`), for one key or up to 1,000 (`memory_keys`), without
    rewriting its value, entities, conflict state or reinforcement. The embedding rows
    get the new expiry by a versioned re-emit."""

    # One key to touch; send it or `memory_keys`, not both.
    memory_key: str = ""
    # Many keys of the same identity in one call — one read,
    # one loom.memory insert, one batched embedding re-emit.
    memory_keys: list[str] = Field(default_factory=list, max_length=1000)
    # New expiry this many seconds from now; send it or `clear_expiry: true`, not both.
    ttl_seconds: int | None = Field(default=None, ge=1)
    # "never expire" — expires_at becomes NULL.
    clear_expiry: bool = False
    # Optionally re-declare the post-action alongside the new expiry.
    on_expire: Literal["", "tombstone", "demote", "anonymize"] = ""
    # Why the new expiry is set (at most 512 chars, not starting with `blacklist.sweep:`);
    # '' keeps the row's reason.
    expire_reason: str = Field(default="", max_length=512)

    @field_validator("expire_reason")
    @classmethod
    def _expire_reason(cls, v: str) -> str:
        return caller_reason(v)

    @model_validator(mode="after")
    def _shape(self) -> MemoryTouchInput:
        if bool(self.memory_key) == bool(self.memory_keys):
            raise ValueError("pass exactly one of `memory_key` or `memory_keys`")
        if (self.ttl_seconds is not None) == bool(self.clear_expiry):
            raise ValueError("pass exactly one of `ttl_seconds` or `clear_expiry: true`")
        return self


class MemoryTouchResult(BaseModel):
    memory_key: str
    touched: bool  # False when the key doesn't exist (or is tombstoned)
    version: int | None = None
    expires_at: datetime | None = None


class MemoryTouchOutput(BaseModel):
    touched: bool  # single key: False when it doesn't exist; many: any touched
    # Single key: the new version (None when not touched). None with `memory_keys`;
    # each result carries its own.
    version: int | None = None
    # The new expiry of the touched rows; None when cleared or none touched.
    expires_at: datetime | None = None
    # With `memory_keys`, one row per asked key.
    results: list[MemoryTouchResult] = Field(default_factory=list)
    # With `memory_keys`, how many keys were touched; 0 for one key.
    touched_count: int = 0


class MemoryListInput(_Identity):
    """List memory keys under this identity, optionally filtered.

    No cursor pagination (capped at 1000 entries per call). `sort_by`:
    "recency" (created_at DESC) or "score" (evidence_count + recall_count*0.5 DESC).
    `quotas` enforces per-memory_type slot counts when sort_by=score, e.g.
    {"observation": 8, "principle": 5} returns at most 13 rows.
    """

    # Only keys that start with this string; None or '' = every key.
    prefix: str | None = None
    # Only rows of this memory type; None = every type.
    memory_type: MemoryType | None = None
    # Exact-match on source_trace — e.g. "conversation:<thread_id>" to list the
    # memories a given conversation produced.
    source_trace: str | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # At most this many entries; `truncated` says when more exist.
    limit: int = Field(default=100, ge=1, le=1000)
    # Page offset for paginated browsing (recency sort). Rows [offset, offset+limit).
    offset: int = Field(default=0, ge=0)
    # The two counts (`total`, `today`) and the
    # namespace version, no rows — for a caller that shows a number (the
    # Setup page's namespace total). Hidden from the MCP listing.
    count_only: bool = False
    # The preview length the caller shows. Each
    # entry's `value` is cut to this many characters (the stored preview is
    # 2,048); `value_truncated` says when the row holds more. Hidden from the
    # MCP listing; agents keep the full preview.
    preview_chars: int = Field(default=2048, ge=40, le=2048)
    # `recency` (newest first) or `score` (evidence_count + 0.5 × recall_count,
    # highest first).
    sort_by: Literal["recency", "score"] = "recency"
    # With `sort_by: score`, the most entries per memory type (`{"principle": 5}`);
    # a type not named, or at 0, is uncapped. Ignored for `recency`.
    quotas: dict[str, int] | None = None
    # Also return tombstoned rows (within the 14d grace window) for recovery views.
    include_tombstoned: bool = False
    # `status='duplicate'` rows are skipped (their canonical is
    # in the same scan) unless asked for.
    include_duplicates: bool = False
    # `status='superseded'` rows (a later write replaced them)
    # are skipped unless asked for; a served entry carries its `status`.
    include_superseded: bool = False
    # `status='proposed'` rows (facts on trial) are skipped
    # unless asked for; `memory.proposals` lists them with their graded counts.
    include_proposed: bool = False
    as_of: datetime | None = None  # see MemoryGetInput.as_of
    # Only rows carrying any one of these tags, in any case; empty = no tag filter.
    tags: list[str] = Field(default_factory=list, max_length=64)
    # Require every one of `tags` instead of any one; default off. Not in the MCP listing:
    # the dashboard's stacked tag chips set it.
    tags_every: bool = Field(default=False, json_schema_extra=MCP_HIDDEN)
    # Only rows whose value or key holds this text, any case (a high row by its key
    # only) — the keyword filter of a browse page's search box, which must find every
    # row that names a word (a ranked search returns its top k and misses the rest).
    # A scan of the namespace's values; None or '' = no filter. Hidden from
    # the MCP listing: agents search with memory.search.
    contains: str | None = Field(default=None, max_length=200, json_schema_extra=MCP_HIDDEN)
    # Whitelist of entry field names; every entry carries only
    # those plus `memory_key` and `sensitivity`. Empty = every field. See
    # MemoryGetInput.fields.
    fields: list[str] = Field(default_factory=list, max_length=64)
    # The `namespace_version` a previous answer
    # carried. When the namespace's newest version is still that one the
    # answer is `unchanged: true` with no rows, no total and no today-count
    # — a poll that learns "nothing moved" costs one small read. Omit it
    # (agents, first loads) for the full answer.
    # A decimal STRING on the wire: the version is a nanosecond timestamp
    # above 2^53, which a JavaScript client rounds when it is a number.
    known_version: int | str | None = None


class _MemoryListEntry(ProjectedRow):
    # See MemoryGetOutput._projection; `memory_key` always rides.
    memory_key: str
    memory_type: MemoryType
    confidence: float
    version: int
    # The same version as a decimal string, exact for a JavaScript caller; set from
    # `version`.
    version_text: str = ""
    # Provenance, e.g. "conversation:<thread_id>" for seeder-promoted facts.
    source_trace: str = ""
    created_at: datetime
    # Last write to this key; distinct from created_at. NULL = never updated.
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    valid_at: datetime | None = None  # real-world time the fact became true; NULL = unsupplied
    tombstoned_at: datetime | None = None  # None for live rows
    # Lifecycle state from loom.memory.status, so a superseded row served on
    # request is recognisable.
    status: str = "active"
    pinned: bool = False
    verified_by: str = ""
    verification: str = "none"  # human | process | stale | none
    evidence_count: int = 1
    recall_count: int = 0
    last_recalled_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    # Writer's identity tuple, so cross-agent list views (agent="*") can re-issue
    # per-row memory.get with the strict identity that wrote each row.
    agent: str = ""
    user_id: str = ""
    session_id: str = ""
    # Serialized value, for the header search bar's client-side substring filter.
    value: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    # The row's value is longer than the `value`
    # sent (the preview was cut), so a caller that needs the whole value
    # reads the row; the stored hash and cap ride so it need not otherwise.
    # A high row comes with no value, no hash and no entities: read it by key.
    value_truncated: bool = False
    content_sha256: str = ""
    char_limit: int | None = None
    # Sensitivity tier ('' | low | medium | high); the server withholds a high row's value.
    sensitivity: str = ""

    @model_validator(mode="after")
    def _version_text(self) -> _MemoryListEntry:
        self.version_text = str(self.version)
        return self


class MemoryListOutput(BaseModel):
    # The matching rows in the requested order; empty on an `unchanged` poll.
    entries: list[_MemoryListEntry]
    truncated: bool  # true if more entries exist beyond `limit`
    # Total rows matching this scope (ignores `limit`), so callers can show
    # "N of TOTAL". 0 on an `unchanged` poll, which carries no counts.
    total: int = 0
    # Of `total`, how many were created since UTC midnight today — the true
    # "today" count (not the truncated page count the day-group headers show).
    today: int = 0
    # The namespace's newest memory version (any
    # agent, any status — every write moves it). Send it back as
    # `known_version` to learn cheaply whether anything changed. "" when the
    # namespace has no rows or the read did not compute it.
    namespace_version: str = ""
    # True when `known_version` matched: entries, total and today were not
    # read and are their defaults.
    unchanged: bool = False
    # When a `prefix` listing is empty, how many rows under
    # the prefix it left out as `duplicate`/`superseded`, and the flag that
    # serves them. 0 / "" otherwise.
    # A prefix listing is a lookup by name, so an empty one names what it hid.
    hidden: int = 0
    # A sentence naming those hidden rows and the flag that lists them; '' otherwise.
    hidden_hint: str = ""


# ============================================================
# memory.get_by_id / memory.update_by_id — opaque-UUID addressing
# ============================================================


# The most keys one memory.keys answer carries.
MEMORY_KEYS_MAX = 100_000  # not a setting: a wire bound the request schema and SDKs publish


class MemoryKeysInput(BaseModel):
    org: str
    namespace: str
    # The most keys to answer; `truncated` says when the namespace holds more.
    limit: int = Field(default=MEMORY_KEYS_MAX, ge=1, le=MEMORY_KEYS_MAX)


class MemoryKeyRef(BaseModel):
    agent: str
    user_id: str
    session_id: str
    memory_key: str


class MemoryKeysOutput(BaseModel):
    # Every live row's identity and key, read by one statement, so the set is the
    # namespace at one moment (memory.list's pages are read one at a time). The rows
    # memory.list serves with every identity at "*", superseded and duplicate rows included
    # and proposed rows left out.
    keys: list[MemoryKeyRef]
    # True when the namespace holds more than `limit` live rows.
    truncated: bool = False


class MemoryGetByIdInput(BaseModel):
    org: str
    memory_id: str  # UUID returned by memory.set
    # As-of replay filter — see MemoryGetInput.as_of.
    as_of: datetime | None = None
    # True answers a 'high' row without its value or hash, and audits no sensitive read.
    omit_high_value: bool = False


class MemoryGetByIdOutput(BaseModel):
    # False when no live row carries this id in the org, or the caller cannot read
    # its namespace.
    found: bool
    # The id asked for; '' when not found.
    memory_id: str = ""
    namespace: str = ""
    agent: str = ""
    user_id: str = ""
    session_id: str = ""
    # The row's key; '' when not found.
    memory_key: str = ""
    # The row's memory type; None when not found.
    memory_type: MemoryType | None = None
    # The stored value as written (text, object, list or scalar).
    value: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    # The writer's confidence in the fact, 0..1.
    confidence: float | None = None
    # Nanosecond version of the live row.
    version: int | None = None
    # When the key was first written; kept across later versions.
    created_at: datetime | None = None
    # When the row expires; None = no expiry.
    expires_at: datetime | None = None
    # Real-world time the fact became true; None when never supplied.
    valid_at: datetime | None = None
    # End of the fact's stated validity; None = open interval.
    valid_to: datetime | None = None
    # True when the row was saved with `memory.pin`.
    pinned: bool = False
    # SHA-256 of the live value (canonical JSON); pass back as if_match_sha256
    # on the next memory.update_by_id for a no-clobber update.
    content_sha256: str = ""
    # Who attested the row (`human:<subject>` / `process:<subject>`); '' = none.
    verified_by: str = ""
    # When that attestation was made; None = none.
    verified_at: datetime | None = None
    # human | process: attested and unchanged since; stale: the content changed
    # after the attestation; none.
    verification: str = "none"
    # '' | low | medium | high. A 'high' read is audited.
    sensitivity: str = ""
    # Who wrote the live version and how it entered memory.
    actor: str = ""
    # How the content entered memory: asserted | extracted | observed | derived |
    # imported | external; '' when not found.
    source_class: str = ""
    # Lock in force and expiry post-action.
    lock_mode: str = ""
    # Why the lock was set; '' when unlocked or lapsed.
    lock_reason: str = ""
    # When the lock lapses; None = open-ended, or no lock.
    lock_until: datetime | None = None
    # Who set the lock (`human:<subject>` / `process:<subject>` under auth); '' when unlocked.
    locked_by: str = ""
    # What the expiry sweep does when `expires_at` passes: tombstone | demote |
    # anonymize; '' = tombstone.
    on_expire: str = ""
    # Why the expiry was set, as the writer gave it; '' when none.
    expire_reason: str = ""


class MemoryUpdateByIdInput(BaseModel):
    org: str
    # The row's stable id (from `memory.set` or a read), org-wide.
    memory_id: str
    # None = keep the current value (a tags/concepts-only rewrite).
    value: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    # Replace the row's tags / concept keys (None = keep). The whole row is
    # re-emitted either way (ReplacingMergeTree) and its embedding is
    # recomputed.
    tags: list[str] | None = None
    # Replace the row's code concepts (symbol, library and error names); None keeps
    # them.
    concepts: list[str] | None = None
    # No-clobber precondition; same shape as MemorySetInput.if_match_sha256.
    if_match_sha256: str = ""
    # Char cap, same as MemorySetInput. Write-through: pass back the row's current
    # char_limit to preserve it across updates.
    char_limit: int | None = None
    # An expired row answers 409 EXPIRED unless the caller says revive=True,
    # which also clears the expiry.
    revive: bool = False
    # The facts the new value states, as MemorySetInput.relations; [] states none.
    relations: list[Fact] = Field(
        default_factory=list, max_length=MAX_TRIPLES_PER_ROW, json_schema_extra=MCP_HIDDEN
    )


class MemoryUpdateByIdOutput(BaseModel):
    updated: bool  # False if memory_id not found
    # Nanosecond version of the written row; 0 when nothing was written.
    new_version: int = 0
    content_sha256: str = ""  # SHA-256 of the just-written value
    # The semantic conflict verdict on the new value, as
    # memory.set computes it — "" when the check did not run (flag off or a
    # non-searchable type), else clear | duplicate | merge | conflict | refines.
    conflict_verdict: str = ""
    # How many existing rows the conflict check matched the new value to.
    conflict_peers: int = 0
    # Set when the new value restates or near-duplicates another row: that
    # row's memory_id (mirrors MemorySetOutput.duplicate_of).
    duplicate_of: str = ""


class MemoryUpdateInput(BaseModel):
    """Key-addressed update: the fact changed or got more specific.

    Resolves `memory_key` namespace-wide when no identity field is given —
    the row's agent / user_id / session_id are whatever wrote it, which the
    caller who just read the key through search or ask usually does not
    hold. Exactly one live row → the update_by_id path (lock, expiry,
    hash precondition, new version, embedding refresh). None → updated=False
    with reason="not_found". Several → 409 AMBIGUOUS_KEY listing them, so
    the caller narrows by identity or falls back to memory.update_by_id.
    """

    org: str
    namespace: str
    # The key to update, resolved across the namespace unless an identity field
    # below narrows it.
    memory_key: str
    # None = keep the current value (a tags/concepts-only rewrite).
    value: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    # Narrows the key to rows written by this agent; "", "-" or "*" = any agent.
    agent: str = ""
    # Narrows the key to this user's rows; "" or "-" = any user.
    user_id: str = ""
    # Narrows the key to this session's rows; "" or "-" = any session.
    session_id: str = ""
    # The run this revision is made under: stamped on the new
    # version's row so the run ledger and the `acted` fold see that this run
    # rewrote the key. Not a filter — the key is resolved as above. "" keeps
    # the row's own run_id.
    run_id: str = Field(default="", max_length=128)
    # Replace the row's tags (lowercased); None keeps them, [] clears them.
    tags: list[str] | None = None
    # Replace the row's code concepts (symbol, library and error names); None keeps
    # them.
    concepts: list[str] | None = None
    # New confidence for the written version (None = keep the row's).
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # Why the fact changed — lands in the audit row (audit=True), not on the memory.
    reason: str = Field(default="", max_length=500)
    # Content hash the live row must still carry, else 409 PRECONDITION_FAILED;
    # '' skips the check.
    if_match_sha256: str = ""
    # Char cap on the new value (422 on exceed); None = no cap on the new version,
    # so pass the row's cap back to keep it.
    char_limit: int | None = None
    # Allow updating an expired row (else 409 EXPIRED); the update clears its expiry.
    revive: bool = False
    # The facts the new value states, as MemorySetInput.relations; [] states none.
    relations: list[Fact] = Field(
        default_factory=list, max_length=MAX_TRIPLES_PER_ROW, json_schema_extra=MCP_HIDDEN
    )


class MemoryUpdateCandidate(BaseModel):
    memory_id: str
    agent: str = ""
    user_id: str = ""
    session_id: str = ""
    version: int = 0
    status: str = ""


class MemoryUpdateOutput(BaseModel):
    # True when a new version was written; False with `reason` otherwise.
    updated: bool
    # Stable id of the updated row; '' when not found.
    memory_id: str = ""
    # The row's key (the asked key when not found).
    memory_key: str = ""
    # Nanosecond version of the written row; 0 when nothing was written.
    new_version: int = 0
    # SHA-256 of the new value; send it as `if_match_sha256` on the next write.
    content_sha256: str = ""
    # "" on success; "not_found" when no live row carries the key.
    reason: str = ""
    # Filled only when the key resolved to several rows (also in the 409 detail).
    candidates: list[MemoryUpdateCandidate] = Field(default_factory=list)
    # See MemoryUpdateByIdOutput.
    conflict_verdict: str = ""
    # How many existing rows the conflict check matched the new value to.
    conflict_peers: int = 0
    # memory_id of the row the new value restates, when the check found one.
    duplicate_of: str = ""


class MemoryRedactInput(BaseModel):
    """Scrub the value of a historical memory row, preserving its audit entry.
    Refuses the live head (caller must memory.delete first).

    Addressed by (memory_id, version): every version of a memory shares one
    memory_id, so the version is what identifies the specific historical row.
    For secret-leak / PII scrubbing. Orthogonal to memory.delete: delete
    tombstones the memory; redact scrubs one historical row.

    Best-effort: loom.memory is ReplacingMergeTree(version) keyed by
    identity+memory_key (version is NOT in the sort key), so a background merge
    collapses non-head versions into the head — a targeted historical row may
    already be gone. Non-head versions are also never served (reads use FINAL),
    so this matters only for the audit trail. For a durable scrub of live data,
    delete + re-set the head.
    """

    org: str
    # The id every version of the memory shares; `version` picks the historical row.
    memory_id: str
    version: int  # target row's version (from memory.set / memory.history)
    reason: str = ""  # short justification; the audit row keeps its SHA-256 and length
    redacted_by: str = ""  # actor for the audit row (caller-supplied)


class MemoryRedactOutput(BaseModel):
    # False when memory_id or that version is not found, or the caller is not admin of
    # its namespace.
    redacted: bool
    refused_head: bool = False  # True when the target was the live head
    # UTC time of the scrub; None when nothing was redacted.
    redacted_at: datetime | None = None
    # The cached answers whose window held the redacted key or a profile key the redact
    # reached, deleted with it: the stored text that quoted the value. A profile key is
    # shared by every user's profile, so its rows are deleted across the org.
    cache_rows_deleted: int = 0
    # Stored `memory.ask` precedents that quoted the redacted key or a profile key the
    # redact reached, deleted with it, across the org for a profile key.
    precedent_rows_deleted: int = 0


class MemorySweepExpiredInput(BaseModel):
    """Run one expiry-sweep pass now — the same pass the
    `memory_expiry_sweep` worker runs on its interval, scoped to an org and
    optionally a namespace. `dry_run=True` reports what would happen."""

    org: str
    namespace: str | None = None
    # At most this many expired rows handled in the pass.
    limit: int = Field(default=500, ge=1, le=5000)
    # True counts and samples what the pass would do, writing nothing.
    dry_run: bool = False


class MemorySweepExpiredOutput(BaseModel):
    # Expired live rows the pass read.
    scanned: int = 0
    # Rows tombstoned (or that would be, under `dry_run`).
    tombstoned: int = 0
    # Rows demoted: expiry cleared, confidence scaled down (or would be, under `dry_run`).
    demoted: int = 0
    # Rows anonymized: value replaced by a marker, embeddings tombstoned, the shell row
    # kept (or would be, under `dry_run`).
    anonymized: int = 0
    # Rows left as they are because a lock refuses their post-action; reads still
    # hide them.
    locked_skipped: int = 0
    # Echo of the request's `dry_run`.
    dry_run: bool = False
    # Up to 20 handled rows: memory_key, namespace, agent, action, expired_at.
    sample: list[dict[str, Any]] = Field(default_factory=list)
    # Errors of the pass (`scan:`, `insert:`, `mirror:`); empty on a clean run.
    failures: list[str] = Field(default_factory=list)


# ============================================================
# memory.verify — attest to one row; the attestation binds to its content.
#
# Reinforce with provenance. `pinned` protects a fact from conflict eviction
# and says nothing about who checked it; `confidence` is the writer's own
# number. `memory.verify` records an actor (`human:<subject>` or
# `process:<subject>`, decided from the caller's token, never the body) and
# the canonical content hash attested to. Readers derive a state —
# human / process / stale / none — against the current hash, so an edit after
# the attestation shows as `stale` rather than silently keeping the badge.
# ============================================================


class MemoryVerifyInput(BaseModel):
    """Attest to (or revoke the attestation on) one memory.

    Address the row by `memory_id`, or by `namespace` + `memory_key` with the
    writer identity (`agent`, `user_id`, `session_id`; default '-').

    Under auth the actor is the caller's token subject, prefixed `human:` or
    `process:` (service account). `actor` is read only when no auth context
    exists (dev mode) and must then be well-formed. `revoke=True` clears the
    attestation.
    """

    org: str
    # The row to attest, by id; '' = address it by `namespace` + `memory_key`.
    memory_id: str = ""
    namespace: str = ""
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"
    # With `namespace` and the writer identity, the row to attest when no
    # `memory_id` is sent.
    memory_key: str = ""
    # True clears the attestation instead of making one.
    revoke: bool = False
    # Attester recorded when the server runs without auth (`human:` / `process:` +
    # subject); under auth the token decides.
    actor: str = ""


class MemoryVerifyOutput(BaseModel):
    # False when the row does not exist or the caller cannot write its namespace.
    found: bool
    # False when the call was a no-op (already attested by this actor and
    # unchanged, or revoking a row with no attestation).
    changed: bool = False
    # Stable id of the attested row; '' when not found.
    memory_id: str = ""
    # The row's key; '' when not found.
    memory_key: str = ""
    namespace: str = ""
    # The attester now on the row; '' after a revoke.
    verified_by: str = ""
    # When the attestation was made; None after a revoke.
    verified_at: datetime | None = None
    # State against the live content hash: human | process | stale | none.
    verification: str = "none"
    # Version of the row after the call (the new row's when `changed`).
    version: int = 0
    # Canonical hash the attestation is bound to (== the row's live hash).
    content_sha256: str = ""

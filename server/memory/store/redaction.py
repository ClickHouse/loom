"""The in-place redaction of a historical memory version, of every profile
version built from that memory (live heads included), and of every derived copy
of their text.

A redaction replaces content bytes and keeps rows, so audit, history and
lineage still see that a version existed, who wrote it and when. What holds
the text of a version, and what this module scrubs:

  loom.memory / loom.memory_versions, the version row itself:
    `value`, `value_json` (the FINAL projection renders `value` from it when
    it is populated), `evidence` (a quote of the value), `entities` and
    `concepts` (terms extracted from it). `value_preview` is MATERIALIZED
    from `value` and is recomputed by the mutation.
  loom.memory_embeddings, one row per identity, key, scope and model
    (`content_excerpt` and its lc/stem/lemma forms, `embedding`, `entities`,
    `concepts`), and loom.memory_chunks, the verbatim chunks of a long value
    (`content`, `caption`, `embedding`):
    these are keyed per key, not per version, and hold the text of whichever
    version wrote them. Every row of the key that does not hold the live head's
    text is deleted: an embedding row judged by its real content hash (64
    lowercase hex) against the live head's, by its excerpt otherwise, a chunk by
    the chunks ClickHouse holds as live, or under Postgres, for a `memory.set`
    chunk, by the chunks the authority's live heads of the key write. That takes
    superseded versions merges have not dropped yet, retired chunks, and, when
    the key has no live head (it was deleted first, as a redaction of the newest
    text requires), all of them. The rows serving the live head are never
    touched, nor an embedding row a newer write stamped after the redaction
    began, unless it carries the redacted text, nor a fence that holds no text
    at all. Chunks carry no version: a write landing between the head read and
    the purge loses its chunks until it is written again.
  loom.memory_code_links, the key's links to code: a link made from another
    value of the key (a superseded or deleted row, or one the worker has not
    relinked yet) may hold `suggestion`, a rewrite of that value's text; it is
    blanked. The links made from the live head's value keep theirs.
  The user's profiles built from the memory: a profile worker writes what a
    model made of the user's facts and names the facts it read in the row's
    trace (`profile.fold:<label>:<memory ids>`). Every version whose trace
    names the redacted memory is scrubbed the same way, head included, since a
    trace names the memory and not its version (`profile_keys_built_from`).

Out of scope, with the reason:
  `tags`, `files`: caller-supplied labels, not the value's text.
  `content_sha256`: a hash, kept so audit rows still verify.
  loom.memory_evidence: the source transcript a fact was extracted from, not
    the memory; erasure owns it.
  memory_belief_sources, memory_chunk_sources: links by id and hash, no text.
  loom.shared and the entity graph tables: other planes, with their own
    erasure steps.
  The answer cache and the ask precedents quoting the key are deleted by the
  memory.redact handler.
  Stored idempotency answers (24 hours) may quote the text in a peer's excerpt:
    a replay of the same request with the same key, by its own caller; an
    erasure removes them.

The version rows are rewritten with ALTER UPDATE (mutations_sync = 2). Each
derived-row purge has a count twin over the same predicate, so
a second run finds nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from server.core import pg
from server.core.ch import sequential_reads
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.redaction_lineage import (
    REDACTED_VALUE,
    Version,
    is_content_hash,
    later_copies,
)
from server.memory.domain.set_chunk_session import set_chunk_session_id
from server.memory.store import pg_memory
from server.memory.store.rows import IdentityKey
from server.memory.store.sql import _SEQUENTIAL

# Each rewrite is spelled out per table, so the mutation inventory test sees it.
_MEMORY_SCRUB = """ALTER TABLE loom.memory UPDATE
    value = '"[REDACTED]"', value_json = '{}', evidence = '', entities = [], concepts = []"""
_VERSIONS_SCRUB = """ALTER TABLE loom.memory_versions UPDATE
    value = '"[REDACTED]"', value_json = '{}', evidence = '', entities = [], concepts = []"""
# The derived rows that do not serve the live head are deleted, not rewritten:
# an UPDATE of memory_embeddings leaves patch parts that switch the fact leg's
# codes-first read off, and the erasure purges delete these tables the same
# way (a lightweight DELETE writes only the row mask; the bytes leave at the
# part's next merge).
_EMBEDDINGS_PURGE = "DELETE FROM loom.memory_embeddings"
_CHUNKS_PURGE = "DELETE FROM loom.memory_chunks"
_LINKS_SCRUB = "ALTER TABLE loom.memory_code_links UPDATE suggestion = '', judged_hash = ''"
_MARK_REDACTED = ", redacted_at = {ts:DateTime}, redaction_reason = {rb:String}"
# The user's profile versions whose trace names the memory among those they were
# built from (`profile.fold:<label>:<id>,<id>,...`). Redacted versions count too:
# a retried redact must reach the same profile keys as the call that failed.
# The builders write only under agent '-', which lets the sort key prune to the user.
_BUILT_FROM = """namespace = {ns:String} AND agent = '-' AND user_id = {u:String}
  AND memory_type = 'profile'
  AND startsWith(source_trace, 'profile.fold:')
  AND has(splitByRegexp('[:,]', source_trace), toString({mid:UUID}))"""
# The named version and the later versions of its key that copy its text (`copies`,
# `redaction_lineage.later_copies`), by the key; the profile versions built from the
# memory. The named version takes the latest call's time and reason; a copy or a profile
# version already redacted keeps its first.
_REDACTED_ROWS = (
    "org = {org:String}\n"
    "  AND ((namespace = {ns:String} AND agent = {agent:String} AND user_id = {u:String}"
    " AND session_id = {sid:String} AND memory_key = {key:String} AND memory_id = {mid:UUID}"
    " AND (version = {v:UInt64} OR (version IN {copies:Array(UInt64)} AND redacted_at IS NULL)))"
    " OR (redacted_at IS NULL AND " + _BUILT_FROM + "))"
)


@dataclass(frozen=True)
class Scrub:
    """One table's rewrite: `update` is the ALTER ... UPDATE or DELETE FROM
    head, and `where` selects the rows that still hold text."""

    table: str
    update: str
    where: str
    params: dict[str, Any] = field(default_factory=dict)

    def count_sql(self) -> str:
        return f"SELECT count() AS n FROM {self.table} WHERE {self.where}"

    def update_sql(self) -> str:
        sync = "mutations_sync" if self.update.startswith("ALTER") else "lightweight_deletes_sync"
        return f"{self.update}\nWHERE {self.where}\nSETTINGS {sync} = 2"


async def run_scrubs(
    scrubs: list[Scrub], *, apply: bool, ch: _CHClient | None = None
) -> dict[str, int]:
    """Rows holding text per table; with `apply`, rewrite them (a table whose
    count is 0 is not rewritten)."""
    ch = ch or _default_client()
    counts: dict[str, int] = {}
    for s in scrubs:
        row = await ch.query_one(s.count_sql(), params=s.params)
        n = int((row or {}).get("n") or 0)
        counts[s.table] = counts.get(s.table, 0) + n
        if apply and n:
            await ch.execute(s.update_sql(), params=s.params)
    return counts


async def delete_by_key(
    table: str, where: str, params: dict[str, Any], *, ch: _CHClient | None = None
) -> int:
    """Delete `table`'s rows matching `where`; the number deleted (none: no
    mutation is issued)."""
    counts = await run_scrubs(
        [Scrub(table, f"ALTER TABLE {table} DELETE", where, params)], apply=True, ch=ch
    )
    return counts[table]


async def delete_llm_response_cache_by_key(
    org: str, memory_key: str, *, ch: _CHClient | None = None
) -> int:
    """`memory.redact`: every cached answer whose window held the redacted
    key leaves with the value it quoted."""
    return await delete_by_key(
        "loom.llm_response_cache",
        "org = {org:String} AND has(window_keys, {key:String})",
        {"org": org, "key": memory_key},
        ch=ch,
    )


async def delete_code_answers_by_key(
    org: str, memory_key: str, *, ch: _CHClient | None = None
) -> int:
    """`memory.redact`: every cached code.ask answer whose memory excerpts held the
    redacted key, its tombstone versions included. The count that decides whether to
    delete reads sequentially, so it sees an answer another replica just stored. A
    lightweight delete, gone from every replica's reads when this returns: the table
    carries a vector column a mutation would rewrite."""
    where = "org = {org:String} AND has(memory_keys, {key:String})"
    scrub = Scrub(
        "loom.code_answers", "DELETE FROM loom.code_answers", where, {"org": org, "key": memory_key}
    )
    with sequential_reads():
        counts = await run_scrubs([scrub], apply=True, ch=ch)
    return counts["loom.code_answers"]


async def delete_code_answers_quoting(
    org: str, keys: list[str], *, ch: _CHClient | None = None
) -> None:
    """Every cached code.ask answer whose memory excerpts held any of `keys`: one
    lightweight delete, which the asking request does not wait on."""
    await (ch or _default_client()).execute(
        "DELETE FROM loom.code_answers "
        "WHERE org = {org:String} AND hasAny(memory_keys, {keys:Array(String)}) "
        "SETTINGS lightweight_deletes_sync = 0",
        params={"org": org, "keys": keys},
    )


async def keys_redacted(
    org: str, namespace: str, keys: list[str], *, ch: _CHClient | None = None
) -> bool:
    """Whether any of `keys` in `namespace` has a redacted version. Read sequentially:
    a stamp another replica just wrote must be seen."""
    ch = ch or _default_client()
    with sequential_reads():
        row = await ch.query_one(
            "SELECT 1 AS hit FROM loom.memory_versions "
            "WHERE org = {org:String} AND namespace = {ns:String} "
            "AND memory_key IN {keys:Array(String)} AND redacted_at IS NOT NULL LIMIT 1",
            params={"org": org, "ns": namespace, "keys": keys},
        )
    return row is not None


async def delete_ask_precedents_by_key(
    org: str, memory_key: str, *, ch: _CHClient | None = None
) -> int:
    """`memory.redact`: every precedent whose window or citations held the
    redacted key."""
    return await delete_by_key(
        "loom.memory_ask_precedents",
        "org = {org:String} AND has(arrayConcat(window_keys, cited_keys), {key:String})",
        {"org": org, "key": memory_key},
        ch=ch,
    )


async def redact_versions(
    key: IdentityKey,
    memory_id: str,
    version: int,
    *,
    when: datetime,
    reason_blob: str,
    head: tuple[str, int] | None,
    ch: _CHClient | None = None,
) -> list[int]:
    """Scrub the named (memory_id, version) of `key`, the later versions of the key that
    copy its text and the user's profile versions built from the memory, in two mutations
    on loom.memory and memory_versions. Under Postgres its rows are scrubbed first, only
    while the key's live head is still `head`. Returns the later copies scrubbed."""
    ch = ch or _default_client()
    org, namespace, agent, user_id, session_id, memory_key = key
    if pg.enabled():
        leases, copies = await pg_memory.redact(
            key, memory_id, version, when=when, reason_blob=reason_blob, head=head
        )
        # A publisher of the old text still in flight lands before ClickHouse is scrubbed.
        await pg.wait_out(leases)
    else:
        live = head[1] if head and UUID(str(head[0])) == UUID(str(memory_id)) else None
        copies = await _later_copies(key, memory_id, version, live=live, ch=ch)
    params = {
        "org": org,
        "ns": namespace,
        "agent": agent,
        "u": user_id,
        "sid": session_id,
        "key": memory_key,
        "mid": memory_id,
        "v": version,
        "copies": copies,
        "ts": when,
        "rb": reason_blob,
    }
    for update in (_MEMORY_SCRUB, _VERSIONS_SCRUB):
        await ch.execute(
            f"{update}{_MARK_REDACTED}\nWHERE {_REDACTED_ROWS}\nSETTINGS mutations_sync = 2",
            params=params,
        )
    return copies


async def _later_copies(
    key: IdentityKey,
    memory_id: str,
    version: int,
    *,
    live: int | None,
    ch: _CHClient,
) -> list[int]:
    """`redaction_lineage.later_copies` over the key's versions in ClickHouse. No lock holds
    the key here: a restore of the redacted text landing during the redaction is a copy
    and is scrubbed too, so the text does not come back."""
    org, namespace, agent, user_id, session_id, memory_key = key
    rows = await ch.query_all(
        f"""
        SELECT version, toStringCutToZero(content_sha256) AS sha FROM loom.memory_versions
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND agent = {{agent:String}}
          AND user_id = {{u:String}} AND session_id = {{sid:String}}
          AND memory_key = {{key:String}} AND memory_id = {{mid:UUID}} AND version >= {{v:UInt64}}
        ORDER BY version {_SEQUENTIAL}
        """,
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "u": user_id,
            "sid": session_id,
            "key": memory_key,
            "mid": memory_id,
            "v": version,
        },
    )
    history = [Version(int(r["version"]), str(r["sha"])) for r in rows]
    return later_copies(history, version, live=live)


async def profile_keys_built_from(
    org: str, namespace: str, user_id: str, memory_id: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The user's profile keys with a version built from `memory_id`, redacted or
    not, each with the newest such version. Under the Postgres authority they are read
    from the revision journal, so a lock on a profile key whose version is not yet in
    ClickHouse still refuses the redact."""
    if pg.enabled():
        return await pg_memory.profile_keys_built_from(org, namespace, user_id, memory_id)
    ch = ch or _default_client()
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT agent, session_id, memory_key, max(version) AS newest
        FROM loom.memory_versions
        WHERE org = {{org:String}} AND {_BUILT_FROM}
        GROUP BY agent, session_id, memory_key
        ORDER BY agent, session_id, memory_key
        """,
        params={"org": org, "ns": namespace, "u": user_id, "mid": memory_id},
    )
    return rows


async def _live_texts(
    ch: _CHClient, org: str, ns: str, agent: str, user_id: str, session_id: str, key: str
) -> tuple[list[str], list[str]]:
    """The excerpts of the key's live embedding rows and the hashes of its live
    chunks: the text a scrub must leave in place."""
    params = {"org": org, "ns": ns, "a": agent, "u": user_id, "s": session_id, "k": key}
    excerpts = await ch.query_all(
        """
        SELECT DISTINCT content_excerpt AS t FROM loom.memory_embeddings FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND agent = {a:String}
          AND user_id = {u:String} AND session_id = {s:String} AND memory_key = {k:String}
          AND tombstoned_at IS NULL
        """,
        params=params,
    )
    chunks = await ch.query_all(
        """
        SELECT DISTINCT content_sha256 AS t FROM loom.memory_chunks FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND agent = {a:String}
          AND user_id = {u:String} AND parent_memory_key = {k:String}
          AND tombstoned_at IS NULL
        """,
        params=params,
    )
    return [str(r["t"]) for r in excerpts], [_hash_text(r["t"]) for r in chunks]


async def set_chunk_count(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """One past the highest index of the key's `memory.set` chunk rows, retired ones
    included (0 with none): how far a plan of a live head must reach to name every chunk
    it wrote."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        SELECT ifNull(maxOrNull(chunk_index) + 1, 0) AS n FROM loom.memory_chunks
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND agent = {{a:String}}
          AND user_id = {{u:String}} AND session_id = {{s:String}}
          AND parent_memory_key = {{k:String}}
        {_SEQUENTIAL}
        """,
        params={
            "org": org,
            "ns": namespace,
            "a": agent,
            "u": user_id,
            "s": set_chunk_session_id(memory_key),
            "k": memory_key,
        },
    )
    return int((row or {}).get("n") or 0)


def _hash_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace").rstrip("\x00")
    return str(value)


async def key_derivative_scrubs(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    head_live: bool,
    live_link_inputs: str = "",
    through: int | None,
    redacted_sha: str = "",
    live_sha: str = "",
    set_chunks_kept: Sequence[str] | None,
    ch: _CHClient | None = None,
) -> list[Scrub]:
    """The purges of one key's embedding and chunk rows that hold text the live head does
    not, and the blanking of its code links' suggestions not made from the live head's
    value (`live_link_inputs`), by the module docstring's rules.

    head_live: False when the key has no live head; every row is stale then.
    through: the redaction's version stamp; a hashed row stamped after it is a newer
        write's (None: no bound).
    redacted_sha / live_sha: the redacted version's and the live head's content hashes.
    set_chunks_kept: the chunk hashes the Postgres authority's live heads write; None on
        the ClickHouse authority."""
    ch = ch or _default_client()
    live_excerpts: list[str] = []
    live_chunks: list[str] = []
    if head_live:
        live_excerpts, live_chunks = await _live_texts(
            ch, org, namespace, agent, user_id, session_id, memory_key
        )
    ident = {"org": org, "ns": namespace, "a": agent, "u": user_id, "k": memory_key}
    bound: dict[str, Any] = {}
    hashed = "match(content_sha256, '^[0-9a-f]{64}$')"  # a real hash, as `is_content_hash`
    # Not the live head's text: by the row's real hash when it has one, else its excerpt.
    other_text = "content_excerpt NOT IN {live:Array(String)}"
    if head_live and is_content_hash(live_sha):
        other_text = f"if({hashed}, content_sha256 != {{ls:String}}, {other_text})"
        bound["ls"] = live_sha
    # Not a newer write's: a row without a real hash cannot be matched to a write, so it
    # is judged whenever it landed.
    not_newer = "1"
    if through is not None:
        not_newer = f"(version <= {{tv:UInt64}} OR NOT ({hashed}))"
        bound["tv"] = through
    # Or the redacted text itself, whenever it landed.
    redacted = "0"
    if is_content_hash(redacted_sha) and redacted_sha != live_sha:
        redacted = "content_sha256 = {rh:String}"
        bound["rh"] = redacted_sha
    # Never a fence: it holds no text (no excerpt, no terms, a placeholder vector) and
    # keeps an older vector in flight hidden under a 'high' head.
    not_fence = (
        "NOT (content_excerpt = '' AND embedding_dim = 0 AND empty(entities) AND empty(concepts))"
    )
    stale = f"((({other_text}) AND {not_newer}) OR {redacted}) AND {not_fence}"
    stale_chunk = "content_sha256 NOT IN {live:Array(FixedString(64))}"
    chunk_params: dict[str, Any] = {"live": live_chunks}
    if set_chunks_kept is not None:
        # ClickHouse may still hold an older value's chunks as live: a memory.set chunk is
        # judged by what the authority's heads write instead.
        stale_chunk = (
            "if(session_id = {ss:String}, "
            f"content_sha256 NOT IN {{kept:Array(FixedString(64))}}, {stale_chunk})"
        )
        chunk_params |= {"ss": set_chunk_session_id(memory_key), "kept": list(set_chunks_kept)}
    return [
        Scrub(
            "loom.memory_embeddings",
            _EMBEDDINGS_PURGE,
            "org = {org:String} AND namespace = {ns:String} AND agent = {a:String} "
            "AND user_id = {u:String} AND session_id = {s:String} "
            f"AND memory_key = {{k:String}} AND {stale}",
            {**ident, "s": session_id, "live": live_excerpts, **bound},
        ),
        Scrub(
            "loom.memory_chunks",
            _CHUNKS_PURGE,
            "org = {org:String} AND namespace = {ns:String} AND agent = {a:String} "
            "AND user_id = {u:String} AND parent_memory_key = {k:String} "
            f"AND {stale_chunk}",
            {**ident, **chunk_params},
        ),
        Scrub(
            "loom.memory_code_links",
            _LINKS_SCRUB,
            "org = {org:String} AND namespace = {ns:String} AND agent = {a:String} "
            "AND user_id = {u:String} AND session_id = {s:String} AND memory_key = {k:String} "
            "AND suggestion != '' AND inputs_hash != {live:String}",
            {**ident, "s": session_id, "live": live_link_inputs},
        ),
    ]


__all__ = [
    "REDACTED_VALUE",
    "Scrub",
    "key_derivative_scrubs",
    "set_chunk_count",
    "profile_keys_built_from",
    "redact_versions",
    "run_scrubs",
]

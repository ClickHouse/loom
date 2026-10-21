"""The in-place redaction of one historical memory version, and every derived
copy of its text.

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
    version wrote them. Every row of the key whose excerpt (embeddings) or
    chunk hash (chunks) is not the live head's is deleted: superseded
    versions merges have not dropped yet, retired chunks, and, when the key
    has no live head (it was deleted first, as a redaction of the newest text
    requires), all of them. The rows serving the live head are never
    touched.
  loom.memory_code_links, the key's links to code: a link made from another
    value of the key (a superseded or deleted row, or one the worker has not
    relinked yet) may hold `suggestion`, a rewrite of that value's text; it is
    blanked. The links made from the live head's value keep theirs.
  The user's profiles built from the memory: a profile worker writes what a
    model made of the user's facts and names the facts it read in the row's
    trace (`profile.fold:<label>:<memory ids>`). Every version whose trace
    names the redacted memory is scrubbed the same way, head included, since a
    trace names the memory and not its version (`profile_versions_built_from`).

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

The version rows are rewritten with ALTER UPDATE (mutations_sync = 2). Each
derived-row purge has a count twin over the same predicate, so
a second run finds nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client

REDACTED_VALUE = '"[REDACTED]"'  # a JSON string, as memory.set stores a string value

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
_LISTED_VERSIONS = (
    "org = {org:String} AND (memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}"
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
    org: str,
    versions: list[tuple[str, int]],
    *,
    when: datetime,
    reason_blob: str,
    ch: _CHClient | None = None,
) -> None:
    """Scrub every listed (memory_id, version) on loom.memory and the
    memory_versions mirror (ALTER UPDATE does not pass through
    memory_versions_mv), in one statement per table: two mutations however many
    versions match. The caller decides which versions may be scrubbed; versions
    share a memory_id, so the version in each pair is what keeps the others."""
    if not versions:
        return
    ch = ch or _default_client()
    params = {"org": org, "pairs": versions, "ts": when, "rb": reason_blob}
    for update in (_MEMORY_SCRUB, _VERSIONS_SCRUB):
        await ch.execute(
            f"{update}{_MARK_REDACTED}\nWHERE {_LISTED_VERSIONS}\nSETTINGS mutations_sync = 2",
            params=params,
        )


async def profile_versions_built_from(
    org: str, namespace: str, user_id: str, memory_id: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The user's profile versions, not redacted yet, whose trace names `memory_id`
    among the memories they were built from (`profile.fold:<label>:<id>,<id>,...`)."""
    ch = ch or _default_client()
    cols = "toString(memory_id) AS memory_id, version, agent, session_id, memory_key"
    where = """org = {org:String} AND namespace = {ns:String} AND user_id = {u:String}
        AND memory_type = 'profile' AND redacted_at IS NULL
        AND startsWith(source_trace, 'profile.fold:')
        AND has(splitByRegexp('[:,]', source_trace), toString({mid:UUID}))"""
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT memory_id, version, agent, session_id, memory_key
        FROM (
            SELECT {cols} FROM loom.memory_versions WHERE {where}
            UNION ALL
            SELECT {cols} FROM loom.memory WHERE {where}
        )
        LIMIT 1 BY memory_id, version
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
    ch: _CHClient | None = None,
) -> list[Scrub]:
    """Purges of the embedding and chunk rows of one key that hold text the
    live head does not (every row of the key when `head_live` is False), and the
    suggestions of its code links not made from the live head's value
    (`live_link_inputs`, their inputs hash)."""
    ch = ch or _default_client()
    live_excerpts: list[str] = []
    live_chunks: list[str] = []
    if head_live:
        live_excerpts, live_chunks = await _live_texts(
            ch, org, namespace, agent, user_id, session_id, memory_key
        )
    ident = {"org": org, "ns": namespace, "a": agent, "u": user_id, "k": memory_key}
    return [
        Scrub(
            "loom.memory_embeddings",
            _EMBEDDINGS_PURGE,
            "org = {org:String} AND namespace = {ns:String} AND agent = {a:String} "
            "AND user_id = {u:String} AND session_id = {s:String} "
            "AND memory_key = {k:String} AND content_excerpt NOT IN {live:Array(String)}",
            {**ident, "s": session_id, "live": live_excerpts},
        ),
        Scrub(
            "loom.memory_chunks",
            _CHUNKS_PURGE,
            "org = {org:String} AND namespace = {ns:String} AND agent = {a:String} "
            "AND user_id = {u:String} AND parent_memory_key = {k:String} "
            "AND content_sha256 NOT IN {live:Array(FixedString(64))}",
            {**ident, "live": live_chunks},
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
    "profile_versions_built_from",
    "redact_versions",
    "run_scrubs",
]

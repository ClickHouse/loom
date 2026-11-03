"""The mentor's reads: the agent's calls in the window from `loom.agent_events`
(sort key `(org, namespace, agent, ts)`: bounded scans) and the namespace facts a
rule needs — its live memory rows, documents, records, runs and the grants the
agent reads code under — and the usage-evidence ladder `mentor.brief` reads at each
brief (call outcomes, indexed keys, reported keys, graded runs, the namespace's
retention). Each method is one statement (two for the credited-keys
pair) and returns plain values; the mentor decides what they mean."""

from __future__ import annotations

from typing import Any

from server.core.grant_sql import LIVE_GRANT
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES
from server.memory.domain.namespace_shapes import STRUCTURED_TYPES
from server.memory.domain.temporal_sql import served_status_sql
from server.memory.domain.transcript_rows import TRANSCRIPT_ROW_SQL as _TRANSCRIPT_ROW
from server.observability.consumer import INSPECTOR_CONSUMERS

_TRACKER_TAG_RE = "(^|[^a-z])(test|tests|flaky|ci|regression|crash|failure|failing)([^a-z]|$)"
_KEY_ID_RE = r"\d{2,}$"
_KEY_PREFIX_RE = r"^(.*?)[-_/:.]?\d{2,}$"
# A tag that names a file (a slash, or a file extension) is a location the
# write is about, not its subject: `entity:file_path:tests/x.py` does not
# make a write "about tests".
_PATH_TAG_RE = r"/|\.[A-Za-z0-9]{1,10}$"
_COORD_SEGMENTS = ("lease", "lock", "freeze", "claim")

_SCOPE = "org = {org:String} AND namespace = {ns:String}"
_WIN = "ts >= now() - INTERVAL {win:UInt32} SECOND"
# The dashboard's and the assistant's calls inspect a namespace on a person's behalf;
# they are not the agent's own use of Loom.
_AGENT_CALL = "consumer NOT IN {inspectors:Array(String)}"
_LEDGER_AGENT = (
    f"FROM loom.agent_events WHERE {_SCOPE} AND agent = {{agent:String}} AND {_WIN} "
    f"AND kind != 'worker' AND {_AGENT_CALL}"
)
_LEDGER_NS = (
    f"FROM loom.agent_events WHERE {_SCOPE} AND {_WIN} AND kind != 'worker' AND {_AGENT_CALL}"
)
# A session the client named. '-' and '*' are what the transports write
# when a call names none; a week of such calls is not one session.
_REAL_SESSION = "session_id NOT IN ('', '-', '*')"
# A row the embedder indexes as prose: not a non-searchable type, not a type
# that is structured by design (procedures are graphs).
_SEARCHABLE_ROW = (
    "memory_type NOT IN {ns_types:Array(String)} AND memory_type NOT IN {structured:Array(String)}"
)


# The keys the agent's runs in the window reported relying on (`run.complete`
# `memories_used`: memory keys and `chunk:` ids), one row per key credited.
_CREDITED_KEYS = (
    "SELECT arrayJoin(memories_used) AS k FROM loom.agent_runs FINAL "
    f"WHERE {_SCOPE} AND agent = {{agent:String}} "
    "AND started_at >= now() - INTERVAL {win:UInt32} SECOND"
)


def _in(ops: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{o}'" for o in ops) + ")"


class ProfileReads:
    """The statements of one mentor profile read: (org, namespace, agent) over
    the last `window_seconds`. `ops` names the operation classes the rules
    count (`sets`, `gets`, `searches`, `packs`); `limits` the value-size
    thresholds (`large`, `huge`, `advisory`, `clip`, `episodic_chars`) and
    `ttl`, the window of the shown-hints read."""

    def __init__(
        self,
        org: str,
        namespace: str,
        agent: str,
        window_seconds: int,
        *,
        ops: dict[str, tuple[str, ...]],
        limits: dict[str, int],
        ch: _CHClient | None = None,
    ) -> None:
        self.ch = ch or _default_client()
        self.sets, self.gets = ops["sets"], ops["gets"]
        self.searches, self.packs = ops["searches"], ops["packs"]
        self.params: dict[str, Any] = {
            "org": org,
            "ns": namespace,
            "agent": agent,
            "inspectors": list(INSPECTOR_CONSUMERS),
            "win": int(window_seconds),
            "ttl": int(limits["ttl"]),
            "tracker_re": _TRACKER_TAG_RE,
            "id_re": _KEY_ID_RE,
            "prefix_re": _KEY_PREFIX_RE,
            "path_tag_re": _PATH_TAG_RE,
            "large": int(limits["large"]),
            "huge": int(limits["huge"]),
            "coord": list(_COORD_SEGMENTS),
            "ns_types": sorted(NON_SEARCHABLE_TYPES),
            "structured": sorted(STRUCTURED_TYPES),
            "advisory": int(limits["advisory"]),
            "clip": int(limits["clip"]),
            "episodic_chars": int(limits["episodic_chars"]),
        }

    async def _rows(self, sql: str, **extra: Any) -> list[dict[str, Any]]:
        return list(await self.ch.query_all(sql, params={**self.params, **extra}))

    async def _one(self, sql: str, **extra: Any) -> dict[str, Any]:
        r = await self.ch.query_one(sql, params={**self.params, **extra})
        return dict(r) if r else {}

    # -- the agent's own calls ------------------------------------------------

    async def op_counts(self) -> dict[str, int]:
        rows = await self._rows(
            f"SELECT operation, count() AS n {_LEDGER_AGENT} "
            "GROUP BY operation ORDER BY n DESC LIMIT 400"
        )
        return {str(r["operation"]): int(r["n"]) for r in rows}

    async def named_sessions(self) -> int:
        r = await self._one(
            f"SELECT uniqExact(session_id) AS sessions {_LEDGER_AGENT} AND {_REAL_SESSION}"
        )
        return int(r.get("sessions") or 0)

    async def shown_codes(self) -> list[str]:
        """The profile hints the ledger recorded on this agent's responses
        within the TTL, from any worker (`out_hint_code`)."""
        r = await self._one(
            f"""
            SELECT groupUniqArray(20)(args['out_hint_code']) AS codes
            FROM loom.agent_events
            WHERE {_SCOPE} AND agent = {{agent:String}}
              AND ts >= now() - INTERVAL {{ttl:UInt32}} SECOND
              AND args['out_hint_code'] != '' AND {_AGENT_CALL}
            """
        )
        return sorted(str(c) for c in (r.get("codes") or []) if c)

    async def session_openings(self) -> dict[str, int]:
        """Named sessions of at least two distinct ops, and how many opened
        with a pack: a session holding one op has no opening to judge (a bot
        that names a session on `run.begin` alone would read as thousands of
        sessions "never opened with a pack")."""
        r = await self._one(
            f"""
            SELECT count() AS sessions,
                   countIf(first_op IN {_in(self.packs)}) AS pack_first
            FROM (
                SELECT session_id, argMin(operation, ts) AS first_op
                {_LEDGER_AGENT} AND {_REAL_SESSION}
                GROUP BY session_id
                HAVING uniqExact(operation) >= 2
            )
            """
        )
        return {
            "sessions": int(r.get("sessions") or 0),
            "pack_first": int(r.get("pack_first") or 0),
        }

    async def outcomes(self) -> dict[str, int]:
        r = await self._one(
            f"""
            SELECT countIf(operation = 'memory.set' AND args['out_duplicate_of'] != '') AS dup_sets,
                   countIf(operation IN {_in(self.searches)}
                           AND args['out_abstain'] = 'true') AS abstains,
                   countIf(operation = 'memory.set'
                           AND toUInt32OrZero(splitByChar('/', args['value'])[-1])
                               >= {{large:UInt32}}) AS large_sets,
                   countIf(operation = 'memory.set'
                           AND toUInt32OrZero(splitByChar('/', args['value'])[-1])
                               >= {{huge:UInt32}}) AS huge_sets,
                   countIf(operation IN {_in(self.sets)}
                           AND arrayExists(t -> match(lower(t), {{tracker_re:String}}),
                                           arrayFilter(t -> NOT match(t, {{path_tag_re:String}}),
                                                       splitByChar(',', args['tags']))))
                       AS tracker_tagged_sets,
                   countIf(operation IN {_in(self.sets)}
                           AND match(memory_key, {{id_re:String}})) AS id_keyed_sets
            {_LEDGER_AGENT}
            """
        )
        keys = (
            "dup_sets",
            "abstains",
            "large_sets",
            "huge_sets",
            "tracker_tagged_sets",
            "id_keyed_sets",
        )
        return {k: int(r.get(k) or 0) for k in keys}

    async def credited_keys(self) -> dict[str, int]:
        """The keys the agent's runs said they relied on (`agent_runs.memories_used`):
        how many, how many chunk ids, how many no live row answers."""
        r = await self._one(
            f"""
            WITH credited AS ({_CREDITED_KEYS})
            SELECT count() AS credited_keys,
                   countIf(startsWith(k, 'chunk:')) AS credited_chunk_keys,
                   countIf(NOT startsWith(k, 'chunk:') AND k NOT IN (
                       SELECT memory_key FROM loom.memory FINAL
                       WHERE {_SCOPE} AND tombstoned_at IS NULL
                   )) AS credited_unresolvable
            FROM credited
            """
        )
        keys = ("credited_keys", "credited_chunk_keys", "credited_unresolvable")
        return {k: int(r.get(k) or 0) for k in keys}

    async def key_reads(self) -> dict[str, int]:
        """Keys the agent's successful gets asked for and missed (the ledger's
        `out_found`, `out_found_n` / `out_missing_n`)."""
        m = await self._one(
            f"""
            SELECT sumIf(if(args['out_found'] != '', 1,
                            toUInt64OrZero(args['out_found_n'])
                            + toUInt64OrZero(args['out_missing_n'])),
                         operation IN {_in(self.gets)}) AS keys_asked,
                   sumIf(if(args['out_found'] = 'false', 1,
                            toUInt64OrZero(args['out_missing_n'])),
                         operation IN {_in(self.gets)}) AS keys_missed
            {_LEDGER_AGENT} AND success = 1
            """
        )
        return {k: int(m.get(k) or 0) for k in ("keys_asked", "keys_missed")}

    async def hot_keys(self) -> dict[str, Any]:
        """The key set and the key read most often within one named session."""
        r = await self._one(
            f"""
            SELECT maxIf(n, op_class = 'set') AS max_sets_one_key,
                   maxIf(n, op_class = 'get') AS max_gets_one_key,
                   argMaxIf(memory_key, n, op_class = 'set') AS hot_set_key,
                   argMaxIf(memory_key, n, op_class = 'get') AS hot_get_key
            FROM (
                SELECT session_id, memory_key,
                       if(operation IN {_in(self.sets)}, 'set', 'get') AS op_class,
                       count() AS n
                {_LEDGER_AGENT} AND memory_key != '' AND {_REAL_SESSION}
                  AND operation IN {_in(self.sets + self.gets)}
                GROUP BY session_id, memory_key, op_class
            )
            """
        )
        return {
            "max_sets_one_key": int(r.get("max_sets_one_key") or 0),
            "max_gets_one_key": int(r.get("max_gets_one_key") or 0),
            "hot_set_key": str(r.get("hot_set_key") or ""),
            "hot_get_key": str(r.get("hot_get_key") or ""),
        }

    async def live_value_chars(self, key: str) -> int:
        row = await self._one(
            f"SELECT length(value) AS hot_key_chars FROM loom.memory FINAL "
            f"WHERE {_SCOPE} AND memory_key = {{hot_key:String}} "
            "AND tombstoned_at IS NULL LIMIT 1",
            hot_key=key,
        )
        return int(row.get("hot_key_chars") or 0)

    async def top_id_prefix(self) -> dict[str, Any]:
        r = await self._one(
            f"""
            SELECT max(n) AS top_prefix_sets, argMax(prefix, n) AS top_prefix
            FROM (
                SELECT extract(memory_key, {{prefix_re:String}}) AS prefix, count() AS n
                {_LEDGER_AGENT} AND operation IN {_in(self.sets)}
                  AND match(memory_key, {{id_re:String}})
                GROUP BY prefix
            )
            """
        )
        return {
            "top_prefix_sets": int(r.get("top_prefix_sets") or 0),
            "top_prefix": str(r.get("top_prefix") or ""),
        }

    # -- the namespace --------------------------------------------------------

    async def namespace_ledger(self) -> dict[str, int]:
        r = await self._one(
            f"""
            SELECT countIf(operation LIKE 'conflicts.%') AS conflict_ops,
                   uniqExactIf(agent, operation IN {_in(self.sets)}
                               AND agent NOT IN ('', '-', '*')) AS writers
            {_LEDGER_NS}
            """
        )
        return {k: int(r.get(k) or 0) for k in ("conflict_ops", "writers")}

    async def namespace_rows(self) -> dict[str, int]:
        # A transcript row's entities (its files) are no graph worth walking,
        # and transcripts can be most of a namespace's entity-bearing rows.
        r = await self._one(
            f"""
            SELECT count() AS live_rows,
                   countIf(length(entities) > 0 AND NOT {_TRANSCRIPT_ROW}) AS entity_rows,
                   countIf(status = 'conflicted') AS conflicted_rows,
                   countIf(hasAny(splitByChar('/', lower(memory_key)), {{coord:Array(String)}})
                           OR hasAny(splitByChar(':', lower(memory_key)),
                                     {{coord:Array(String)}})) AS coord_keys
            FROM loom.memory FINAL
            WHERE {_SCOPE} AND tombstoned_at IS NULL
            """
        )
        return {
            k: int(r.get(k) or 0)
            for k in ("live_rows", "entity_rows", "conflicted_rows", "coord_keys")
        }

    async def live_documents(self) -> int:
        r = await self._one(
            f"SELECT count() AS n FROM loom.documents WHERE {_SCOPE} AND superseded_at IS NULL"
        )
        return int(r.get("n") or 0)

    async def active_records(self) -> int:
        r = await self._one(
            f"SELECT count() AS n FROM loom.memory_records WHERE {_SCOPE} AND status = 'active'"
        )
        return int(r.get("n") or 0)

    async def row_shapes(self) -> dict[str, int]:
        """This agent's live rows that are documents or checkpoints stored as
        memory (the audit's thresholds), never the types that are structured
        by design (a procedure is a graph)."""
        r = await self._one(
            f"""
            SELECT countIf({_SEARCHABLE_ROW} AND length(value) > {{advisory:UInt32}}) AS oversized,
                   countIf({_SEARCHABLE_ROW} AND length(value) > {{clip:UInt32}}
                           AND (JSONType(value) IN ('Object', 'Array')
                                OR (JSONType(value) = 'String'
                                    AND JSONType(JSONExtractString(value)) IN ('Object', 'Array'))))
                       AS json_prefix,
                   countIf(memory_type = 'episodic' AND length(value) > {{episodic_chars:UInt32}})
                       AS episodic_blobs
            FROM loom.memory FINAL
            WHERE {_SCOPE} AND agent = {{agent:String}} AND tombstoned_at IS NULL
            """
        )
        return {k: int(r.get(k) or 0) for k in ("oversized", "json_prefix", "episodic_blobs")}

    async def subjects(self) -> list[str]:
        """The subjects the agent called under in the window (at most 8)."""
        rows = await self._rows(
            f"SELECT DISTINCT subject {_LEDGER_AGENT} AND subject != '' LIMIT 8"
        )
        return [str(r["subject"]) for r in rows if r.get("subject")]

    async def granted_namespaces(self, subjects: list[str]) -> set[str]:
        """The namespaces live grants give these subjects; a service account
        inherits its creator's grants when it was minted to."""
        rows = await self._rows(
            """
            SELECT DISTINCT namespace FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND {live}
              AND subject IN (
                  SELECT arrayJoin({subjects:Array(String)})
                  UNION ALL
                  SELECT created_by FROM loom.service_accounts FINAL
                  WHERE org = {org:String} AND sa_subject IN {subjects:Array(String)}
                    AND inherit_creator_grants AND revoked_at IS NULL
              )
            """.replace("{live}", LIVE_GRANT),
            subjects=subjects,
        )
        return {str(r["namespace"]) for r in rows if r.get("namespace")}


# ---------------------------------------------------------------------------
# mentor.brief
# ---------------------------------------------------------------------------


async def top_writers(
    org: str,
    namespace: str,
    window_seconds: int,
    *,
    write_ops: tuple[str, ...],
    anonymous: frozenset[str],
    limit: int,
    ch: _CHClient | None = None,
) -> list[str]:
    """The namespace's writers in the window, most active first; the
    anonymous names and the dashboard's own traffic left out."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT agent, count() AS n FROM loom.agent_events
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND ts >= now() - INTERVAL {{win:UInt32}} SECOND AND kind != 'worker'
          AND operation IN {_in(write_ops)} AND agent NOT IN {{anon:Array(String)}}
          AND {_AGENT_CALL}
        GROUP BY agent ORDER BY n DESC LIMIT {{lim:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "win": int(window_seconds),
            "anon": sorted(anonymous),
            "lim": int(limit),
            "inspectors": list(INSPECTOR_CONSUMERS),
        },
    )
    return [str(r["agent"]) for r in rows]


async def hints_shown(
    org: str,
    namespace: str,
    agent: str,
    window_seconds: int,
    *,
    cap: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(session_id, code, first_ts) per client-named session and hint code that
    reached the agent in the window, newest first, at most `cap`."""
    ch = ch or _default_client()
    return list(
        await ch.query_all(
            f"""
            SELECT session_id, args['out_hint_code'] AS code, min(ts) AS first_ts
            FROM loom.agent_events
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND agent = {{agent:String}}
              AND ts >= now() - INTERVAL {{win:UInt32}} SECOND
              AND args['out_hint_code'] != '' AND session_id NOT IN ('', '-', '*')
              AND {_AGENT_CALL}
            GROUP BY session_id, code
            ORDER BY first_ts DESC
            LIMIT {{cap:UInt32}}
            """,
            params={
                "org": org,
                "ns": namespace,
                "agent": agent,
                "win": int(window_seconds),
                "cap": int(cap),
                "inspectors": list(INSPECTOR_CONSUMERS),
            },
        )
    )


async def last_calls(
    org: str,
    namespace: str,
    agent: str,
    window_seconds: int,
    *,
    sessions: list[str],
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(session_id, operation, last_ts): each op's latest call in each of
    these sessions within the window."""
    ch = ch or _default_client()
    return list(
        await ch.query_all(
            f"""
            SELECT session_id, operation, max(ts) AS last_ts
            FROM loom.agent_events
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND agent = {{agent:String}}
              AND ts >= now() - INTERVAL {{win:UInt32}} SECOND
              AND session_id IN {{sessions:Array(String)}} AND {_AGENT_CALL}
            GROUP BY session_id, operation
            """,
            params={
                "org": org,
                "ns": namespace,
                "agent": agent,
                "win": int(window_seconds),
                "sessions": sessions,
                "inspectors": list(INSPECTOR_CONSUMERS),
            },
        )
    )


# A call the evidence ladder counts as the agent's: a request row (not a deferred verdict
# a call left behind), not the dashboard's or the assistant's traffic, and not a brief,
# which is ledgered under the agent it names.
_COUNTED_CALL = f"kind = 'request' AND {_AGENT_CALL} AND operation != 'mentor.brief'"


async def call_evidence(
    org: str,
    namespace: str,
    agent: str,
    window_seconds: int,
    *,
    searches: tuple[str, ...],
    gets: tuple[str, ...],
    ch: _CHClient | None = None,
) -> dict[str, Any]:
    """What the agent's calls in the window show, from the call ledger: attempts,
    successes, `memory.set` calls that stored a row (`noop` false: a merge or a dry run
    answers true), reads that answered (`abstain` false, a hit, a found key), and the
    oldest row the ledger still holds for the agent. Each count needs the result column
    that shows it, so an idempotent replay, which records no result, counts as an attempt
    and nothing more."""
    ch = ch or _default_client()
    # An aggregate with no GROUP BY answers exactly one row.
    (row,) = await ch.query_all(
        f"""
        SELECT countIf({_COUNTED_CALL}) AS attempts,
               countIf({_COUNTED_CALL} AND success) AS successes,
               countIf({_COUNTED_CALL} AND success AND operation = 'memory.set'
                       AND args['out_noop'] = 'false') AS committed_writes,
               countIf({_COUNTED_CALL} AND success AND (
                   (operation IN {_in(searches)} AND args['out_abstain'] = 'false')
                   OR (operation = 'search.query'
                       AND toUInt64OrZero(args['out_section_hits']) > 0)
                   OR (operation IN {_in(gets)} AND (args['out_found'] = 'true'
                       OR toUInt64OrZero(args['out_found_n']) > 0)))) AS returned_hits,
               minOrNull(ts) AS oldest_row
        FROM loom.agent_events
        WHERE {_SCOPE} AND agent = {{agent:String}} AND {_WIN}
        """,
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "win": int(window_seconds),
            "inspectors": list(INSPECTOR_CONSUMERS),
        },
    )
    return row


async def indexed_writes(
    org: str,
    namespace: str,
    agent: str,
    window_seconds: int,
    *,
    model: str,
    ch: _CHClient | None = None,
) -> int:
    """Memories the agent first created in the window, by any write op, one per user,
    session and key, that have a search vector from `model` (the deployment's embedder; a
    row still waiting has `embedding_dim = 0`), in a served status and not past
    `expires_at`. An embedding row's `created_at` is its version's write, so when a memory
    was first created is read from `loom.memory`, which carries it forward across
    versions."""
    ch = ch or _default_client()
    (row,) = await ch.query_all(
        f"""
        SELECT uniqExact(user_id, session_id, memory_key) AS n
        FROM loom.memory_embeddings FINAL
        WHERE {_SCOPE} AND agent = {{agent:String}} AND tombstoned_at IS NULL
          AND (user_id, session_id, memory_key) IN (
              SELECT user_id, session_id, memory_key FROM loom.memory
              WHERE {_SCOPE} AND agent = {{agent:String}}
                AND created_at >= now() - INTERVAL {{win:UInt32}} SECOND
          )
          AND embedding_model = {{model:String}} AND embedding_dim > 0
          AND {served_status_sql()} AND (expires_at IS NULL OR expires_at > now())
        -- Pruning granules by the key set costs about 1 GiB for 300,000 keys; the scope
        -- prefix already bounds the scan.
        SETTINGS use_index_for_in_with_subqueries = 0
        """,
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "win": int(window_seconds),
            "model": model,
        },
    )
    return int(row["n"])


async def reported_keys(
    org: str, namespace: str, agent: str, window_seconds: int, *, ch: _CHClient | None = None
) -> int:
    """How many distinct memory keys the agent reported relying on in the window: its runs'
    `run.complete` `memories_used` (which keeps only keys a live row answers; `chunk:` ids,
    which name a passage rather than a memory, left out) and its `memory.outcome` labels by
    id that said `used` or `positive` and landed on a row, resolved to that row's key.

    A label by key is not counted: it resolves under the row writer's `agent`, so the ledger
    records it under the writer, whoever gave it, and a label claims the caller's reliance."""
    ch = ch or _default_client()
    (row,) = await ch.query_all(
        f"""
        SELECT uniqExact(k) AS n FROM (
            SELECT k FROM ({_CREDITED_KEYS}) WHERE NOT startsWith(k, 'chunk:')
            UNION ALL
            -- A memory's key never changes across its versions, so any version answers.
            SELECT memory_key AS k FROM loom.memory
            WHERE {_SCOPE} AND memory_id IN (
                SELECT toUUIDOrZero(args['memory_id']) FROM loom.agent_events
                WHERE {_SCOPE} AND agent = {{agent:String}} AND {_WIN} AND {_COUNTED_CALL}
                  AND success AND operation = 'memory.outcome'
                  AND args['out_recorded'] = 'true' AND args['outcome'] IN ('used', 'positive')
                  AND args['memory_id'] != ''
            )
        )
        """,
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "win": int(window_seconds),
            "inspectors": list(INSPECTOR_CONSUMERS),
        },
    )
    return int(row["n"])


async def graded_runs(
    org: str, namespace: str, agent: str, window_seconds: int, *, ch: _CHClient | None = None
) -> int:
    """How many of the agent's runs that started in the window carry a grade (`run.grade`
    pass or fail)."""
    ch = ch or _default_client()
    (row,) = await ch.query_all(
        f"""
        SELECT count() AS n FROM loom.agent_runs FINAL
        WHERE {_SCOPE} AND agent = {{agent:String}} AND grade != ''
          AND started_at >= now() - INTERVAL {{win:UInt32}} SECOND
        """,
        params={"org": org, "ns": namespace, "agent": agent, "win": int(window_seconds)},
    )
    return int(row["n"])

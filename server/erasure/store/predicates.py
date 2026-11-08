"""The scope predicates the erasure steps and the residue census share.

A step and the census count of the same table read one predicate here, so the
census can never count rows the step's scope did not reach. The user scope
reaches tables without a user_id column through the subject's memory keys,
tombstoned rows included: the memory step runs first and these must follow it,
not escape it.

A predicate that holds a subquery is for reads only: ClickHouse refuses a mutation
with a subquery on a replicated table. Its step reads the subquery
(`scope_reads`) and mutates by the bound form beside it.
"""

from __future__ import annotations

NS_EQ = "namespace = {ns:String}"
NS_IN = "namespace IN {ns:Array(String)}"


ORG_NS = "org = {org:String} AND namespace = {ns:String}"
# Every row a user wrote in the org, whatever the namespace (the knowledge plane).
ORG_UID = "org = {org:String} AND user_id = {uid:String}"


# A user's memory keys, namespace-qualified. The extractor mints generic keys
# — `user_employer`, `user_city` — so the same key names different users'
# facts in different namespaces; matching on `memory_key` alone would erase
# another tenant's link rows in every namespace that shares the key. Every
# link table carries the namespace, so the pair is the identity.
USER_KEYS_SUBQ = (
    "(SELECT namespace, memory_key FROM loom.memory "
    "WHERE org = {org:String} AND user_id = {uid:String})"
)
USER_PARENT_KEYS = f"(namespace, memory_key) IN {USER_KEYS_SUBQ}"
PARENT_KEYS_IN = "(namespace, memory_key) IN {keys:Array(Tuple(String, String))}"
# loom.memory_chunks names its parent `parent_memory_key`; same pair, same
# reason. A chunk carries its writer's `user_id`, which reaches the chunks no
# parent key names (a conversation's transcript chunks). Users of one namespace
# share extractor keys, so the pair reaches only the chunks stamped with no user
# ('-'): a legacy chunk of the user's memory can carry '-', and the write path
# retires those as the writer's own too (`set_chunks._key_chunks`). Another
# user's chunks of the key stay.
USER_CHUNKS = (
    "(user_id = {uid:String} OR (user_id = '-' AND "
    f"(namespace, parent_memory_key) IN {USER_KEYS_SUBQ}))"
)
CHUNK_KEYS_IN = (
    "(user_id = {uid:String} OR (user_id = '-' AND "
    "(namespace, parent_memory_key) IN {keys:Array(Tuple(String, String))}))"
)
USER_RECORDS_SUBQ = (
    "(SELECT record_id FROM loom.memory_records "
    "WHERE org = {org:String} AND user_id = {uid:String})"
)
USER_RECORD_IDS = f"record_id IN {USER_RECORDS_SUBQ}"
RECORD_IDS_IN = "record_id IN {ids:Array(UUID)}"
# memory_record_conflicts has no record_id: it pairs a prior and an incoming
# record. A conflict belongs to the user when either side
# does. The table has no `record_id` column to filter on.
USER_CONFLICT_RECORD_IDS = (
    f"(prior_record_id IN {USER_RECORDS_SUBQ} OR incoming_record_id IN {USER_RECORDS_SUBQ})"
)
CONFLICT_RECORD_IDS_IN = (
    "(prior_record_id IN {ids:Array(UUID)} OR incoming_record_id IN {ids:Array(UUID)})"
)


# The user scope reaches the derived tables that carry no user_id
# column through a parent-key predicate, as for the graph links. A cached
# answer or a precedent quotes the memories of its window; `window_keys`
# names them. The subject's keys are read from loom.memory with
# tombstoned rows included — memory.tombstone runs first and these steps
# must follow it, not escape it. The match is by key within the org, not by
# (namespace, key): an ask under `all_mine` renders memories from several
# namespaces into one row whose `namespace` is the request's, and deleting a
# cached answer or a precedent that shares a key spelling with the subject's
# costs one provider call, never a row of anyone's memory — the key-only match
# refused for link rows is the safe direction here.
# ---------------------------------------------------------------------------
USER_KEY_LIST = (
    "(SELECT groupUniqArray(memory_key) FROM loom.memory "
    "WHERE org = {org:String} AND user_id = {uid:String})"
)
# Summaries synthesised FROM an erased user's rows. A consolidated topic
# memory (or merged canonical row) lives under the identity that ran the
# consolidation — often `user_id='-'` — and carries its sources' content
# verbatim by design, tagged `src:<key>`. A user_id match never reaches it. A `src:`
# tag names a key of the summary's own namespace, as consolidation writes it (a copy
# into another namespace keeps the tags, and is not the user's there); the next dense
# pass rebuilds a retired summary from the surviving sources. The user's tags let the
# skip index prune; their (namespace, tag) pairs decide, on each version.
# `scope_reads.summary_slices` binds them; `USER_SUMMARIES` is the same match as a read.
CONSOLIDATED_IN = (
    "has(tags, 'consolidated') AND hasAny(tags, {src:Array(String)}) "
    "AND hasAny(arrayMap(t -> (namespace, t), tags), {pairs:Array(Tuple(String, String))})"
)
_USER_SRC_PAIRS = (
    "(SELECT groupUniqArray((toString(namespace), concat('src:', memory_key))) "
    "FROM loom.memory WHERE org = {org:String} AND user_id = {uid:String})"
)
USER_SUMMARIES = (
    "has(tags, 'consolidated') AND "
    f"hasAny(arrayMap(t -> (toString(namespace), t), tags), {_USER_SRC_PAIRS})"
)
# The summaries built from the user's memories, from the current rows and the history: a
# summary rebuilt without the user keeps under its key what its old text gave it, and
# only the history names it then, merged or not.
_SUMMARY_COLUMNS = "namespace, agent, user_id, memory_key, memory_id, tags"
SUMMARY_ROWS_FROM = (
    f"FROM (SELECT {_SUMMARY_COLUMNS} FROM loom.memory FINAL WHERE org = {{org:String}} "
    f"UNION ALL SELECT {_SUMMARY_COLUMNS} FROM loom.memory_versions "
    f"WHERE org = {{org:String}}) WHERE {USER_SUMMARIES}"
)
# A cached answer or a precedent that quoted such a summary quoted the user; the cache
# steps run while the history still names a rebuilt one.
USER_SUMMARY_KEY_LIST = f"(SELECT groupUniqArray(memory_key) {SUMMARY_ROWS_FROM})"
USER_QUOTED_KEY_LIST = f"arrayConcat({USER_KEY_LIST}, {USER_SUMMARY_KEY_LIST})"
# What was chunked or extracted from a summary is the user's too: chunks and entity
# vectors by the summary's agent, user and key (neither carries its session), row links
# by (namespace, id), graph key links by (namespace, key), as for the user's own keys.
# Each read form has its bound form beside it.
_USER_SUMMARY_KEYS_SUBQ = f"(SELECT toString(namespace), memory_key {SUMMARY_ROWS_FROM})"
_USER_SUMMARY_IDENTITIES = (
    f"(SELECT toString(namespace), agent, user_id, memory_key {SUMMARY_ROWS_FROM})"
)
_IDENTITIES = "{rows:Array(Tuple(String, String, String, String))}"
USER_SUMMARY_CHUNKS = (
    f"(namespace, agent, user_id, parent_memory_key) IN {_USER_SUMMARY_IDENTITIES}"
)
SUMMARY_CHUNKS_IN = f"(namespace, agent, user_id, parent_memory_key) IN {_IDENTITIES}"
USER_SUMMARY_VECTORS = f"(namespace, agent, user_id, memory_key) IN {_USER_SUMMARY_IDENTITIES}"
SUMMARY_VECTORS_IN = f"(namespace, agent, user_id, memory_key) IN {_IDENTITIES}"
USER_SUMMARY_IDS = (
    f"(namespace, memory_id) IN (SELECT toString(namespace), memory_id {SUMMARY_ROWS_FROM})"
)
SUMMARY_IDS_IN = "(namespace, memory_id) IN {ids:Array(Tuple(String, UUID))}"
USER_GRAPH_KEYS = (
    f"((namespace, memory_key) IN {USER_KEYS_SUBQ} "
    f"OR (namespace, memory_key) IN {_USER_SUMMARY_KEYS_SUBQ})"
)
USER_WINDOW_KEYS = f"hasAny(window_keys, {USER_QUOTED_KEY_LIST})"
USER_PRECEDENT_KEYS = f"hasAny(arrayConcat(window_keys, cited_keys), {USER_QUOTED_KEY_LIST})"
WINDOW_KEYS_IN = "hasAny(window_keys, {keys:Array(String)})"
PRECEDENT_KEYS_IN = "hasAny(arrayConcat(window_keys, cited_keys), {keys:Array(String)})"
# A cached code.ask answer names the memory it quoted (`memory_namespace`, `memory_keys`);
# MEMORY_NS_EQ contains NS_EQ, so a namespace list rewrites it to `IN` the same way.
MEMORY_NS_EQ = "memory_namespace = {ns:String}"
USER_MEMORY_KEYS = f"hasAny(memory_keys, {USER_QUOTED_KEY_LIST})"
MEMORY_KEYS_IN = "hasAny(memory_keys, {keys:Array(String)})"
# A community row names an entity (and a label — the highest-degree member of
# its community, another entity name). The entity belongs to the user when
# every memory linking it in the co-occurrence graph is the user's (or a summary
# of theirs) and at least one is — the exclusive-ownership rule, read from the edge link
# table, so this runs before graph_links.delete removes those rows. A row
# whose LABEL is such an entity goes too: the label text is the residue, and
# the communities worker relabels on its next pass from the edges that remain.
USER_COMMUNITY_ENTITIES = (
    "(SELECT namespace, entity FROM ("
    "SELECT namespace, entity_a AS entity, memory_key FROM loom.entity_edge_memories FINAL "
    "WHERE org = {org:String} "
    "UNION ALL "
    "SELECT namespace, entity_b AS entity, memory_key FROM loom.entity_edge_memories FINAL "
    "WHERE org = {org:String}) "
    "GROUP BY namespace, entity "
    f"HAVING countIf({USER_GRAPH_KEYS}) > 0 AND countIf(NOT {USER_GRAPH_KEYS}) = 0)"
)
USER_COMMUNITY_ROWS = (
    f"((namespace, entity) IN {USER_COMMUNITY_ENTITIES} "
    f"OR (namespace, label) IN {USER_COMMUNITY_ENTITIES})"
)
# The entities by `cityHash64(namespace, entity)`: a stored name can outgrow a form field.
COMMUNITY_ROWS_IN = (
    "(cityHash64(namespace, entity) IN {entities:Array(UInt64)} "
    "OR cityHash64(namespace, label) IN {entities:Array(UInt64)})"
)


# The live predicate of a soft-delete table, and the user-scope predicate of a
# table with a user_id column.
LIVE = "tombstoned_at IS NULL"
UID = "user_id = {uid:String}"
# A projection failure's writer: its payload's identity (a bulk write's, a replay's), or
# the identity of a retired row it owes the record of.
PAYLOAD_UID = (
    "(JSONExtractString(payload, 'user_id') = {uid:String}"
    " OR arrayExists(e -> JSONExtractString(e, 'user_id') = {uid:String},"
    " JSONExtractArrayRaw(payload, 'retired')))"
)

# Candidates any supporter row in the org names.
SUPPORTED_LEARNINGS = (
    "(SELECT candidate_id FROM loom.learnings_supporters WHERE org = {org:String})"
)
# Bot-rule candidates nobody supports that still hold their rule, counted org-wide.
ORPHAN_LEARNINGS = (
    "source_kind = 'assistant_turn' AND proposed_value != '' AND candidate_id NOT IN "
    + SUPPORTED_LEARNINGS
)

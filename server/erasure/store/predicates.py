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


# The user scope reaches the three derived tables that carry no user_id
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
USER_WINDOW_KEYS = f"hasAny(window_keys, {USER_KEY_LIST})"
USER_PRECEDENT_KEYS = f"hasAny(arrayConcat(window_keys, cited_keys), {USER_KEY_LIST})"
WINDOW_KEYS_IN = "hasAny(window_keys, {keys:Array(String)})"
PRECEDENT_KEYS_IN = "hasAny(arrayConcat(window_keys, cited_keys), {keys:Array(String)})"
# A cached answer belongs to the user when the user asked it
# (`user_id`, the request's) or when its pool quoted any of the user's keys.
# A community row names an entity (and a label — the highest-degree member of
# its community, another entity name). The entity belongs to the user when
# every memory linking it in the co-occurrence graph is the user's and at
# least one is — the exclusive-ownership rule, read from the edge link
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
    f"HAVING countIf((namespace, memory_key) IN {USER_KEYS_SUBQ}) > 0 "
    f"AND countIf((namespace, memory_key) NOT IN {USER_KEYS_SUBQ}) = 0)"
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

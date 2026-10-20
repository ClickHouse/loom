"""The scope predicates the erasure steps and the residue census share.

A step's mutation and the census count of the same table read one predicate
here, so the census can never count rows the step's scope did not reach. The
user scope reaches tables without a user_id column through the subject's
memory keys, tombstoned rows included: the memory step runs first and these
must follow it, not escape it.
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
# loom.memory_chunks names its parent `parent_memory_key`; same pair, same
# reason. A chunk also carries its writer's `user_id` ('-' when it has
# none), which reaches the chunks no parent key names — a
# conversation's transcript chunks. The saga step and the census both read
# this one predicate.
USER_CHUNKS = f"(user_id = {{uid:String}} OR (namespace, parent_memory_key) IN {USER_KEYS_SUBQ})"
USER_RECORD_IDS = (
    "record_id IN (SELECT record_id FROM loom.memory_records "
    "WHERE org = {org:String} AND user_id = {uid:String})"
)
# memory_record_conflicts has no record_id: it pairs a prior and an incoming
# record. A conflict belongs to the user when either side
# does. The table has no `record_id` column to filter on.
USER_CONFLICT_RECORD_IDS = (
    "(prior_record_id IN (SELECT record_id FROM loom.memory_records "
    "WHERE org = {org:String} AND user_id = {uid:String}) "
    "OR incoming_record_id IN (SELECT record_id FROM loom.memory_records "
    "WHERE org = {org:String} AND user_id = {uid:String}))"
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


# The live predicate of a soft-delete table, and the user-scope predicate of a
# table with a user_id column.
LIVE = "tombstoned_at IS NULL"
UID = "user_id = {uid:String}"

# A learnings queue candidate belongs to the end user whose conversation its
# rationale quotes: the one on its earliest supporter row. Every version of it is in
# scope, a review's status row included, whatever columns that row carries.
USER_FILED_LEARNING_IDS = (
    "(SELECT candidate_id FROM loom.learnings_supporters WHERE org = {org:String} "
    "GROUP BY candidate_id HAVING argMin(user_id, created_at) = {uid:String})"
)
USER_LEARNINGS = f"candidate_id IN {USER_FILED_LEARNING_IDS}"
# The user's own support rows, and every supporter of a candidate the user filed.
USER_LEARNING_SUPPORT = f"(user_id = {{uid:String}} OR {USER_LEARNINGS})"

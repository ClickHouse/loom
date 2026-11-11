"""Static guard for ClickHouse mutation callsites in server code."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_MUTATION_RE = re.compile(
    r"ALTER\s+TABLE\s+loom\.(?P<table>[A-Za-z_]+)\s+"
    r"(?P<operation>UPDATE|DELETE)\b",
    re.MULTILINE,
)


# Every statically visible ALTER UPDATE/DELETE in server/*.py must be
# listed here and explained in docs/architecture/clickhouse.md ("Deletes and
# mutations"). This count-based guard keeps
# the inventory honest without trying to parse Python string concatenation.
_EXPECTED_MUTATIONS = Counter(
    {
        # The contradiction repair's edge close, end_edges, which ends a dropped fact's
        # edge with its triples (only when no live row keeps the pair), and the undo of
        # a close's own ends (reopen_closed_by).
        ("server/memory/app/projections.py", "entity_edges", "UPDATE"): 3,
        # One folded UPDATE per table per write (close_triples serves
        # both the deterministic repair and the LLM judgement), and
        # reopen_triples, which undoes a follow's close for a triple a row
        # written meanwhile holds (rare: only in that race), and end_triples_now,
        # which ends now a dropped fact that ends later (only when one does).
        ("server/memory/app/projections.py", "entity_triples", "UPDATE"): 3,
        # In-place retries cannot recreate a transcript erased before a delayed command lands.
        ("server/memory/app/extraction_queue.py", "memory_extraction_queue", "UPDATE"): 1,
        ("server/memory/store/blacklist.py", "blacklist", "UPDATE"): 1,
        ("server/memory/store/conflicts.py", "conflict_allowlist", "UPDATE"): 1,
        ("server/knowledge/store/conversations.py", "conversation_threads", "UPDATE"): 1,
        # tombstone_thread and conversation.compact's tombstone_messages.
        ("server/knowledge/store/conversations.py", "conversations", "UPDATE"): 2,
        # tombstone_by_id writes a versioned row + embeddings cascade, not an
        # ALTER UPDATE.
        # Task inbox plane: the lease table is KeeperMap, so
        # these address a Keeper znode (a compare-and-swap under strict mode
        # and the terminal-state key drop), not a MergeTree part; the third UPDATE moves
        # an idle row at an older fence to a reopen's fence, and the fourth gives a
        # row back idle (a claim that backs out, the lease reconcile); the second DELETE
        # is the reconcile's, guarded on the holder and fence it read.
        ("server/tasks/store/repo.py", "task_leases", "UPDATE"): 4,
        ("server/tasks/store/repo.py", "task_leases", "DELETE"): 2,
        # run.complete drops a run id claim, also a KeeperMap key (the erasure saga's
        # drop goes through erasure.store.mutations.run_keeper_delete).
        ("server/tasks/store/runs.py", "agent_run_claims", "DELETE"): 1,
        # KeeperMap keys only (elsewhere an end is a row): a projection's end drops its
        # slot key, a begin clears a slot a dead process left, and a graph write raises
        # its namespace's write key (a version-checked UPDATE).
        ("server/memory/store/graph_projections.py", "graph_projections", "DELETE"): 2,
        ("server/memory/store/graph_projections.py", "graph_projections", "UPDATE"): 1,
        # The leader's hourly sweep of the slot keys of projections that died.
        ("server/core/leases.py", "graph_projections", "DELETE"): 1,
        # The leader's sweep, every ten minutes, of the heartbeat keys of processes gone an hour:
        # KeeperMap keys, not a MergeTree part.
        ("server/core/instances.py", "instances", "DELETE"): 1,
        ("server/core/instances.py", "cell_heartbeats", "DELETE"): 1,
        # Sign-in state, a KeeperMap key per pending login: a compare-and-swap
        # on the key's value under strict mode, its drop when the login ends,
        # and the sweep of expired keys.
        ("server/auth/claims.py", "oauth_signin_state", "UPDATE"): 1,
        ("server/auth/claims.py", "oauth_signin_state", "DELETE"): 2,
        # Conditional Keeper key recovery, only after a kernel death proof and
        # a server-side quiescence check; not a MergeTree data mutation.
        ("server/insights/store/provider_lock.py", "_migration_lock", "DELETE"): 1,
        # memory.redact scrubs the named historical version and the profile
        # versions built from it in one statement per table, on loom.memory and
        # on the append-only mirror (ALTER UPDATE does not flow through
        # memory_versions_mv).
        ("server/memory/store/redaction.py", "memory", "UPDATE"): 1,
        ("server/memory/store/redaction.py", "memory_versions", "UPDATE"): 1,
        # a deleted code link's suggestion, a rewrite of the redacted text
        ("server/memory/store/redaction.py", "memory_code_links", "UPDATE"): 1,
        # Expiry sweep `demote`: clears expires_at and lowers
        # confidence on the key's embedding rows in place — the same mirror
        # memory.touch performs; the memory-table side is a version row.
        # memory.apply label / demote: tags or confidence
        # mirrored onto the key's embedding rows; the memory-table side is a
        # version row. One helper, one UPDATE statement, ALTER fallback inline.
        # The repair's operator queue, narrow by failure id: one UPDATE resolves, one
        # abandons a failure no retry can repair, one carries a failed replay's closes.
        ("server/memory/app/projection_failures.py", "memory_projection_failures", "UPDATE"): 3,
    }
)


def test_clickhouse_mutation_inventory_is_classified() -> None:
    """New server-side CH mutations must be consciously classified."""

    root = Path("server")
    observed: Counter[tuple[str, str, str]] = Counter()
    for path in root.rglob("*.py"):
        rel = path.as_posix()
        text = path.read_text()
        for match in _MUTATION_RE.finditer(text):
            observed[(rel, match.group("table"), match.group("operation"))] += 1

    assert observed == _EXPECTED_MUTATIONS

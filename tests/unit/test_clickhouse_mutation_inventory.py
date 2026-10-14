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
        ("server/memory/app/projections.py", "entity_edges", "UPDATE"): 1,
        # One folded UPDATE per table per write (close_triples serves
        # both the deterministic repair and the LLM judgement).
        ("server/memory/app/projections.py", "entity_triples", "UPDATE"): 1,
        ("server/memory/store/blacklist.py", "blacklist", "UPDATE"): 1,
        ("server/memory/store/conflicts.py", "conflict_allowlist", "UPDATE"): 1,
        ("server/knowledge/store/conversations.py", "conversation_embeddings", "UPDATE"): 1,
        ("server/knowledge/store/conversations.py", "conversation_threads", "UPDATE"): 1,
        # tombstone_thread and conversation.compact's tombstone_messages.
        ("server/knowledge/store/conversations.py", "conversations", "UPDATE"): 2,
        # tombstone_by_id writes a versioned row + embeddings cascade, not an
        # ALTER UPDATE.
        # Task inbox plane: the lease table is KeeperMap, so
        # these address a Keeper znode (a compare-and-swap under strict mode
        # and the terminal-state key drop), not a MergeTree part.
        ("server/tasks/store/repo.py", "task_leases", "UPDATE"): 2,
        ("server/tasks/store/repo.py", "task_leases", "DELETE"): 1,
        # run.complete drops a run id claim, and a session run's close drops the
        # close claim it holds or a stale one it takes over: all KeeperMap keys (the
        # erasure saga's drop goes through erasure.store.mutations.run_keeper_delete).
        ("server/tasks/store/runs.py", "agent_run_claims", "DELETE"): 2,
        # Conditional Keeper key recovery, only after a kernel death proof and
        # a server-side quiescence check; not a MergeTree data mutation.
        ("server/insights/store/provider_lock.py", "_migration_lock", "DELETE"): 1,
        # memory.redact scrubs one historical version on loom.memory and on the
        # append-only mirror (ALTER UPDATE does not flow through
        # memory_versions_mv).
        ("server/memory/store/redaction.py", "memory", "UPDATE"): 1,
        ("server/memory/store/redaction.py", "memory_versions", "UPDATE"): 1,
        # Expiry sweep `demote`: clears expires_at and lowers
        # confidence on the key's embedding rows in place — the same mirror
        # memory.touch performs; the memory-table side is a version row.
        # memory.apply label / demote: tags or confidence
        # mirrored onto the key's embedding rows; the memory-table side is a
        # version row. One helper, one UPDATE statement, ALTER fallback inline.
        ("server/memory/app/projection_failures.py", "memory_projection_failures", "UPDATE"): 1,
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

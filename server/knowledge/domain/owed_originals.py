"""The originals a knowledge document commit still owes ClickHouse names, and those an
open ingestion job reads, as Postgres fragments for the originals sweep and a user
erasure's shared-originals check.

`loom.knowledge_deliveries` holds each unpublished commit's row as JSON. A row needs
what it names while it is a live document whose original is not erased (a JSON null or
an absent key reads as NULL); a pending delete's tombstone needs none. Each row names up
to two: its original and its extraction artifact, the empty ones left out, so a live
row with no original still keeps its artifact. `LIVE_ORIGINAL_SQL`, which the sweep
applies to published rows, also requires an original (`blob_path != ''`) before it
counts the artifact; this fragment does not copy that condition.
"""

from __future__ import annotations

# FROM loom.knowledge_deliveries, <this> AS sha: one row per sha a delivery names.
OWED_SHAS = (
    "unnest(array_remove(array_remove("
    "ARRAY[row->>'blob_path', row->>'artifact_sha256'], ''), NULL))"
)
# WHERE ... AND <this>: the deliveries whose row still needs what it names.
NEEDS_ORIGINAL = "NOT settled AND row->>'superseded_at' IS NULL AND row->>'blob_erased_at' IS NULL"
# FROM loom.knowledge_jobs WHERE ... AND <this>: the open ingestion jobs, each of which
# reads its original (`row->>'sha256'`).
OPEN_JOB = "state IN ('pending', 'processing')"

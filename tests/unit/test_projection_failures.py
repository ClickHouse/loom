import pytest

from server.memory.app.projection_failures import (
    ProjectionFailureRow,
    _session_from_source_resource_id,
    retry_projection_failure,
)


def test_session_from_source_resource_id_parses_message_ingest_id() -> None:
    assert _session_from_source_resource_id("memory.set_from_messages:s-1:r-1") == "s-1"
    assert _session_from_source_resource_id("other") == "-"


async def test_retry_projection_failure_reports_unsupported_projection() -> None:
    result = await retry_projection_failure(
        ProjectionFailureRow(
            failure_id="f1",
            org="o",
            namespace="n",
            agent="a",
            projection="memory_evidence",
            source_operation="memory.set_from_messages",
            source_resource_id="memory.set_from_messages:s:r",
            memory_keys=[],
            error_type="RuntimeError",
            error_message="boom",
        ),
        ch=object(),
    )

    assert result.retried is False
    assert result.resolved is False
    assert result.reason == "unsupported_projection"


class _GraphCH:
    """Records every insert's table and settings; the repair reads find nothing."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, dict]] = []
        self.executed: list[str] = []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, dict(settings or {})))

    async def query_all(self, sql, params=None):
        return []

    async def query_one(self, sql, params=None):
        return None

    async def execute(self, sql, params=None):
        self.executed.append(sql)


def _graph_failure(projection: str) -> ProjectionFailureRow:
    entry = {
        "memory_key": "k1",
        "memory_type": "semantic",
        "value": "alice works with bob at acme",
        "entities": ["alice", "bob", "acme"],
        "relations": [["alice", "works_with", "bob"]],
    }
    return ProjectionFailureRow(
        failure_id="11111111-1111-1111-1111-111111111111",
        org="o",
        namespace="n",
        agent="a",
        projection=projection,
        source_operation="memory.bulk_set",
        source_resource_id="",
        memory_keys=["k1"],
        error_type="RuntimeError",
        error_message="boom",
        payload={"entries": [entry], "results": [{"memory_key": "k1", "version": 1}]},
    )


@pytest.mark.parametrize(
    ("projection", "tables"),
    [
        ("entity_edges", {"loom.entity_edges", "loom.entity_edge_memories"}),
        ("entity_triples", {"loom.entity_triples", "loom.entity_triple_memories"}),
    ],
)
async def test_a_graph_replay_inserts_under_the_failures_dedup_token(projection, tables) -> None:
    """The plain-MergeTree graph tables count every row as an observation.
    The resolve is an ALTER UPDATE that may apply after the next pass, so a
    replay can run twice; the second insert carries the same token per table
    and ClickHouse drops it instead of counting the co-occurrence again."""
    ch = _GraphCH()
    first = await retry_projection_failure(_graph_failure(projection), ch=ch)
    second = await retry_projection_failure(_graph_failure(projection), ch=ch)
    assert first.resolved and second.resolved, (first, second)
    seen = [(t, s) for t, s in ch.inserts if t in tables]
    assert {t for t, _ in seen} == tables and len(seen) == 4
    for table, s in seen:
        short = table.removeprefix("loom.")
        assert s == {
            "async_insert": 0,
            "insert_deduplicate": 1,
            "insert_deduplication_token": (
                f"projection-repair:11111111-1111-1111-1111-111111111111:{short}"
            ),
        }


def test_the_write_path_keeps_its_async_inserts() -> None:
    from server.memory.app.projections import graph_insert_settings

    assert graph_insert_settings("entity_edges", wait=False, dedup_token="") == {
        "async_insert": 1,
        "wait_for_async_insert": 0,
    }

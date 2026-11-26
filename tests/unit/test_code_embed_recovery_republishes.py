"""`code_embed_recovery` fills vectors outside the ingest pass, which is what
publishes the shared counts and coverage rows a status read serves. When its scan
comes back empty, it republishes both rows for every configured namespace it
embedded since the last time; the rows are read back through `CodeRepo`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.code.store.index import CodeRepo
from server.core.ch import sequential_reads
from server.workers import pass_context

ORG, NS = "acme", "code-a"


class _Table:
    """`loom.code_index_state` and the chunk table of one namespace, answering the
    statements the counts and coverage refresh run."""

    def __init__(self, chunks: int, embedded: int) -> None:
        self.chunks: list[dict[str, Any]] = [
            {
                "org": ORG,
                "namespace": NS,
                "path": f"src/{i}.py",
                "chunk_id": f"c{i}",
                "content": "def f(): pass",
                "embedding_dim": 2 if i < embedded else 0,
                "embed_attempts": 0,
                "version": i + 1,
            }
            for i in range(chunks)
        ]
        self.state: dict[str, dict[str, Any]] = {}
        self.state_writes = 0
        # "aggregate" (the counts' chunk scan), or a state key whose write fails.
        self.failing = ""

    def live(self) -> dict[str, int]:
        n = len(self.chunks)
        embedded = sum(1 for c in self.chunks if c["embedding_dim"] > 0)
        return {"n": n, "embedded": embedded, "embeddable": n}

    def _read(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM loom.code_index_state" in sql:
            if "{key:String}" in sql or "'table_counts'" in sql:
                key = params.get("key", "table_counts")
                return [dict(self.state[key])] if key in self.state else []
            keys = params.get("keys") or sorted(self.state)
            return [{"state_key": k, **self.state[k]} for k in sorted(keys) if k in self.state]
        if "AS described" in sql:
            live = self.live()
            row = {"lang": "python", "chunks": live["n"], "described": 0, "last_at": None}
            return [{**row, "embedded": live["embedded"]}]
        if "AS embeddable" in sql:
            if self.failing == "aggregate":
                raise TimeoutError("aggregate timed out")
            return [self.live()]
        if "count() AS n FROM loom." in sql:
            return [{"n": 1}]
        return []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return self._read(sql, dict(params or {}))

    async def query_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        rows = self._read(sql, dict(params or {}))
        return rows[0] if rows else None

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.code_index_state", table
        for r in rows:
            if r["state_key"] == self.failing:
                raise TimeoutError("insert timed out")
            self.state_writes += 1
            self.state[r["state_key"]] = {
                "state_value": r["state_value"],
                "detail": r["detail"],
                "updated_at": r["updated_at"],
            }


class _Namespace(CodeRepo):
    """The real state, counts and coverage code over `_Table`; the recovery scans
    and the replacement insert are the table's chunk rows."""

    def __init__(self, table: _Table) -> None:
        super().__init__(ch=table)  # type: ignore[arg-type]
        self.table = table

    async def pending_embed_keys(
        self,
        table: str,
        repos: Any,
        model: str,
        max_attempts: int,
        limit: int,
        *,
        min_version: int = 0,
    ) -> list[dict[str, Any]]:
        if table != "code_chunks":
            return []
        stubs = [
            c for c in self.table.chunks if not c["embedding_dim"] and c["version"] > min_version
        ]
        return [dict(c) for c in stubs[:limit]]

    async def live_stub_keys(self, table: str, model: str, keys: Any, max_attempts: int) -> Any:
        return keys

    async def pending_embed_rows_by_keys(
        self, table: str, model: str, keys: Any, max_attempts: int
    ) -> Any:
        return keys

    @staticmethod
    def tombstone_of(table: str, live_row: dict[str, Any]) -> dict[str, Any]:
        return dict(live_row)

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        by_id = {r["chunk_id"]: r for r in rows}
        self.table.chunks = [by_id.get(c["chunk_id"], c) for c in self.table.chunks]


class _Embedder:
    model_name = "m"
    dim = 2

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.5, 0.5] for _ in texts]


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The configured repositories: the one namespace, until a test removes it."""
    from server.code.domain.index import repos as repos_mod
    from server.code.workers import embed_recovery as w
    from server.embedding import embedder as emb_mod
    from server.settings import settings

    class _Cfg:
        org, namespace = ORG, NS

    out: list[Any] = [_Cfg()]
    monkeypatch.setattr(repos_mod, "configured_repos", lambda: list(out))
    monkeypatch.setattr(emb_mod, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(settings, "code_embed_recovery_full_scan_seconds", 1800)
    monkeypatch.setattr(w, "_watermark", {})
    monkeypatch.setattr(w, "_full_scan_at", 0.0)
    return out


async def _namespace(monkeypatch: pytest.MonkeyPatch, table: _Table) -> _Namespace:
    """The namespace after `--phase index`: stubs, and the index phase's counts and
    coverage published."""
    from server.code.store import index as cr

    repo = _Namespace(table)
    await repo.refresh_coverage(ORG, NS)
    await repo.refresh_table_counts(ORG, NS)
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    return repo


async def _pass() -> Any:
    from server.code.workers import embed_recovery as w

    pass_context.begin("code_embed_recovery")
    try:
        return await w.run_pass()
    finally:
        pass_context.clear()


async def _published(repo: _Namespace) -> tuple[dict[str, int], int]:
    with sequential_reads():
        state = await repo.all_state(ORG, NS)
    counts: dict[str, int] = json.loads(state["table_counts"]["value"])
    cov = json.loads(state["coverage"]["value"])["cov"]
    return counts, sum(int(c["embedded"]) for c in cov["chunks"])


async def test_a_drained_namespace_publishes_the_counts_and_coverage_of_its_table(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _Table(chunks=5, embedded=2)
    repo = await _namespace(monkeypatch, table)

    busy = await _pass()
    assert busy.chunks_embedded == 3 and not busy.idle
    drained = await _pass()
    assert drained.idle and drained.errors == []

    counts, coverage_embedded = await _published(repo)
    assert table.live() == {"n": 5, "embedded": 5, "embeddable": 5}
    assert counts["code_chunks"] == 5
    assert counts["code_chunks_embedded"] == counts["code_chunks_embeddable"] == 5
    assert coverage_embedded == 5

    # An idle pass with nothing embedded since the last drain writes nothing.
    writes = table.state_writes
    await _pass()
    assert table.state_writes == writes


@pytest.mark.parametrize("failing", ["coverage", "table_counts", "aggregate"])
async def test_a_failed_republish_is_retried_on_the_next_idle_pass(
    failing: str, configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _Table(chunks=3, embedded=0)
    repo = await _namespace(monkeypatch, table)
    await _pass()

    table.failing = failing
    failed = await _pass()
    assert failed.idle and len(failed.errors) == 1 and "TimeoutError" in failed.errors[0]
    counts, _ = await _published(repo)
    assert counts["code_chunks_embedded"] == 0

    table.failing = ""
    retried = await _pass()
    assert retried.errors == []
    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 3


async def test_a_namespace_removed_before_the_drain_is_not_republished(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _Table(chunks=3, embedded=0)
    await _namespace(monkeypatch, table)
    await _pass()
    writes = table.state_writes

    configured.clear()  # `code.repo.remove`: the namespace's rows are purged next
    await _pass()
    assert table.state_writes == writes

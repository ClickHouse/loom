"""`code_embed_recovery` fills vectors outside the ingest pass, which is what
publishes the shared counts and coverage rows a status read serves. Once a
namespace it embedded has no stubs left, it republishes both rows for it, whether
or not other namespaces still have work; the rows are read back through `CodeRepo`."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from server.code.store.index import CodeRepo
from server.core.ch import sequential_reads, sequential_reads_active
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
    and the replacement insert are the table's chunk rows, and `summaries` the
    summary stubs of any namespace."""

    def __init__(self, table: _Table) -> None:
        super().__init__(ch=table)  # type: ignore[arg-type]
        self.table = table
        # Stub versions ReplacingMergeTree has not merged away: a scan returns them,
        # and `live_stub_keys` drops them, as the newer embedded version wins.
        self.superseded: list[dict[str, Any]] = []
        self.summaries: list[dict[str, Any]] = []

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
        if table == "code_summaries":
            stubs = list(self.summaries)
        else:
            stubs = [c for c in self.table.chunks if not c["embedding_dim"]] + self.superseded
        page = sorted((c for c in stubs if c["version"] > min_version), key=lambda c: c["version"])
        return [dict(c) for c in page[:limit]]

    async def pending_embed_chunks(
        self,
        org: str,
        ns: str,
        model: str,
        max_attempts: int,
        limit: int,
        *,
        include_poisoned: bool = False,
    ) -> list[dict[str, Any]]:
        stubs = [c for c in self.table.chunks if c["namespace"] == ns and not c["embedding_dim"]]
        return stubs[:limit]

    async def live_stub_keys(self, table: str, model: str, keys: Any, max_attempts: int) -> Any:
        return [k for k in keys if k not in self.superseded]

    async def pending_embed_rows_by_keys(
        self, table: str, model: str, keys: Any, max_attempts: int
    ) -> Any:
        return keys

    @staticmethod
    def tombstone_of(table: str, live_row: dict[str, Any]) -> dict[str, Any]:
        return dict(live_row)

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        if table == "code_summaries":
            done = {r["scope_path"] for r in rows}
            self.summaries = [s for s in self.summaries if s["scope_path"] not in done]
            return
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


async def test_a_drained_namespace_is_republished_while_another_has_summary_work(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.workers import embed_recovery as w

    class _Other:
        org, namespace = ORG, "code-b"

    configured.append(_Other())
    table = _Table(chunks=3, embedded=0)
    repo = await _namespace(monkeypatch, table)
    monkeypatch.setattr(w, "_full_scan_at", time.monotonic())  # no full scan in this test
    republished: list[tuple[str, str]] = []
    refresh = repo.refresh_coverage

    async def spy(org: str, ns: str, **k: Any) -> Any:
        republished.append((org, ns))
        return await refresh(org, ns, **k)

    monkeypatch.setattr(repo, "refresh_coverage", spy)

    landed = await _pass()  # the three vectors land for code-a
    assert landed.chunks_embedded == 3 and not landed.idle
    for i in range(3):  # then only code-b's summaries have work
        repo.summaries.append(
            {
                "org": ORG,
                "namespace": "code-b",
                "scope_type": "file",
                "scope_path": f"b/{i}.py",
                "summary": "what b does",
                "embed_attempts": 0,
                "version": 100 + i,
            }
        )
        busy = await _pass()
        assert busy.summaries_embedded == 1 and busy.chunks_embedded == 0
        assert not busy.idle and busy.errors == []

    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 3
    assert republished == [(ORG, NS)]


async def test_an_idle_pass_does_not_publish_over_a_stub_below_the_watermark(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.workers import embed_recovery as w
    from server.settings import settings

    monkeypatch.setattr(settings, "code_embed_recovery_batch", 1)
    table = _Table(chunks=2, embedded=0)
    repo = await _namespace(monkeypatch, table)
    monkeypatch.setattr(w, "_full_scan_at", time.monotonic())  # no full scan until asked
    insert = repo.insert_rows
    inserts = 0

    async def second_times_out(t: str, rows: list[dict[str, Any]]) -> None:
        nonlocal inserts
        inserts += 1
        if inserts == 2:
            raise TimeoutError("insert timed out")
        await insert(t, rows)

    monkeypatch.setattr(repo, "insert_rows", second_times_out)

    assert (await _pass()).chunks_embedded == 1
    with pytest.raises(TimeoutError):
        await _pass()  # the watermark is already past the second stub
    idle = await _pass()
    assert idle.idle and idle.errors == []
    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 0

    monkeypatch.setattr(w, "_full_scan_at", 0.0)  # the full scan finds the second stub
    assert (await _pass()).chunks_embedded == 1
    assert (await _pass()).idle
    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 2


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


async def test_a_restart_before_the_drain_still_republishes_on_the_full_scan_pass(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.workers import embed_recovery as w

    table = _Table(chunks=3, embedded=0)
    repo = await _namespace(monkeypatch, table)
    await _pass()  # the last vectors land

    # A restart: the process memory is gone, so the next pass is the boot's full scan.
    monkeypatch.setattr(w, "_unpublished", set())
    monkeypatch.setattr(w, "_watermark", {})
    monkeypatch.setattr(w, "_full_scan_at", 0.0)
    drained = await _pass()
    assert drained.idle and drained.full_scan and drained.errors == []
    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 3

    # Once the rows match, the next full-scan pass reads them and writes nothing.
    writes = table.state_writes
    monkeypatch.setattr(w, "_full_scan_at", 0.0)
    await _pass()
    assert table.state_writes == writes


def _restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process memory gone: the next pass is the boot's full scan."""
    from server.code.workers import embed_recovery as w

    monkeypatch.setattr(w, "_unpublished", set())
    monkeypatch.setattr(w, "_watermark", {})
    monkeypatch.setattr(w, "_full_scan_at", 0.0)


async def test_current_counts_over_a_coverage_row_behind_are_republished_after_a_restart(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A handoff: the previous build published coverage, its last vectors landed,
    # then it published counts. The counts match the chunks; the coverage does not.
    table = _Table(chunks=3, embedded=0)
    repo = await _namespace(monkeypatch, table)
    for c in table.chunks:
        c["embedding_dim"] = 2
    await repo.refresh_table_counts(ORG, NS)
    _restart(monkeypatch)

    drained = await _pass()
    assert drained.idle and drained.full_scan and drained.errors == []
    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 3


async def test_a_coverage_row_alone_behind_the_chunks_is_republished_after_a_restart(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The index phase published coverage and its counts write failed; the last
    # vectors landed afterwards. Only the coverage row exists, and it trails.
    table = _Table(chunks=3, embedded=0)
    repo = await _namespace(monkeypatch, table)
    del table.state["table_counts"]
    for c in table.chunks:
        c["embedding_dim"] = 2
    _restart(monkeypatch)

    drained = await _pass()
    assert drained.idle and drained.full_scan and drained.errors == []
    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 3


async def test_the_comparison_and_the_republish_read_with_sequential_consistency(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lagging replica must not make a current row read as behind, nor let a
    # republish overwrite a current row with older figures.
    table = _Table(chunks=3, embedded=0)
    repo = await _namespace(monkeypatch, table)
    await _pass()  # the last vectors land
    _restart(monkeypatch)
    seen: dict[str, bool] = {}

    def spy(name: str) -> Any:
        real = getattr(repo, name)

        async def call(*a: Any, **k: Any) -> Any:
            seen[name] = sequential_reads_active()
            return await real(*a, **k)

        return call

    for name in ("published_chunks_behind", "refresh_coverage", "refresh_table_counts"):
        monkeypatch.setattr(repo, name, spy(name))

    drained = await _pass()
    assert drained.idle and drained.errors == []
    assert seen == {
        "published_chunks_behind": True,
        "refresh_coverage": True,
        "refresh_table_counts": True,
    }


async def test_a_full_scan_that_meets_unmerged_stub_versions_still_notes_the_namespace(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _Table(chunks=3, embedded=0)
    repo = await _namespace(monkeypatch, table)
    await _pass()  # the last vectors land
    # The stub versions those vectors replaced are not merged yet.
    repo.superseded = [{**table.chunks[0], "embedding_dim": 0, "version": 1}]
    _restart(monkeypatch)

    restarted = await _pass()
    assert restarted.full_scan and not restarted.idle and restarted.chunks_embedded == 0
    drained = await _pass()  # above the watermark: idle, and not a full scan
    assert drained.idle and not drained.full_scan and drained.errors == []
    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 3


async def test_a_failed_comparison_does_not_fail_the_pass(
    configured: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.workers import embed_recovery as w

    table = _Table(chunks=3, embedded=0)
    repo = await _namespace(monkeypatch, table)
    await _pass()
    _restart(monkeypatch)

    table.failing = "aggregate"
    failed = await _pass()
    assert failed.full_scan and len(failed.errors) == 1 and "TimeoutError" in failed.errors[0]

    table.failing = ""
    monkeypatch.setattr(w, "_full_scan_at", 0.0)
    assert (await _pass()).errors == []
    counts, coverage_embedded = await _published(repo)
    assert counts["code_chunks_embedded"] == coverage_embedded == 3

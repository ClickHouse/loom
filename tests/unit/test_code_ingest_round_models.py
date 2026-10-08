"""A round's model calls are made together before its files
are written, and its rows are written one INSERT per table — with the rows a
file-by-file ingest writes.

The model answers are keyed by the exact call (the description's system/user/
max_tokens, the embedding's clipped text), so a file's own call is served
from them or made as before; a failed prefetch call is not recorded. The
write buffer flushes before any read that could see a buffered row."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import ingest, ingest_resolve
from server.code.app.index.round_models import prefetch_models
from server.code.app.index.round_reads import RoundReads
from server.settings import settings
from tests.unit.test_code_ingest_pass_budget import _A, _B, _ReadRepo, _rows


class _Repo(_ReadRepo):
    async def cards_for_paths(self, org: str, ns: str, model: str, paths: list[str]) -> dict:
        self.reads.append(f"cards:{','.join(sorted(paths))}")
        return {p: f"card of {p}" for p in paths}


class _LLM:
    provider, model = "fake", "fake-1"

    def __init__(self, fail: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.calls: list[str] = []
        self.fail = set(fail)
        self.in_flight = self.peak = 0

    async def complete(self, system: str, user: str, max_tokens: int = 1024, **_: Any) -> Any:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        scope = user.split("Scope: ", 1)[1].split("\n", 1)[0]
        self.calls.append(scope)
        if scope in self.fail:
            raise RuntimeError("429")

        class _R:
            text = f"describes {scope}"

        return _R()


class _Emb:
    model_name, dim = "m", 3

    def __init__(self) -> None:
        self.batches: list[int] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [[float(len(t)), 1.0, 0.0] for t in texts]

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]


_FILES = (("src/a.cpp", _A), ("src/b.cpp", _B))


@pytest.fixture(autouse=True)
def _describe_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_context_description_prompt", "questions")


async def _per_file(repo: Any, llm: Any, emb: Any) -> None:
    for path, src in _FILES:
        await ingest.ingest_file(
            repo,
            "o",
            "code-ns",
            path=path,
            source=src,
            commit_sha="c" * 40,
            embedding_model="m",
            embedder=emb,
            llm=llm,
            describe=True,
        )


async def _round(repo: Any, llm: Any, emb: Any, *, embed: bool = True) -> RoundReads:
    reads = RoundReads(repo, "o", "code-ns")
    sources = dict(_FILES)
    await reads.prefetch(sources)
    await prefetch_models(
        reads,
        "o",
        "code-ns",
        sources,
        llm=llm,
        embedder=emb,
        embedding_model="m",
        max_chunk_chars=6000,
        embed=embed,
    )
    for path, src in _FILES:
        await ingest.ingest_file(
            reads,
            "o",
            "code-ns",
            path=path,
            source=src,
            commit_sha="c" * 40,
            embedding_model="m",
            embedder=emb,
            llm=llm,
            describe=True,
            parsed=reads.parsed.get(path),
        )
    await reads.flush()
    return reads


def test_the_round_writes_the_rows_a_file_by_file_ingest_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain, batched = _Repo(), _Repo()
    llm_a, llm_b, emb_a, emb_b = _LLM(), _LLM(), _Emb(), _Emb()
    asyncio.run(_per_file(plain, llm_a, emb_a))
    reads = asyncio.run(_round(batched, llm_b, emb_b))
    assert _rows(batched) == _rows(plain)
    assert sorted(llm_b.calls) == sorted(llm_a.calls), "one call per description, made once"
    assert len(emb_b.batches) == 1 and emb_a.batches == [2, 2], "one embedding batch for the round"
    # b.cpp calls f and h, which a.cpp (earlier in the round) defines: its name
    # read must see a.cpp's symbols, so the buffer is written before it — one
    # flush there, one at the round's end, never one INSERT per file per table.
    assert reads.flushes == 2
    tables = [t for t, _ in batched.inserts]
    assert all(tables.count(t) <= reads.flushes for t in set(tables))
    assert len(plain.inserts) > len(batched.inserts)


def test_descriptions_run_concurrently_under_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_ingest_round_model_concurrency", 3)
    llm = _LLM()
    asyncio.run(_round(_Repo(), llm, _Emb()))
    assert llm.peak == 3 and len(llm.calls) == 4


def test_a_failed_prefetch_call_is_retried_by_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 in the prefetch records nothing: the file calls again, as the
    per-file path always did, and a second failure leaves no description."""
    llm = _LLM(fail={"g"})
    repo = _Repo()
    asyncio.run(_round(repo, llm, _Emb()))
    assert llm.calls.count("g") == 2 and llm.calls.count("f") == 1
    chunk_rows = [r for t, rows in repo.inserts if t == "code_chunks" for r in rows]
    by_scope = {r["scope"]: r for r in chunk_rows}
    assert by_scope["g"]["context_source"] == "structural"
    assert by_scope["f"]["context_description"] == "describes f"


def test_a_text_the_round_did_not_embed_is_embedded_by_the_file() -> None:
    emb = _Emb()
    asyncio.run(_round(_Repo(), _LLM(), emb, embed=False))
    assert emb.batches == [2, 2], "no round embedding: each file embeds its own"


def test_the_card_is_read_once_for_the_round() -> None:
    repo = _Repo()
    asyncio.run(_round(repo, _LLM(), _Emb()))
    assert [r for r in repo.reads if r.startswith("cards:")] == ["cards:src/a.cpp,src/b.cpp"]
    assert [r for r in repo.reads if r == "code_chunks"] == ["code_chunks", "code_chunks"], (
        "each path's chunk index read once, by the prefetch; the file is served from it"
    )


# ---- the write buffer's flush rule ----------------------------------------------


class _Probe(_Repo):
    async def live_text_for_path(self, org: str, ns: str, path: str) -> str:
        self.reads.append(f"text:{path}")
        return ""

    async def symbols_by_name(self, org: str, ns: str, name: str) -> list:
        self.reads.append(f"byname:{name}")
        return []


def _buffered(monkeypatch: pytest.MonkeyPatch) -> tuple[_Probe, RoundReads]:
    repo = _Probe()
    return repo, RoundReads(repo, "o", "code-ns")


def test_a_read_of_a_buffered_path_flushes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, reads = _buffered(monkeypatch)

    async def run() -> None:
        await reads.insert_rows("code_chunks", [{"path": "src/a.h", "chunk_id": "x"}])
        await reads.live_text_for_path("o", "code-ns", "src/other.cpp")
        assert repo.inserts == [], "another path cannot see the buffer"
        await reads.live_text_for_path("o", "other-ns", "src/a.h")
        assert repo.inserts == [], "another namespace cannot see the buffer"
        await reads.live_text_for_path("o", "code-ns", "src/a.h")
        assert [t for t, _ in repo.inserts] == ["code_chunks"], "the header's own text: flushed"

    asyncio.run(run())


def test_a_read_that_is_not_path_scoped_flushes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, reads = _buffered(monkeypatch)

    async def run() -> None:
        await reads.insert_rows("code_symbols", [{"path": "src/a.h", "name": "f"}])
        await reads.symbols_by_name("o", "code-ns", "f")
        assert [t for t, _ in repo.inserts] == ["code_symbols"]

    asyncio.run(run())


def test_the_row_cap_writes_early(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, reads = _buffered(monkeypatch)
    monkeypatch.setattr(settings, "code_ingest_round_write_rows", 3)

    async def run() -> None:
        await reads.insert_rows("code_edges", [{"path": "p"}] * 2)
        assert repo.inserts == []
        await reads.insert_rows("code_edges", [{"path": "p"}] * 2)
        assert [len(r) for _, r in repo.inserts] == [4]

    asyncio.run(run())


def test_concurrent_reads_of_one_key_share_one_statement() -> None:
    calls: list[str] = []

    async def read() -> str:
        calls.append("x")
        await asyncio.sleep(0.01)
        return "text"

    async def run() -> list[str]:
        return list(
            await asyncio.gather(
                *(
                    ingest_resolve.single_flight(("package", "o", "ns", "a/package.json"), read)
                    for _ in range(5)
                )
            )
        )

    assert asyncio.run(run()) == ["text"] * 5 and calls == ["x"]
    asyncio.run(run())
    assert calls == ["x", "x"], "nothing kept past the read: the TTL caches decide that"


def test_a_cancelled_first_caller_does_not_cancel_the_read_the_others_share() -> None:
    """A `code.refresh` whose wait timed out is cancelled mid-read; the worker pass
    sharing that read must still get the text, not a CancelledError that ends its loop."""
    key = ("package", "o", "ns", "b/package.json")

    async def read() -> str:
        await asyncio.sleep(0.02)
        return "text"

    async def run() -> str:
        first = asyncio.ensure_future(ingest_resolve.single_flight(key, read))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(ingest_resolve.single_flight(key, read))
        await asyncio.sleep(0)
        first.cancel()
        got = await second
        assert first.cancelled()
        assert key not in ingest_resolve._IN_FLIGHT, "the finished read leaves the table"
        return str(got)

    assert asyncio.run(run()) == "text"


class _Memo(_Repo):
    async def chunks_of_path(self, org: str, ns: str, path: str, *, limit: int = 400) -> list:
        self.reads.append(f"chunks_of:{path}")
        return [{"start_line": 1, "content": f"body of {path}"}]

    async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
        self.reads.append(f"present:{','.join(sorted(paths))}")
        return {p for p in paths if p.endswith(".h")}


def test_a_header_and_a_path_are_read_once_per_round_until_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Memo()
    reads = RoundReads(repo, "o", "code-ns")

    async def run() -> None:
        for _ in range(3):
            got = await reads.chunks_of_path("o", "code-ns", "a.h")
            got[0]["content"] = "mutated by a caller"
        assert (await reads.chunks_of_path("o", "code-ns", "a.h"))[0]["content"] == "body of a.h"
        assert await reads.live_paths_present("o", "code-ns", ["a.h", "b.cpp"]) == {"a.h"}
        assert await reads.live_paths_present("o", "code-ns", ["b.cpp", "a.h"]) == {"a.h"}
        # the round writes a.h's chunks and b.cpp's file row: both re-read,
        # after the buffer is written
        await reads.insert_rows("code_chunks", [{"path": "a.h", "chunk_id": "x"}])
        await reads.insert_rows("code_files", [{"path": "b.cpp"}])
        await reads.chunks_of_path("o", "code-ns", "a.h")
        await reads.live_paths_present("o", "code-ns", ["a.h", "b.cpp"])

    asyncio.run(run())
    assert repo.reads == [
        "chunks_of:a.h",
        "present:a.h,b.cpp",
        "chunks_of:a.h",
        "present:b.cpp",
    ]
    assert [t for t, _ in repo.inserts] == ["code_chunks", "code_files"], (
        "flushed before the re-read"
    )


# The flush rule trusts a `path` / `paths` parameter to mean "reads rows of
# these paths only". Every CodeRepo read with one must filter on it — or be
# named here with why it cannot see a buffered row.
_PATH_PARAM_REVIEWED = {
    "chunks_full_by_ids": "WHERE (org, namespace, embedding_model, path, chunk_id) IN …",
    "types_for_paths": "delegates to functions_for_paths(paths)",
    "first_owner_of_paths": "ownership table, never written by the ingest",
    "gh_prs_matching": "GitHub PR tables, never written by the ingest",
    "pr_churn_for": "PR tables, never written by the ingest",
    "pr_churn_touching": "PR tables, never written by the ingest",
    "pr_history_for": "PR tables, never written by the ingest",
    "pr_history_in_window": "PR tables, never written by the ingest",
    "prs_overlapping_paths": "PR tables, never written by the ingest",
    "prs_touching_in_window": "PR tables, never written by the ingest",
}


def test_every_path_scoped_repository_read_filters_on_its_paths() -> None:
    import inspect
    import re

    from server.code.store.index import CodeRepo

    unconfirmed = []
    for name, fn in inspect.getmembers(CodeRepo, inspect.iscoroutinefunction):
        params = inspect.signature(fn).parameters
        if "path" not in params and "paths" not in params:
            continue
        src = inspect.getsource(fn)
        if re.search(r"\b\w*path\s*(=|IN)\s*\{\{?paths?:", src) or name in _PATH_PARAM_REVIEWED:
            continue
        unconfirmed.append(name)
    assert unconfirmed == [], (
        "a CodeRepo read with a path/paths parameter the round buffer's flush rule "
        f"cannot trust: {unconfirmed}"
    )


def test_a_reader_waits_for_a_flush_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dependents run concurrently over one RoundReads: a task's read of a
    path whose rows another task is still writing must see them written."""
    order: list[str] = []

    class _Slow(_Probe):
        async def insert_rows(self, table: str, rows: list) -> None:
            order.append("insert:start")
            await asyncio.sleep(0.02)
            order.append("insert:done")
            await super().insert_rows(table, rows)

        async def live_text_for_path(self, org: str, ns: str, path: str) -> str:
            order.append(f"read:{path}")
            return ""

    reads = RoundReads(_Slow(), "o", "code-ns")

    async def run() -> None:
        await reads.insert_rows("code_chunks", [{"path": "a.h"}])
        writer = asyncio.create_task(reads.flush())
        await asyncio.sleep(0)  # the flush has started and holds a.h in flight
        await reads.live_text_for_path("o", "code-ns", "a.h")
        await writer

    asyncio.run(run())
    assert order == ["insert:start", "insert:done", "read:a.h"]

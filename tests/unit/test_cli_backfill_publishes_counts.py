"""A CLI backfill phase moves rows outside the ingest pass, which is what publishes
the shared counts and coverage rows a status read serves. Each phase that declared
its org republishes them once, after its last watermark write; the rows are read back
through `CodeRepo` from a replica that has not seen this run's writes unless the read
asks for sequential consistency."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import pytest

from server.cli import code_backfill
from server.code.store.index import CodeRepo
from server.code.store.index.status import counts_fingerprint, parse_shared_coverage
from server.core.ch import sequential_reads, sequential_reads_active


class _Replica:
    """`loom.code_index_state` and the chunk table of one namespace. A state read
    sees the rows as they were when the run started unless it reads with sequential
    consistency: a replica lagging every insert of this process."""

    def __init__(
        self,
        state: dict[str, str],
        *,
        chunks: int = 0,
        embedded: int = 0,
        embeddable: int | None = None,
    ) -> None:
        self.rows = {
            k: {"state_value": v, "detail": "", "updated_at": None} for k, v in state.items()
        }
        self.lagging = {k: dict(r) for k, r in self.rows.items()}
        embeddable = chunks if embeddable is None else embeddable
        self.chunks = {"n": chunks, "embedded": embedded, "embeddable": embeddable}
        self.inserted: list[str] = []
        self.interrupt_counts = False
        self.fail_coverage: type[BaseException] | None = None
        self.refuse_writes: set[str] = set()

    def _state(self) -> dict[str, dict[str, Any]]:
        return self.rows if sequential_reads_active() else self.lagging

    def _read(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM loom.code_index_state" in sql:
            state = self._state()
            if "{key:String}" in sql or "'table_counts'" in sql:
                key = params.get("key", "table_counts")
                return [dict(state[key])] if key in state else []
            keys = params.get("keys") or sorted(state)
            return [{"state_key": k, **state[k]} for k in sorted(keys) if k in state]
        if "AS described" in sql:  # the coverage's chunks per language
            if self.fail_coverage is not None:
                raise self.fail_coverage
            return [
                {
                    "lang": "python",
                    "chunks": self.chunks["n"],
                    "described": 0,
                    "last_at": None,
                    "embedded": self.chunks["embedded"],
                }
            ]
        if "AS embeddable" in sql:
            if self.interrupt_counts:
                raise asyncio.CancelledError  # Ctrl-C while the phase republishes
            return [dict(self.chunks)]
        if "count() AS n FROM loom." in sql:
            return [{"n": 1}]
        return []  # the coverage aggregates of an empty namespace

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return self._read(sql, dict(params or {}))

    async def query_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        rows = self._read(sql, dict(params or {}))
        return rows[0] if rows else None

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.code_index_state", table
        for r in rows:
            if r["state_key"] in self.refuse_writes:
                raise ConnectionError(f"{r['state_key']} not written")
            self.inserted.append(r["state_key"])
            self.rows[r["state_key"]] = {
                "state_value": r["state_value"],
                "detail": r["detail"],
                "updated_at": r["updated_at"],
            }


class _Namespace(CodeRepo):
    """The real state, counts and coverage code over `_Replica`; the embed queue and
    the chunk writes are the replica's chunk numbers."""

    def __init__(self, replica: _Replica, *, pages: int = 0) -> None:
        super().__init__(ch=replica)  # type: ignore[arg-type]
        self.replica = replica
        self.pages = pages

    async def pending_embed_chunks(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        if not self.pages:
            return []
        self.pages -= 1
        return [{"org": "acme", "namespace": "code-a", "content": "def f(): pass"}]

    @staticmethod
    def tombstone_of(table: str, live_row: dict[str, Any]) -> dict[str, Any]:
        return dict(live_row)

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert table == "code_chunks"
        self.replica.chunks["embedded"] += sum(1 for r in rows if r.get("embedding_dim"))


def _read_back(repo: _Namespace) -> dict[str, Any]:
    async def read() -> Any:
        with sequential_reads():
            return await repo.all_state("acme", "code-a")

    return dict(asyncio.run(read()))


def _published(state: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = json.loads(state["table_counts"]["value"])
    return counts


def _coverage_embedded(state: dict[str, Any]) -> int:
    cov = json.loads(state["coverage"]["value"])["cov"]
    return sum(int(c["embedded"]) for c in cov["chunks"])


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out: list[str] = []

    class _Embedder:
        model_name, dim = "m", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [[0.5, 0.5] for _ in texts]

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def declare(_ch: Any, args: argparse.Namespace) -> None:
        out.append(str(getattr(args, "org", "")))

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Embedder())
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(code_backfill, "_record_manual_pass", nothing)
    monkeypatch.setattr(code_backfill, "_compact_after_bulk", nothing)
    monkeypatch.setattr(code_backfill, "connect_code_plane", declare)
    monkeypatch.setattr("server.code.store.index.status._COUNTS_CACHE", {})
    return out


def _embed(repo: _Namespace, org: str = "acme") -> int:
    args = argparse.Namespace(org=org)
    return asyncio.run(code_backfill._backfill_embed(repo, org, "code-a", args))


_COUNTS = {"code_chunks": 4, "code_chunks_embedded": 2, "code_chunks_embeddable": 4}


def test_a_phase_that_embedded_publishes_the_new_coverage(declared: list[str]) -> None:
    repo = _Namespace(
        _Replica({"table_counts": json.dumps(_COUNTS)}, chunks=4, embedded=2), pages=2
    )
    assert _embed(repo) == 0
    state = _read_back(repo)
    assert declared == ["acme"]
    assert _published(state)["code_chunks_embedded"] == 4
    assert parse_shared_coverage(state.get("coverage"), counts_fingerprint(state)) is not None


def test_an_embed_phase_behind_its_published_counts_republishes(declared: list[str]) -> None:
    # the last page landed and the process died before it republished
    repo = _Namespace(_Replica({"table_counts": json.dumps(_COUNTS)}, chunks=4, embedded=4))
    assert _embed(repo) == 0
    assert declared == ["acme"]
    assert _published(_read_back(repo))["code_chunks_embedded"] == 4


def test_an_embed_phase_declares_before_it_republishes(
    declared: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli import _ch

    async def refuse(_ch_client: Any, _args: argparse.Namespace) -> None:
        raise _ch.OrgNotHeld("not held here")

    monkeypatch.setattr(code_backfill, "connect_code_plane", refuse)
    repo = _Namespace(_Replica({"table_counts": json.dumps(_COUNTS)}, chunks=4, embedded=4))
    with pytest.raises(_ch.OrgNotHeld):
        _embed(repo)
    assert repo.replica.inserted == []


@pytest.mark.parametrize("embedded", [4, 3], ids=["all-embedded", "one-poisoned"])
def test_an_idle_embed_phase_declares_and_writes_nothing(
    declared: list[str], embedded: int
) -> None:
    # every vector that can be embedded is, and the published counts already say so
    done = {**_COUNTS, "code_chunks_embedded": embedded}
    repo = _Namespace(_Replica({"table_counts": json.dumps(done)}, chunks=4, embedded=embedded))
    assert _embed(repo) == 0
    assert declared == [] and repo.replica.inserted == []


@pytest.mark.parametrize("stop", [asyncio.CancelledError, MemoryError], ids=["ctrl-c", "error"])
def test_an_embed_phase_stopped_before_its_coverage_is_republished_by_the_rerun(
    declared: list[str], stop: type[BaseException]
) -> None:
    # The coverage row is published first: until it lands the counts stay behind,
    # which is what the rerun reads.
    coverage = json.dumps({"cov": {"chunks": [{"embedded": 2}]}, "gaps": None})
    state = {"table_counts": json.dumps(_COUNTS), "coverage": coverage}
    repo = _Namespace(_Replica(state, chunks=4, embedded=2), pages=2)
    repo.replica.fail_coverage = stop
    if stop is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            _embed(repo)
    else:
        assert _embed(repo) == 0
    assert _published(_read_back(repo))["code_chunks_embedded"] == 2
    repo.replica.fail_coverage = None
    assert _embed(repo) == 0
    state_after = _read_back(repo)
    assert _published(state_after)["code_chunks_embedded"] == 4
    assert _coverage_embedded(state_after) == 4


def test_a_refused_coverage_write_keeps_the_counts_behind_for_the_rerun(
    declared: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The real store's coverage write fails: the counts stay where they were, so
    # the next phase still finds them behind and republishes both rows.
    repo = _Namespace(
        _Replica({"table_counts": json.dumps(_COUNTS)}, chunks=4, embedded=2), pages=2
    )
    repo.replica.refuse_writes = {"coverage"}
    assert _embed(repo) == 0
    assert "table_counts" not in repo.replica.inserted
    assert "coverage and counts not republished: ConnectionError" in capsys.readouterr().err
    repo.replica.refuse_writes = set()
    assert _embed(repo) == 0
    state = _read_back(repo)
    assert _published(state)["code_chunks_embedded"] == 4
    assert _coverage_embedded(state) == 4


def test_current_counts_over_a_coverage_row_behind_republish_the_coverage(
    declared: list[str],
) -> None:
    # The coverage row was written before the last vectors, the counts row after
    # them: the counts match the chunks and the coverage still trails.
    coverage = json.dumps({"cov": {"chunks": [{"embedded": 2}]}, "gaps": None})
    done = {**_COUNTS, "code_chunks_embedded": 4}
    state = {"table_counts": json.dumps(done), "coverage": coverage}
    repo = _Namespace(_Replica(state, chunks=4, embedded=4))
    assert _embed(repo) == 0
    assert declared == ["acme"]
    assert _coverage_embedded(_read_back(repo)) == 4


def test_a_coverage_row_alone_behind_the_chunks_is_republished_by_the_rerun(
    declared: list[str],
) -> None:
    # The index phase published the coverage and its first counts write failed; the
    # embed phase then landed its last vectors and stopped before publishing.
    coverage = json.dumps({"cov": {"chunks": [{"embedded": 2}]}, "gaps": None})
    repo = _Namespace(_Replica({"coverage": coverage}, chunks=4, embedded=4))
    assert _embed(repo) == 0
    assert declared == ["acme"]
    state = _read_back(repo)
    assert _published(state)["code_chunks_embedded"] == 4
    assert _coverage_embedded(state) == 4


@pytest.mark.parametrize("counts_row", [None, "zeros"])
def test_an_empty_embed_phase_for_an_unindexed_org_declares_nothing(
    declared: list[str], counts_row: str | None
) -> None:
    # A mistyped --org: `loom code stats` with the same typo wrote a row of zeros.
    zeros = {k: 0 for k in _COUNTS}
    state = {"table_counts": json.dumps(zeros)} if counts_row else {}
    repo = _Namespace(_Replica(state))
    assert _embed(repo, org="acmee") == 0
    assert declared == [] and repo.replica.inserted == []


def test_a_failed_refresh_does_not_fail_the_phase(
    declared: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    class _Down(_Namespace):
        async def refresh_table_counts(self, *_a: Any, **_k: Any) -> dict[str, int]:
            raise ConnectionError("down")

    repo = _Down(_Replica({}, chunks=1), pages=1)
    assert _embed(repo) == 0
    assert repo.replica.inserted == ["coverage"]
    assert "table counts not republished: ConnectionError: down" in capsys.readouterr().err


def _index(
    monkeypatch: pytest.MonkeyPatch,
    repo: _Namespace,
    *,
    held: str = "",
    paths_file: str = "",
    suffix: str = "",
) -> int:
    async def head(*_a: Any) -> str:
        return "new"

    async def paths(*_a: Any) -> list[str]:
        return ["a.py"]

    async def batch(*_a: Any, **_k: Any) -> str:
        return held

    async def no_denylist(*_a: Any) -> None:
        return None

    cfg = argparse.Namespace(
        org="acme", namespace="code-a", git_dir="/g", git_branch="main", path_prefixes=("",)
    )
    cfg.embed_reuse_from = ""
    from server.code.store import index as code_index

    monkeypatch.setattr(code_index, "CodeRepo", lambda: repo)
    monkeypatch.setattr(code_backfill, "_repo_cfg", lambda args: cfg)
    monkeypatch.setattr("server.cli.code_backfill.os.path.isdir", lambda p: True)
    monkeypatch.setattr(code_backfill, "_backfill_head", head)
    monkeypatch.setattr(code_backfill, "_backfill_paths", paths)
    monkeypatch.setattr(code_backfill, "_held_batch", batch)
    monkeypatch.setattr("server.code.app.index.denylist.load_denylist", no_denylist)
    args = argparse.Namespace(
        org="acme",
        namespace="code-a",
        phase="index",
        embed=False,
        limit=0,
        paths_file=paths_file,
        embedding_model_suffix=suffix,
        concurrency=1,
        workers=1,
        no_edges_second_pass=True,
    )
    return asyncio.run(code_backfill._backfill(args))


@pytest.mark.parametrize("cursor", ["", "1"], ids=["fresh", "resumed-at-the-end"])
def test_a_full_index_run_publishes_both_rows_at_its_new_watermark(
    declared: list[str], monkeypatch: pytest.MonkeyPatch, cursor: str
) -> None:
    # The rows carry the counts fingerprint, `git_head` included; a reader serves the
    # coverage row only while it matches. "1": the cursor of a run that died after its
    # last batch, before it published.
    repo = _Namespace(_Replica({"git_head": "old", "backfill_cursor:index": cursor}))
    assert _index(monkeypatch, repo) == 0
    state = _read_back(repo)
    final = counts_fingerprint(state)
    assert state["git_head"]["value"] == "new" and state["backfill_cursor:index"]["value"] == ""
    assert state["table_counts"]["detail"] == final
    assert parse_shared_coverage(state.get("coverage"), final) is not None


def test_an_interrupted_publish_leaves_the_cursor_for_the_rerun(
    declared: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _Namespace(_Replica({"git_head": "old"}))
    repo.replica.interrupt_counts = True
    with pytest.raises(asyncio.CancelledError):
        _index(monkeypatch, repo)
    state = _read_back(repo)
    assert state["git_head"]["value"] == "new"
    assert state["backfill_cursor:index"]["value"] == "1"  # the rerun resumes at the end
    repo.replica.interrupt_counts = False
    repo.replica.lagging = {k: dict(r) for k, r in repo.replica.rows.items()}
    assert _index(monkeypatch, repo) == 0
    state = _read_back(repo)
    assert state["table_counts"]["detail"] == counts_fingerprint(state)
    assert state["backfill_cursor:index"]["value"] == ""


@pytest.mark.parametrize(
    ("held", "paths_file", "suffix", "rc"),
    [("held: switched off", "", "", 1), ("", "/paths.txt", "", 0), ("", "", "-twin", 0)],
    ids=["hold", "repair", "twin"],
)
def test_a_hold_a_repair_and_a_twin_publish_at_the_unchanged_watermark(
    declared: list[str],
    monkeypatch: pytest.MonkeyPatch,
    held: str,
    paths_file: str,
    suffix: str,
    rc: int,
) -> None:
    repo = _Namespace(_Replica({"git_head": "old", "backfill_cursor:index": ""}, chunks=2))
    assert _index(monkeypatch, repo, held=held, paths_file=paths_file, suffix=suffix) == rc
    state = _read_back(repo)
    assert state["git_head"]["value"] == "old"
    assert state["table_counts"]["detail"] == counts_fingerprint(state) == "old||||"
    assert parse_shared_coverage(state.get("coverage"), "old||||") is not None
    # the next embed phase finds the published figures current: nothing to declare
    repo.replica.inserted.clear()
    assert _embed(repo) == 0
    assert declared == ["acme"] and repo.replica.inserted == []

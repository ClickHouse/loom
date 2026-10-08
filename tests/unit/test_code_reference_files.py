"""A test's expected output is in the index, unembedded.

`tests/queries/**/*.reference` (and `.reference.j2`, `.expect`) index as
language `reference`: one `file_header` chunk per file, the head capped at
4,000 chars with an elision marker and a `truncated:` description, lexemes
from that head, no embedding (the embed-recovery read excludes the marker),
no symbols, no edges, no extractor-stamp move. `code.files` and
`code.test_home` name the sibling next to a test; `code.outline` on a
fixture returns its head.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.index import ingest, ingest_parse, ingest_stats
from server.code.domain.extract import extractor_stamp as es
from server.code.domain.extract import lang_reference as lr
from server.settings import settings

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

ST = "tests/queries/0_stateless/"
REF = f"{ST}05138_with_fill_step_below_precision.reference"
SMALL = b"1\n100000000000000000\n100000000000000030\n1\t100000000000000000\n"


# ------------------------------------------------------------------ language


def test_reference_paths_are_the_queries_tree_fixtures_only() -> None:
    assert lr.reference_lang_for_path(REF) == "reference"
    assert lr.reference_lang_for_path(f"{ST}00172_hits_joins.reference.j2") == "reference"
    assert lr.reference_lang_for_path(f"{ST}01179_insert_values_semicolon.expect") == "reference"
    assert lr.reference_lang_for_path("tests/queries/1_stateful/00001_x.reference") == "reference"
    assert lr.reference_lang_for_path("src/Foo.reference") == ""  # not a test fixture
    assert lr.reference_lang_for_path(f"{ST}05138_x.sql") == ""
    assert lr.reference_lang_for_path(f"{ST}data_csv/a.csv") == ""


def test_parse_for_ingest_yields_a_chunk_only_reference_file_and_skips_over_a_megabyte() -> None:
    pf, err = ingest_parse._parse_for_ingest(REF, SMALL)
    assert pf is not None and pf.lang == "reference" and pf.symbols == [] and err == ""
    big, err = ingest_parse._parse_for_ingest(REF, b"x" * (lr.REFERENCE_MAX_BYTES + 1))
    assert big is None and err == ""
    # the language table itself is untouched: no stamp move for the parsers
    from server.code.domain.extract.treesitter import lang_for_path

    assert lang_for_path(REF) == ""


def test_lang_for_path_of_source_files_is_unchanged() -> None:
    pf, _ = ingest_parse._parse_for_ingest(f"{ST}05138_x.sql", b"SELECT 1;\n")
    assert pf is not None and pf.lang == "sql"


# ------------------------------------------------------------------- chunker


def test_one_file_header_chunk_whole_when_small() -> None:
    c = lr.reference_chunk(REF, SMALL)
    assert (c.kind, c.scope, c.start_line, c.end_line) == ("file_header", "", 1, 4)
    assert c.content == SMALL.decode()
    assert "100000000000000030" in c.lexemes
    assert lr.reference_head(SMALL.decode()) == (SMALL.decode(), "")


def test_head_is_capped_with_an_elision_marker_and_a_truncated_description() -> None:
    lines = [f"row_{i:05d}\tvalue_{i:05d}" for i in range(2000)]
    text = "\n".join(lines) + "\n"
    content, desc = lr.reference_head(text)
    assert content.startswith(text[:4000]) and "more chars elided" in content
    assert len(content) < 4000 + 80
    assert desc == f"truncated: head 4000 of {len(text)} chars"
    c = lr.reference_chunk(REF, text.encode())
    assert c.end_line == 2000  # the span still names the whole file
    # lexemes come from the head only: a value past the cap is not findable
    assert "row_00010" in c.lexemes and "row_01999" not in c.lexemes


def test_reference_chunk_id_is_content_addressed_like_every_other_chunk() -> None:
    a = lr.reference_chunk(REF, SMALL).chunk_id("n")
    b = lr.reference_chunk(REF, SMALL).chunk_id("n")
    c = lr.reference_chunk(REF, SMALL + b"2\n").chunk_id("n")
    assert a == b != c


# ------------------------------------------------------------------- ingest


class _Repo:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def live_chunk_index_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_chunks")
        return []

    async def chunks_full_by_ids(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_chunks:full")
        return []

    async def live_symbols_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_symbols")
        return []

    async def name_candidates(self, *_: Any) -> dict[str, Any]:
        self.reads.append("code_symbol_names")
        return {}

    async def live_edges_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_edges")
        return []

    async def live_call_misses_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_call_sites_unresolved")
        return []

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, rows))

    def rows(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows in self.inserts for r in rows if t == table]


class _Embedder:
    model_name = "m"

    def __init__(self) -> None:
        self.calls = 0

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.1, 0.2] for _ in texts]


def _ingest(repo: _Repo, source: bytes, embedder: Any = None) -> ingest_stats.FileIngestStats:
    return asyncio.run(
        ingest.ingest_file(
            repo,
            "o",
            "n",
            path=REF,
            source=source,
            commit_sha="c" * 40,
            embedding_model="m",
            embedder=embedder,
        )
    )


def test_ingest_writes_one_unembedded_chunk_and_a_file_row_and_reads_no_symbol_or_edge_rows() -> (
    None
):
    repo = _Repo()
    emb = _Embedder()
    stats = _ingest(repo, SMALL, embedder=emb)
    assert emb.calls == 0, "a reference fixture is never sent to the embedder"
    chunks = repo.rows("code_chunks")
    assert len(chunks) == 1 and stats.chunks_new == 1 and stats.embedded == 0
    row = chunks[0]
    assert row["kind"] == "file_header" and row["embedding_dim"] == 0 and row["embedding"] == []
    assert row["embed_last_error"] == lr.UNEMBEDDED_BY_DESIGN
    assert row["embed_attempts"] == 0 and row["context_text"] == ""
    assert row["context_description"] == "" and row["context_source"] == "structural"
    assert "100000000000000030" in row["lexemes"]
    assert stats.embed_stubs == 0, "not a stub the recovery loop should count"
    assert repo.rows("code_symbols") == [] and repo.rows("code_edges") == []
    assert "code_symbols" not in repo.reads and "code_edges" not in repo.reads
    files = repo.rows("code_files")
    assert len(files) == 1 and files[0]["lang"] == "reference" and files[0]["chunk_count"] == 1
    assert files[0]["symbol_count"] == 0 and files[0]["index_error"] == ""
    assert stats.stages_run == ("chunk", "symbol", "edge", "guard") and not stats.skipped


def test_ingest_of_a_long_fixture_records_the_truncation_in_the_description() -> None:
    repo = _Repo()
    text = ("0.5\n" * 3000).encode()
    _ingest(repo, text)
    row = repo.rows("code_chunks")[0]
    assert row["context_description"] == f"truncated: head 4000 of {len(text)} chars"
    assert row["context_source"] == "structural"
    assert len(row["content"]) < 4100


def test_ingest_skips_a_fixture_over_the_byte_cap_without_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the global file cap (code_ingest_max_file_bytes) runs first and records
    # an error row; with it out of the way the reference rule itself skips
    # the file outright — no chunk, no file row
    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 0)
    repo = _Repo()
    stats = _ingest(repo, b"x" * (lr.REFERENCE_MAX_BYTES + 1))
    assert stats.skipped and repo.inserts == []
    repo = _Repo()
    stats = _ingest(repo, b"x" * lr.REFERENCE_MAX_BYTES)
    assert not stats.skipped and len(repo.rows("code_chunks")) == 1


# -------------------------------------------------------------------- stamp


def test_reference_module_moves_no_extractor_stamp() -> None:
    assert "lang_reference.py" not in es.EXTRACTOR_MODULES
    for stage in es.STAGES:
        assert "lang_reference.py" not in es.stage_modules(stage)
    here = pathlib.Path(es.__file__).resolve().parent
    with_it = es.compute_stage_stamps(here, {})
    # the stamp is a hash over the listed modules only: this file's presence
    # (or a future edit to it) cannot change any stage's value
    assert with_it == es.compute_stage_stamps(here, {})
    from tests.unit.test_code_extractor_stamp import NON_EXTRACTOR_MODULES

    assert "reference fixtures" in NON_EXTRACTOR_MODULES["lang_reference.py"]


# ------------------------------------------------------------ embed recovery


def test_pending_embed_read_excludes_the_unembedded_marker_under_both_filters() -> None:
    from server.code.store.index import CodeRepo

    class _CH:
        def __init__(self) -> None:
            self.sql: list[str] = []
            self.params: list[dict[str, Any]] = []

        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            self.sql.append(sql)
            self.params.append(params)
            return []

    for poisoned in (False, True):
        ch = _CH()
        repo = CodeRepo(ch)  # type: ignore[arg-type]
        asyncio.run(repo.pending_embed_chunks("o", "n", "m", 3, 10, include_poisoned=poisoned))
        # phase 1 judges the key's newest version
        assert "NOT startsWith(argMax(embed_last_error, version), {unembedded:String})" in ch.sql[0]
        assert "PREWHERE" not in ch.sql[0] and "GROUP BY path, chunk_id" in ch.sql[0]
        assert ch.params[0]["unembedded"] == lr.UNEMBEDDED_BY_DESIGN


# ----------------------------------------------------------- reference sibling


def test_sibling_candidates_and_lookup() -> None:
    assert lr.reference_candidates(f"{ST}05138_x.sql") == (
        f"{ST}05138_x.reference",
        f"{ST}05138_x.reference.j2",
    )
    assert lr.reference_candidates(f"{ST}00172_hits_joins.sql.j2")[1] == (
        f"{ST}00172_hits_joins.reference.j2"
    )
    assert lr.reference_candidates(f"{ST}01179_x.expect") == (
        f"{ST}01179_x.reference",
        f"{ST}01179_x.reference.j2",
    )
    assert lr.reference_candidates(f"{ST}01179_x.sh") == (
        f"{ST}01179_x.reference",
        f"{ST}01179_x.reference.j2",
    )
    assert lr.reference_candidates(f"{ST}05138_x.reference") == ()
    assert lr.reference_candidates("src/Foo.cpp") == ()
    assert lr.reference_candidates("tests/integration/test_x/test.py") == ()
    present = {f"{ST}05138_x.reference"}
    assert lr.reference_sibling(f"{ST}05138_x.sql", present) == f"{ST}05138_x.reference"
    assert lr.reference_sibling(f"{ST}05139_y.sql", present) is None
    assert lr.reference_sibling("src/Foo.cpp", present) is None


class _FilesRepo:
    def __init__(self, files: list[dict[str, Any]], present: set[str]) -> None:
        self._files = files
        self._present = present
        self.present_calls: list[list[str]] = []

    async def list_files(self, org: str, ns: str, **kw: Any) -> tuple[list[dict[str, Any]], int]:
        return [dict(f) for f in self._files], len(self._files)

    async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
        self.present_calls.append(list(paths))
        return {p for p in paths if p in self._present}


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


async def _as_of(*a: Any, **k: Any) -> dict[str, Any]:
    return {}


def _files(repo: Any, **kw: Any) -> Any:
    from server.code.api.models_grep import CodeFilesInput
    from server.code.app import files as files_mod

    async def go() -> Any:
        with (
            patch.object(files_mod, "CodeRepo", return_value=repo),
            patch.object(files_mod, "index_as_of", new=_as_of),
            patch.object(files_mod, "schedule_code_trace", new=lambda **k: None),
        ):
            return await files_mod.handle_code_files(CodeFilesInput(org="o", namespace="n", **kw))

    return asyncio.run(go())


def _file(path: str, lang: str) -> dict[str, Any]:
    return {"path": path, "lang": lang, "size_bytes": 10, "loc": 1, "indexed_commit": "c"}


def test_code_files_names_the_reference_sibling_of_a_test_and_leaves_source_rows_alone() -> None:
    files = [
        _file(f"{ST}05138_with_fill_step_below_precision.sql", "sql"),
        _file(REF, "reference"),
        _file(f"{ST}05139_no_reference.sh", "shell"),
        _file("src/Foo.cpp", "cpp"),
    ]
    repo = _FilesRepo(files, present={REF})
    out = _files(repo, stem="05138_with_fill")
    by_path = {f["path"]: f for f in out.files}
    assert by_path[f"{ST}05138_with_fill_step_below_precision.sql"]["reference"] == REF
    assert by_path[f"{ST}05139_no_reference.sh"]["reference"] is None
    assert "reference" not in by_path["src/Foo.cpp"], "a source row is byte for byte as before"
    assert "reference" not in by_path[REF], "the fixture is its own row, not a test"
    assert by_path[REF]["lang"] == "reference"
    # one membership read carrying every candidate
    assert len(repo.present_calls) == 1
    assert (
        REF in repo.present_calls[0]
        and f"{ST}05139_no_reference.reference" in repo.present_calls[0]
    )
    assert out.total == 4


def test_code_files_with_no_test_rows_does_not_read_the_file_table_again() -> None:
    repo = _FilesRepo([_file("src/Foo.cpp", "cpp")], present=set())
    out = _files(repo, path_prefix="src/")
    assert repo.present_calls == [] and "reference" not in out.files[0]


# ----------------------------------------------------------- code.test_home


def test_test_home_candidates_carry_their_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_testing import CodeTestHomeInput
    from server.code.app import coverage as h
    from server.code.domain.index import coverage_claim as cc

    home = f"{ST}02994_sanity_check_settings.sql"

    class _Plane:
        present_calls: list[list[str]] = []

        async def facets_present(self, org: str, ns: str) -> bool:
            return False  # a namespace without facets: test_home greps every part

        async def tests_for_facets(self, *a: Any, **k: Any) -> Any:
            return [], 0

        async def grep_files(self, org: str, ns: str, pattern: str, **kw: Any) -> Any:
            if pattern == cc.grep_pattern("symbol", "arrayJoin"):
                return [{"path": home, "chunks": 1, "first_line": 1}], 1
            return [{"path": home, "chunks": 2, "first_line": 3}], 1

        async def paths_by_stem(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return []

        async def max_stateless_test_number(self, *a: Any) -> int:
            return 4100

        async def test_map_for(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return []

        async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
            self.present_calls.append(list(paths))
            return {f"{ST}02994_sanity_check_settings.reference"}

    plane = _Plane()

    async def go() -> Any:
        with (
            patch.object(h, "CodeRepo", return_value=plane),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_test_home(
                CodeTestHomeInput(org="o", namespace="n", settings=["max_threads"])
            )

    out = asyncio.run(go())
    assert out.home == home
    assert out.candidates[0]["reference"] == f"{ST}02994_sanity_check_settings.reference"
    assert set(out.candidates[0]) == {"path", "matched", "map_confidence", "score", "reference"}
    assert plane.present_calls and plane.present_calls[0][0].endswith(
        "02994_sanity_check_settings.reference"
    )


# -------------------------------------------------------------- code.outline


class _OutlineRepo:
    def __init__(self, file_row: dict[str, Any] | None, chunk: dict[str, Any] | None) -> None:
        self._file_row = file_row
        self._chunk = chunk
        self.chunk_reads = 0

    async def outline_for_path(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return []

    async def live_files_for_path(self, *a: Any) -> list[dict[str, Any]]:
        return [self._file_row] if self._file_row else []

    async def chunks_of_path(self, org: str, ns: str, path: str, *, limit: int = 400) -> Any:
        self.chunk_reads += 1
        assert limit == 1
        return [self._chunk] if self._chunk else []


def _outline(repo: Any, path: str) -> Any:
    from server.code.api.models_navigation import CodeOutlineInput
    from server.code.app.symbol import outline as h

    async def go() -> Any:
        with (
            patch.object(h, "CodeRepo", return_value=repo),
            patch.object(h, "index_as_of", new=_as_of),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_outline(CodeOutlineInput(org="o", namespace="n", path=path))

    return asyncio.run(go())


def test_outline_of_a_reference_file_returns_its_head() -> None:
    chunk = {"start_line": 1, "end_line": 4, "content": SMALL.decode(), "context_description": ""}
    repo = _OutlineRepo({"path": REF, "lang": "reference", "index_error": ""}, chunk)
    out = _outline(repo, REF)
    assert out.head == SMALL.decode() and out.head_truncated is False
    assert out.entries == [] and out.verdict == "" and out.indexed is True
    assert out.tokens_returned == len(SMALL) // 4
    data = out.model_dump()
    assert data["head"] == SMALL.decode() and data["head_truncated"] is False


def test_outline_of_a_truncated_reference_says_so() -> None:
    chunk = {
        "start_line": 1,
        "end_line": 9000,
        "content": "0.5\n" * 1000 + "… elided",
        "context_description": "truncated: head 4000 of 36000 chars",
    }
    repo = _OutlineRepo({"path": REF, "lang": "reference", "index_error": ""}, chunk)
    out = _outline(repo, REF)
    assert out.head_truncated is True and out.head.startswith("0.5\n")


def test_outline_of_a_source_file_is_byte_identical_and_reads_no_chunk() -> None:
    repo = _OutlineRepo({"path": "src/a.sql", "lang": "sql", "index_error": ""}, None)
    out = _outline(repo, "src/a.sql")
    data = out.model_dump()
    assert "head" not in data and "head_truncated" not in data
    assert out.verdict == "no_indexed_paths" and repo.chunk_reads == 0


def test_outline_of_an_unindexed_reference_path_keeps_its_verdict() -> None:
    repo = _OutlineRepo(None, None)
    out = _outline(repo, REF)
    assert out.verdict == "no_indexed_paths" and "head" not in out.model_dump()
    assert repo.chunk_reads == 0

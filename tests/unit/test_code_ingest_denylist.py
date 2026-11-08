"""Ingest denylist: registry `exclude` globs and the
repo-local ignore file are always honoured, the built-in
generated/vendored rules only when switched on, binaries never index, and
an excluded path's legacy rows are tombstoned by the ingest that refuses
it."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.index import ingest
from server.code.app.index.denylist import (
    Denylist,
    denylist_for,
    load_denylist,
    parse_ignore_file,
)
from server.code.domain.index.repos import configured_repos
from server.settings import settings
from tests.unit.code_fixtures import repo_config

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")


def test_builtin_off_by_default_matches_nothing() -> None:
    d = Denylist()
    for p in ("src/a.pb.cc", "x/node_modules/y.js", "contrib/poco/a.cpp", "src/t.generated.cpp"):
        assert d.reason(p) == ""


def test_builtin_rules_when_on() -> None:
    d = Denylist(builtin=True)
    assert d.reason("src/proto/a.pb.cc").startswith("excluded: suffix .pb.cc")
    assert d.reason("py/gen/foo_pb2.py").startswith("excluded: suffix _pb2.py")
    assert d.reason("web/app.min.js").startswith("excluded: suffix .min.js")
    assert d.reason("web/types.d.ts").startswith("excluded: suffix .d.ts")
    assert d.reason("poetry.lock").startswith("excluded: suffix .lock")
    assert d.reason("src/Functions/URL/tldLookup.generated.cpp").startswith("excluded: generated")
    assert d.reason("web/node_modules/x/index.js") == "excluded: dir node_modules/"
    assert d.reason("tools/build/gen.py") == "excluded: dir build/"
    assert d.reason("contrib/poco/Foundation/x.cpp") == "excluded: dir contrib/ (per-repo opt-in)"
    assert Denylist(builtin=True, include_contrib=True).reason("contrib/poco/x.cpp") == ""
    # a *file* named build is not a directory rule hit; a normal source is kept
    assert d.reason("src/build") == ""
    assert d.reason("src/Storages/MergeTree/MergeTreeData.cpp") == ""


def test_patterns_are_honoured_without_builtin() -> None:
    d = Denylist(patterns=("base/poco/", "*.generated.cpp", "README.md"))
    assert d.reason("base/poco/Foundation/src/Ascii.cpp") == "excluded: pattern base/poco/"
    assert d.reason("src/base/poco/x.h") == "excluded: pattern base/poco/"
    assert d.reason("src/Functions/URL/tldLookup.generated.cpp").endswith("*.generated.cpp")
    assert d.reason("docs/README.md") == "excluded: pattern README.md"
    assert d.reason("base/base/types.h") == ""
    assert d.reason("src/Functions/generated_helper.cpp") == ""


def test_parse_ignore_file() -> None:
    assert parse_ignore_file("# vendored\n\n/base/poco/\n*.snap \n") == ("base/poco/", "*.snap")


@pytest.mark.asyncio
async def test_load_denylist_reads_repo_local_ignore_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_ingest_denylist_builtin", False, raising=False)

    def Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(
            git_dir="var/x.git",
            exclude=("docs/",),
            include_contrib=False,
            ignore_file=".loomignore",
        )

    async def show_file(git_dir: str, sha: str, path: str, max_bytes: int = 0) -> bytes | None:
        assert (git_dir, sha, path) == ("var/x.git", "deadbeef", ".loomignore")
        return b"# repo says\nprograms/bash-completion/\n"

    d = await load_denylist(Cfg(), "deadbeef", show_file)
    assert d.patterns == ("docs/", "programs/bash-completion/")
    assert d.reason("programs/bash-completion/completions/clickhouse") != ""
    assert d.reason("docs/en/intro.md") != ""

    async def missing(*_: Any) -> None:
        return None

    assert (await load_denylist(Cfg(), "deadbeef", missing)).patterns == ("docs/",)


def test_repo_config_exclude_and_contrib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        (
            '[{"namespace": "code-clickhouse", "exclude": ["base/poco/", "*.generated.cpp"],'
            '  "include_contrib": true, "ignore_file": ""},'
            ' {"namespace": "code-other", "exclude": "vendor/,dist/"}]'
        ),
    )
    monkeypatch.setattr(settings, "code_ingest_exclude", "", raising=False)
    a, b = configured_repos()
    assert a.exclude == ("base/poco/", "*.generated.cpp")
    assert a.include_contrib is True and a.ignore_file == ""
    assert b.exclude == ("vendor/", "dist/")
    assert b.include_contrib is False and b.ignore_file == ".loomignore"
    d = denylist_for(a)
    assert d.reason("base/poco/Foundation/x.cpp") == "excluded: pattern base/poco/"
    assert d.reason("contrib/x/y.cpp") == ""


def test_scalar_exclude_setting_reaches_the_scalar_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_ingest_exclude", "base/poco/, *.snap", raising=False)
    (cfg,) = configured_repos()
    assert cfg.exclude == ("base/poco/", "*.snap")


# ---- ingest_file is the choke point -----------------------------------------


class _StubRepo:
    def __init__(self, live_chunks: int = 0) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self._live = [
            {"chunk_id": f"c{i}", "path": "src/a.pb.cc", "version": 1, "tombstoned_at": None}
            for i in range(live_chunks)
        ]

    async def live_chunks_for_path(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return list(self._live)

    async def live_chunk_index_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def chunks_full_by_ids(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def live_symbols_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def live_edges_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def live_files_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def live_call_misses_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def name_candidates(self, *_: Any) -> dict[str, Any]:
        return {}

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        if rows:
            self.inserts.append((table, rows))


async def _ingest(repo: _StubRepo, path: str, source: bytes, denylist: Denylist | None):
    return await ingest.ingest_file(
        repo,
        "o",
        "code-ns",
        path=path,
        source=source,
        commit_sha="c" * 40,
        embedding_model="m",
        embedder=None,
        denylist=denylist,
    )


@pytest.mark.asyncio
async def test_excluded_path_indexes_nothing_and_tombstones_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tombstoned: list[dict[str, Any]] = []

    def _tomb(table: str, row: dict[str, Any]) -> dict[str, Any]:
        tombstoned.append(row)
        return {**row, "tombstoned_at": "now"}

    monkeypatch.setattr(ingest.CodeRepo, "tombstone_of", staticmethod(_tomb))
    repo = _StubRepo(live_chunks=3)
    stats = await _ingest(repo, "src/a.pb.cc", b"int x;\n", Denylist(builtin=True))
    assert stats.skipped and stats.excluded == "excluded: suffix .pb.cc"
    assert stats.chunks_new == 0 and stats.symbols_new == 0
    assert len(tombstoned) == 3
    tables = [t for t, _ in repo.inserts]
    assert tables == ["code_chunks"]  # only the tombstones; no file row, no new chunks


@pytest.mark.asyncio
async def test_binary_content_is_refused_regardless_of_rules() -> None:
    repo = _StubRepo()
    stats = await _ingest(repo, "src/blob.h", b"\x7fELF\x00\x00\x01" * 10, Denylist())
    assert stats.skipped and stats.excluded == "excluded: binary"
    assert repo.inserts == []


@pytest.mark.asyncio
async def test_default_denylist_indexes_a_normal_source_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_ingest_denylist_builtin", False, raising=False)
    monkeypatch.setattr(settings, "code_ingest_exclude", "", raising=False)
    repo = _StubRepo()
    stats = await _ingest(repo, "src/a.cpp", b"int f() { return 1; }\n", None)
    assert not stats.skipped and stats.excluded == ""
    assert any(t == "code_chunks" for t, _ in repo.inserts)


_GENERATED_GO = b"""// Code generated by protoc-gen-go. DO NOT EDIT.

package pb

type Msg struct{ Name string }

func (m *Msg) GetName() string { return m.Name }
"""


@pytest.mark.asyncio
async def test_a_generated_file_is_declared_not_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry's generated rule keeps the file's TEXT out of
    the index (no chunks; legacy chunks retired) while its declarations —
    the message type and its getter everything else calls — still enter
    the symbol and edge tables, with a file row of zero chunks."""
    tombstoned: list[dict[str, Any]] = []

    def _tomb(table: str, row: dict[str, Any]) -> dict[str, Any]:
        tombstoned.append(row)
        return {**row, "tombstoned_at": "now"}

    monkeypatch.setattr(ingest.CodeRepo, "tombstone_of", staticmethod(_tomb))
    repo = _StubRepo(live_chunks=2)
    stats = await _ingest(repo, "api/msg.pb.go", _GENERATED_GO, Denylist(exclude_generated=True))
    assert not stats.skipped and stats.declared_only
    assert stats.excluded.startswith("excluded: generated")
    assert stats.chunks_new == 0 and stats.symbols_new >= 2  # Msg, Msg::GetName (+ the field)
    tables = {t for t, _ in repo.inserts}
    assert "code_symbols" in tables and "code_files" in tables
    new_chunks = [
        r
        for t, rows in repo.inserts
        if t == "code_chunks"
        for r in rows
        if not r.get("tombstoned_at")
    ]
    assert new_chunks == []
    names = {
        r["name"]
        for t, rows in repo.inserts
        if t == "code_symbols"
        for r in rows
        if not r.get("tombstoned_at")
    }
    assert {"Msg", "GetName"} <= names

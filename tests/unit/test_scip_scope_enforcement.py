"""apply_delta must enforce the repo's configured path prefixes.

A full scip index sweeps the entire compile graph; without server-side
scope enforcement an unfiltered upload would land every vendored
contrib/ symbol in the live index. Files outside the configured prefixes are skipped and
counted, never applied.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index.scip_delta import ScipDelta, ScipFile, apply_delta
from tests.unit.code_fixtures import repo_config


@pytest.mark.asyncio
async def test_out_of_scope_files_are_skipped():
    delta = ScipDelta(
        delta_version=1,
        commit="c" * 40,
        files=[
            ScipFile(path="src/Core/Field.cpp"),
            ScipFile(path="contrib/boost/thing.cpp"),
            ScipFile(path="build/generated/x.cpp"),
        ],
    )
    repo = AsyncMock()
    repo.live_symbols_for_path.return_value = []
    repo.live_edges_for_path.return_value = []
    repo.chunk_spans_for_path.return_value = []

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(path_prefixes=("src/", "base/", "programs/", "tests/queries/"))

    with patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()):
        stats = await apply_delta(repo, "org", "ns", delta)

    assert stats.files == 1
    assert stats.files_skipped_out_of_scope == 2
    visited = {c.args[2] for c in repo.live_symbols_for_path.await_args_list}
    assert visited == {"src/Core/Field.cpp"}


def test_watermark_key_is_producer_scoped() -> None:
    """A python backfill at an older commit must never regress the C++
    tier's scip_commit (it re-widens the next delta window); each producer
    family advances its own watermark."""
    from server.code.workers.scip_apply import watermark_key_for

    assert watermark_key_for("scip_harness") == "scip_commit"
    assert watermark_key_for("") == "scip_commit"
    assert watermark_key_for("scip-python") == "scip_commit_py"
    assert watermark_key_for("SCIP-Python 0.6") == "scip_commit_py"


@pytest.mark.asyncio
async def test_a_kind_change_rewrites_an_unmoved_symbol():
    """The symbol id is name|signature, so a converter that
    now reads `const X = forwardRef(...)` as a function changes nothing the
    key sees; the unchanged-and-unmoved shortcut must compare the kind too
    or the live row keeps the old kind forever."""
    from server.code.app.index.scip_delta import ScipSymbol, symbol_id

    sym = ScipSymbol(
        qualified_name="src.a::BaseButton",
        name="BaseButton",
        kind="function",
        signature="",
        line_start=3,
        line_end=8,
    )
    delta = ScipDelta(
        delta_version=1, commit="c" * 40, files=[ScipFile(path="src/a.tsx", symbols=[sym])]
    )
    repo = AsyncMock()
    repo.live_symbols_for_path.return_value = [
        {
            "qualified_name": "src.a::BaseButton",
            "symbol_id": symbol_id("src.a::BaseButton", ""),
            "start_line": 3,
            "end_line": 8,
            "kind": "const",
            "path": "src/a.tsx",
        }
    ]
    repo.live_edges_for_path.return_value = []
    repo.chunk_spans_for_path.return_value = []

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(path_prefixes=("src/",))

    with patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()):
        stats = await apply_delta(repo, "org", "ns", delta)
    assert stats.symbols_new == 1 and stats.symbols_tombstoned == 0
    # same lines, same kind: the live row is kept
    repo.live_symbols_for_path.return_value[0]["kind"] = "function"
    with patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()):
        stats = await apply_delta(repo, "org", "ns", delta)
    assert stats.symbols_new == 0


@pytest.mark.asyncio
async def test_paths_the_entry_excludes_are_skipped_too():
    """The entry's path denylist binds the compiler tier —
    a `*.pb.go` the exclude globs name, or a vendored directory, never
    enters through a scip index."""
    delta = ScipDelta(
        delta_version=1,
        commit="c" * 40,
        files=[
            ScipFile(path="svc/internal/api.go"),
            ScipFile(path="svc/internal/api.pb.go"),
            ScipFile(path="svc/testdata/golden.go"),
        ],
    )
    repo = AsyncMock()
    repo.live_symbols_for_path.return_value = []
    repo.live_edges_for_path.return_value = []
    repo.chunk_spans_for_path.return_value = []

    def _Cfg(*_a: Any, **_k: Any) -> Any:
        return repo_config(
            path_prefixes=("svc/",),
            exclude=("*.pb.go", "testdata/"),
            include_contrib=False,
            exclude_generated=True,
        )

    with patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()):
        stats = await apply_delta(repo, "org", "ns", delta)

    assert stats.files == 1
    assert stats.files_skipped_excluded == 2
    visited = {c.args[2] for c in repo.live_symbols_for_path.await_args_list}
    assert visited == {"svc/internal/api.go"}


def test_converter_drops_generated_documents(tmp_path) -> None:
    """The producer side of generated-file scoping: the converter reads the document's
    own source and drops Go's generated header and the `@generated` marker;
    a header after the package clause is prose."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "scip_to_delta",
        Path(__file__).resolve().parents[2] / "scripts/scip_harness/scip_to_delta.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    gen = [
        "// Code generated by protoc-gen-go. DO NOT EDIT.",
        "// versions:",
        "",
        "package pb",
        "type X struct{}",
    ]
    licensed = [
        "/*",
        "Copyright",
        "*/",
        "",
        "// Code generated by controller-gen. DO NOT EDIT.",
        "",
        "package v1",
    ]
    prose = ["package main", "// Code generated by hand. DO NOT EDIT."]
    marker = ["/* eslint-disable */", "// @generated by codegen", "export const x = 1;"]
    assert mod._is_generated("a/x.pb.go", gen) is True
    assert mod._is_generated("api/v1/zz_generated.deepcopy.go", licensed) is True
    assert mod._is_generated("a/x.go", prose) is False
    assert mod._is_generated("a/x.ts", marker) is True
    assert mod._is_generated("a/x.go", None) is False
    assert mod._is_generated("a/y.go", ["package y", "func F() {}"]) is False

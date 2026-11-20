"""A full compiler-tier run retires the documents the new
index no longer has."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from server.code.app.index.compiler_tier import gone_documents
from server.code.app.index.delta_file import DeltaFile, open_delta


@pytest.fixture
def make_delta(tmp_path: Path) -> Callable[..., DeltaFile]:
    """A delta read as a full run reads one, from a file of these paths."""

    def make(paths: list[str], deleted: list[str] | None = None) -> DeltaFile:
        path = tmp_path / "delta.json"
        body = {"commit": "abc", "files": [{"path": p} for p in paths]}
        path.write_text(json.dumps({**body, "deleted_files": deleted or []}))
        return open_delta(path)

    return make


def test_live_paths_the_delta_does_not_emit_are_deleted_files(
    make_delta: Callable[..., DeltaFile],
) -> None:
    delta = make_delta(["web/src/a.ts"], ["web/src/old.ts"])
    live = {
        "web/src/a.ts",
        "web/src/moved/b.ts",
        "packages/shared/src/server/query-ast/kysely/dedup.ts",
    }
    assert gone_documents(live, delta, "typescript") == [
        "packages/shared/src/server/query-ast/kysely/dedup.ts",
        "web/src/moved/b.ts",
        "web/src/old.ts",
    ]


def test_nothing_gone_when_every_live_path_is_emitted(make_delta: Callable[..., DeltaFile]) -> None:
    delta = make_delta(["a.ts"])
    assert gone_documents({"a.ts"}, delta, "typescript") == []


def test_a_full_run_never_retires_another_tiers_documents(
    make_delta: Callable[..., DeltaFile],
) -> None:
    """One namespace, two tiers (Python primary, TypeScript beside it): compiler rows
    carry no producer column, so each full run retires only paths its own producer
    could have emitted."""
    live = {"server/a.py", "server/gone.py", "frontend/src/a.ts", "frontend/src/gone.tsx"}
    ts = make_delta(["frontend/src/a.ts"])
    assert gone_documents(live, ts, "typescript") == ["frontend/src/gone.tsx"]
    py = make_delta(["server/a.py"])
    assert gone_documents(live, py, "python") == ["server/gone.py"]
    assert gone_documents(live, py, "scip-python") == ["server/gone.py"]

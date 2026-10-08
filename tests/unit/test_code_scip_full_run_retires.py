"""A full compiler-tier run retires the documents the new
index no longer has."""

from __future__ import annotations

from server.code.app.index.compiler_tier import gone_documents
from server.code.app.index.scip_delta import ScipDelta, ScipFile


def test_live_paths_the_delta_does_not_emit_are_deleted_files() -> None:
    delta = ScipDelta(
        commit="abc",
        files=[ScipFile(path="web/src/a.ts", deleted=False)],
        deleted_files=["web/src/old.ts"],
    )
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


def test_nothing_gone_when_every_live_path_is_emitted() -> None:
    delta = ScipDelta(commit="abc", files=[ScipFile(path="a.ts", deleted=False)])
    assert gone_documents({"a.ts"}, delta, "typescript") == []


def test_a_full_run_never_retires_another_tiers_documents() -> None:
    """One namespace, two tiers (Python primary, TypeScript beside it): compiler rows
    carry no producer column, so each full run retires only paths its own producer
    could have emitted."""
    live = {"server/a.py", "server/gone.py", "frontend/src/a.ts", "frontend/src/gone.tsx"}
    ts = ScipDelta(commit="abc", files=[ScipFile(path="frontend/src/a.ts", deleted=False)])
    assert gone_documents(live, ts, "typescript") == ["frontend/src/gone.tsx"]
    py = ScipDelta(commit="abc", files=[ScipFile(path="server/a.py", deleted=False)])
    assert gone_documents(live, py, "python") == ["server/gone.py"]
    assert gone_documents(live, py, "scip-python") == ["server/gone.py"]

"""A fan-out Loom expanded skips the branches the caller cannot
read and says so; a list the caller named still fails closed.

A release branch in the registry that the caller has not joined must not turn
every `code.backport_gap {code-clickhouse, pr_number}` answer into
AUTH_INSUFFICIENT by authorizing every configured branch at once."""

from __future__ import annotations

import pytest

from server.code.app import backport as code_backport
from server.code.app import search_fanout as fanout


def _patch_readable(monkeypatch: pytest.MonkeyPatch, readable: set[str]) -> None:
    async def row_in_scope(org: str, ns: str, role: object, *, hides_answer=True) -> bool:
        return ns in readable

    import server.auth.authz as authz

    monkeypatch.setattr(authz, "row_in_scope", row_in_scope)


@pytest.mark.asyncio
async def test_readable_subset_splits_and_discloses(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_readable(monkeypatch, {"code-clickhouse-26-8"})
    readable, skipped = await fanout.readable_subset(
        "clickhouse", ["code-clickhouse-26-8", "code-clickhouse-26-9"]
    )
    assert readable == ["code-clickhouse-26-8"]
    assert skipped == [{"namespace": "code-clickhouse-26-9", "reason": "not_readable"}]


@pytest.mark.asyncio
async def test_an_implicit_expansion_skips_the_unreadable_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_readable(monkeypatch, {"code-clickhouse-26-8", "code-clickhouse-26-7"})
    branches = [
        ("code-clickhouse-26-9", "26.9"),
        ("code-clickhouse-26-8", "26.8"),
        ("code-clickhouse-26-7", "26.7"),
    ]
    kept, skipped = await code_backport._implicit_branches_readable(
        "clickhouse", branches, explicit=False
    )
    assert [b for b, _ in kept] == ["code-clickhouse-26-8", "code-clickhouse-26-7"]
    assert skipped == [{"namespace": "code-clickhouse-26-9", "reason": "not_readable"}]


@pytest.mark.asyncio
async def test_named_branches_fail_closed_before_any_stage_reads_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stages that authorize a branch later run only on some paths (a fix with
    symbols, files, or `exact`); the fix stage reads every named branch's ledger first."""
    from server.core.errors import AuthInsufficientError

    _patch_readable(monkeypatch, {"code-clickhouse-26-8"})
    with pytest.raises(AuthInsufficientError):
        await code_backport._implicit_branches_readable(
            "clickhouse",
            [("code-clickhouse-26-8", "26.8"), ("code-clickhouse-26-9", "26.9")],
            explicit=True,
        )
    kept, skipped = await code_backport._implicit_branches_readable(
        "clickhouse", [("code-clickhouse-26-8", "26.8")], explicit=True
    )
    assert kept == [("code-clickhouse-26-8", "26.8")] and skipped == []


def test_both_outputs_carry_the_disclosure() -> None:
    from server.code.api.models_history import CodeBackportGapOutput, CodeBackportGapsOutput

    assert "skipped_branches" in CodeBackportGapOutput.model_fields
    assert "skipped_branches" in CodeBackportGapsOutput.model_fields


@pytest.mark.asyncio
async def test_init_registers_the_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from server.cli import code_maintenance
    from server.code.app.index import registry

    seen: list[str] = []

    async def ensure(cfg: object, **_: object) -> bool:
        seen.append(cfg.namespace)
        return True

    monkeypatch.setattr(registry, "ensure_registered", ensure)
    await code_maintenance._register_after_init(
        SimpleNamespace(org="clickhouse", namespace="code-clickhouse-26-9")
    )
    assert seen == ["code-clickhouse-26-9"]

    async def boom(cfg: object, **_: object) -> bool:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(registry, "ensure_registered", boom)
    await code_maintenance._register_after_init(
        SimpleNamespace(org="clickhouse", namespace="x")
    )  # fail-quiet

"""One statement per table for a listing.

`code.repos` reads every cold namespace's `code_index_state` in one `IN` over
the (org, namespace) pairs and hands each status computation its state, shared
counts row included, instead of one statement per configured namespace.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.code.store import index as cr
from server.code.store.index import status as code_status


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sqls: list[str] = []
        self.params: list[Any] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        self.params.append(params)
        return list(self.rows)

    async def query_one(self, sql: str, params: Any = None) -> Any:
        self.sqls.append(sql)
        self.params.append(params)
        return None


@pytest.mark.asyncio
async def test_all_state_many_reads_every_namespace_in_one_statement() -> None:
    ch = _CH(
        [
            {
                "org": "o",
                "namespace": "a",
                "state_key": "git_head",
                "state_value": "aaa",
                "detail": "",
                "updated_at": "t",
            },
            {
                "org": "o",
                "namespace": "a",
                "state_key": "table_counts",
                "state_value": json.dumps({"code_files": 3}),
                "detail": "aaa||",
                "updated_at": "t",
            },
            {
                "org": "o",
                "namespace": "b",
                "state_key": "git_head",
                "state_value": "bbb",
                "detail": "",
                "updated_at": "t",
            },
        ]
    )
    repo = cr.CodeRepo(ch)  # type: ignore[arg-type]
    out = await repo.all_state_many([("o", "a"), ("o", "b"), ("o", "empty")])
    assert len(ch.sqls) == 1
    assert "(org, namespace) IN {pairs:Array(Tuple(String, String))}" in ch.sqls[0]
    assert ch.params[0] == {"pairs": [("o", "a"), ("o", "b"), ("o", "empty")]}
    assert out[("o", "a")]["git_head"]["value"] == "aaa"
    assert out[("o", "a")]["table_counts"]["detail"] == "aaa||"
    assert out[("o", "b")] == {"git_head": {"value": "bbb", "detail": "", "updated_at": "t"}}
    assert out[("o", "empty")] == {}  # a namespace with no rows is present, empty


@pytest.mark.asyncio
async def test_all_state_many_with_no_pairs_issues_nothing() -> None:
    ch = _CH([])
    assert await cr.CodeRepo(ch).all_state_many([]) == {}  # type: ignore[arg-type]
    assert ch.sqls == []


@pytest.mark.asyncio
async def test_table_counts_with_a_prefetched_shared_row_reads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH([])
    repo = cr.CodeRepo(ch)  # type: ignore[arg-type]
    code_status._COUNTS_CACHE.clear()
    counts = await repo.table_counts(
        "o",
        "a",
        fingerprint="aaa||",
        max_age_s=600.0,
        shared_state={"value": json.dumps({"code_files": 3, "code_symbols": 9}), "detail": "aaa||"},
    )
    assert counts == {"code_files": 3, "code_symbols": 9}
    assert ch.sqls == [], "the shared row came with the state; nothing was read"


@pytest.mark.asyncio
async def test_code_repos_hands_each_status_its_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing reads the states once and each status computation gets its
    own namespace's, so no per-namespace state read follows."""
    from server.code.app import index_status

    index_status.reset_repo_status_cache()
    seen: list[tuple[str, str, dict[str, Any] | None]] = []
    calls: list[list[tuple[str, str]]] = []

    class _Cfg:
        def __init__(self, ns: str) -> None:
            self.org, self.namespace, self.repo, self.git_dir, self.git_branch = (
                "o",
                ns,
                f"x/{ns}",
                "/tmp",
                "main",
            )

    async def fake_all_state_many(
        self: Any, pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        calls.append(list(pairs))
        return {p: {"git_head": {"value": p[1], "detail": "", "updated_at": "t"}} for p in pairs}

    async def fake_repo_status(cfg: Any, *, state: dict[str, Any] | None = None) -> Any:
        seen.append((cfg.org, cfg.namespace, state))
        from server.code.api.models_status import CodeIndexStatusOutput

        return CodeIndexStatusOutput(
            org=cfg.org, namespace=cfg.namespace, repo=cfg.repo, branch=cfg.git_branch
        )

    monkeypatch.setattr(cr.CodeRepo, "all_state_many", fake_all_state_many)
    monkeypatch.setattr(index_status, "_repo_status", fake_repo_status)
    cfgs = [_Cfg("a"), _Cfg("b")]
    states = await cr.CodeRepo().all_state_many([(c.org, c.namespace) for c in cfgs])
    for c in cfgs:
        await index_status._repo_status_cached(c, state=states.get((c.org, c.namespace)))
    assert calls == [[("o", "a"), ("o", "b")]]
    assert [(o, n, (st or {}).get("git_head", {}).get("value")) for o, n, st in seen] == [
        ("o", "a", "a"),
        ("o", "b", "b"),
    ]

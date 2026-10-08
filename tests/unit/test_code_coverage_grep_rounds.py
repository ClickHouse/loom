"""The coverage claim's test-tree greps are one statement per
claim, and the positive control is one read per namespace.

`grep_files_many` groups by (pattern, path) under the OR of the round's
predicates and pages in memory; `_grep_tests_many` assembles each request the
way `_grep_tests` does; `claim_tests` answers identically whether the
repository has the round-wide read or only `grep_files`; `positive_control`
re-reads a namespace only after its TTL.
"""

from __future__ import annotations

from typing import Any

import pytest

import server.code.app.coverage as h
from server.code.domain.index import coverage_claim as cc
from server.code.store.index import CodeRepo

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

ORG, NS = "clickhouse", "code-x"
ST, IT = cc.STATELESS, cc.INTEGRATION

# The corpus: (path, content) chunks; `first_line` = 10 * chunk ordinal.
CHUNKS = [
    (f"{ST}00001_a.sql", "select arrayJoin(x) from parseQuotaKey"),
    (f"{ST}00001_a.sql", "-- second chunk names parseQuotaKey again"),
    (f"{ST}00002_b.sql", "select arrayJoin([1])"),
    (f"{ST}00003_c.sh", "clickhouse-client -q 'select QuotaCache::parseQuotaKey'"),
    (f"{IT}test_quota/test.py", "def test_parse(): parseQuotaKey()"),
    (f"{IT}test_x/test.py", "arrayJoin"),
    ("src/Access/QuotaCache.cpp", "parseQuotaKey definition, not a test"),
]


class _CH:
    """A ClickHouse stub that answers the two grep shapes by evaluating the
    predicates on CHUNKS, so the per-pattern and the round-wide read are
    compared on the same rows."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.statements.append(" ".join(sql.split()))
        if "ARRAY JOIN" in sql:
            pats = params["pats"]
            prefixes = [v for k, v in params.items() if k.startswith("pre_")]
            groups: dict[tuple[int, str], list[int]] = {}
            for ordinal, (path, content) in enumerate(CHUNKS):
                if not any(path.startswith(p) for p in prefixes):
                    continue
                for i, pat in enumerate(pats, start=1):
                    if pat in content:
                        groups.setdefault((i, path), []).append(ordinal * 10)
            return [
                {"pi": i, "path": path, "chunks": len(lines), "first_line": min(lines)}
                for (i, path), lines in sorted(groups.items())
            ]
        pat, prefix = params["pat"], params["prefix"]
        per_path: dict[str, list[int]] = {}
        for ordinal, (path, content) in enumerate(CHUNKS):
            if pat in content and (not prefix or path.startswith(prefix)):
                per_path.setdefault(path, []).append(ordinal * 10)
        # The page carries its distinct-path total as a window
        rows = [
            {"path": p, "chunks": len(ls), "first_line": min(ls), "total": len(per_path)}
            for p, ls in sorted(per_path.items())
        ]
        return rows[: int(params["limit"])]

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        self.statements.append(" ".join(sql.split()))
        pat, prefix = params["pat"], params["prefix"]
        paths = {
            path
            for path, content in CHUNKS
            if pat in content and (not prefix or path.startswith(prefix))
        }
        return {"n": len(paths)}


@pytest.mark.asyncio
async def test_grep_files_many_returns_grep_files_rows_per_pattern_and_prefix() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    pats = ["parseQuotaKey", "arrayJoin", "nothing_here"]
    prefixes = list(cc.TEST_PREFIXES)
    many = await repo.grep_files_many(ORG, NS, pats, path_prefixes=prefixes, limit=1)
    assert len(ch.statements) == 1
    batched = ch.statements[0]
    assert "ARRAY JOIN" in batched
    # one statement matches every pattern: the guard indexes the pattern array
    assert "position(content, {pats:Array(String)}[i]) > 0" in batched
    for pat in pats:
        for pre in prefixes:
            ch.statements.clear()
            files, total = await repo.grep_files(ORG, NS, pat, path_prefix=pre, limit=1)
            assert len(ch.statements) == 1  # the page carries its total
            assert many[(pat, pre)] == (files, total), (pat, pre)
    # the page is the first paths in path order; the total counts every group
    assert many[("parseQuotaKey", ST)] == (
        [{"path": f"{ST}00001_a.sql", "chunks": 2, "first_line": 0}],
        2,
    )
    assert many[("nothing_here", IT)] == ([], 0)


@pytest.mark.asyncio
async def test_grep_files_many_guard_is_the_or_of_the_patterns_and_batches_by_32() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.grep_files_many(ORG, NS, ["parseQuotaKey", "arrayJoin"], path_prefixes=[ST], limit=5)
    sql = ch.statements[0]
    assert "lower(content) LIKE {gm_0:String} OR lower(content) LIKE {gm_1:String}" in sql
    assert "startsWith(path, {pre_0:String})" in sql and "PREWHERE" not in sql
    assert "query_plan_direct_read_from_text_index = 0" in sql
    ch.statements.clear()
    await repo.grep_files_many(
        ORG, NS, [f"pat{i:02d}" for i in range(40)], path_prefixes=[ST], limit=5
    )
    assert len(ch.statements) == 2
    # a pattern too short for an n-gram drops the guard for its statement; the exact matcher stays
    ch.statements.clear()
    await repo.grep_files_many(ORG, NS, ["id", "parseQuotaKey"], path_prefixes=[ST], limit=5)
    assert "LIKE" not in ch.statements[0] and "position(content" in ch.statements[0]


class _Repo:
    """The coverage-claim tests' stub, with the round-wide read on top when asked."""

    def __init__(self, *, many: bool) -> None:
        self.calls: list[str] = []
        self._ch = _CH()
        self._real = CodeRepo(self._ch)  # type: ignore[arg-type]
        if not many:
            self.grep_files_many = None  # type: ignore[method-assign]

    async def grep_files(
        self,
        org,
        ns,
        pattern,
        *,
        regex=False,
        case_sensitive=True,
        path_prefix="",
        limit=200,
        scope=None,
    ):
        self.calls.append(f"one:{pattern}:{path_prefix}")
        return await self._real.grep_files(
            org,
            ns,
            pattern,
            regex=regex,
            case_sensitive=case_sensitive,
            path_prefix=path_prefix,
            limit=limit,
        )

    async def grep_files_many(self, org, ns, patterns, *, path_prefixes, limit):
        self.calls.append(f"many:{','.join(patterns)}:{limit}")
        return await self._real.grep_files_many(
            org, ns, patterns, path_prefixes=path_prefixes, limit=limit
        )

    async def coverage_rows_for(self, org, ns, paths, *, limit=2000):
        return []

    async def coverage_files_for(self, org, ns, paths):
        return {}

    async def test_map_detail(self, org, ns, paths, *, limit=60):
        return [
            {
                "src_path": "src/Access/QuotaCache.cpp",
                "test_path": f"{ST}01234_quota.sql",
                "method": "content_reference",
                "confidence": 0.6,
                "evidence": "parseQuotaKey",
            }
        ]


@pytest.mark.asyncio
async def test_grep_tests_many_is_one_statement_per_limit_and_equals_grep_tests() -> None:
    batched, per = _Repo(many=True), _Repo(many=False)
    reqs = [("parseQuotaKey", 15), ("QuotaCache::parseQuotaKey", 15), ("arrayJoin", 3)]
    got = await h._grep_tests_many(batched, ORG, NS, reqs)
    want = await h._grep_tests_many(per, ORG, NS, reqs)
    assert got == want
    assert [c.split(":")[0] for c in batched.calls] == ["many", "many"]  # limits 15 and 3
    assert len(per.calls) == 9 and all(c.startswith("one:") for c in per.calls)
    assert batched._ch.statements and len(batched._ch.statements) == 2
    assert len(per._ch.statements) == 9  # one page-with-total per (pattern, tree)
    files, total = got[0]
    assert [f["path"] for f in files] == [
        f"{ST}00001_a.sql",
        f"{ST}00003_c.sh",
        f"{IT}test_quota/test.py",
    ]
    assert total == 3
    assert await h._grep_tests_many(batched, ORG, NS, []) == []


@pytest.mark.asyncio
async def test_claim_tests_rows_are_identical_with_and_without_the_round_read() -> None:
    batched, per = _Repo(many=True), _Repo(many=False)
    terms = {"parseQuotaKey": cc.SHORT_NAME_GREP, "QuotaCache::parseQuotaKey": cc.EXACT_SCOPED}
    a = await h.claim_tests(
        batched,
        ORG,
        NS,
        "symbol",
        "DB::QuotaCache::parseQuotaKey",
        ["src/Access/QuotaCache.cpp"],
        limit=5,
        terms=dict(terms),
    )
    b = await h.claim_tests(
        per,
        ORG,
        NS,
        "symbol",
        "DB::QuotaCache::parseQuotaKey",
        ["src/Access/QuotaCache.cpp"],
        limit=5,
        terms=dict(terms),
    )
    assert a == b
    ordered, total = a
    assert total == 3
    by_path = {r["test_path"]: r for r in ordered}
    assert set(by_path) == {
        f"{ST}00001_a.sql",
        f"{ST}00003_c.sh",
        f"{ST}01234_quota.sql",
        f"{IT}test_quota/test.py",
    }
    assert by_path[f"{ST}00003_c.sh"]["tier"] == cc.EXACT_SCOPED
    assert by_path[f"{ST}00001_a.sql"]["evidence"] == "names parseQuotaKey at line 0"
    assert len(batched._ch.statements) == 1 and len(per._ch.statements) == 6


@pytest.mark.asyncio
async def test_positive_control_is_read_once_per_namespace_within_the_ttl(monkeypatch) -> None:
    h.reset_control_cache()
    repo = _Repo(many=False)
    assert await h.positive_control(repo, ORG, NS) == (True, "arrayJoin")
    assert await h.positive_control(repo, ORG, NS) == (True, "arrayJoin")
    assert await h.positive_control(repo, ORG, "code-y") == (True, "arrayJoin")
    assert len(repo.calls) == 6  # three trees once per namespace, not per call
    # the clock moves past the TTL: one more read
    real = h.time.monotonic
    monkeypatch.setattr(h.time, "monotonic", lambda: real() + h._CONTROL_TTL_S + 1)
    assert await h.positive_control(repo, ORG, NS) == (True, "arrayJoin")
    assert len(repo.calls) == 9
    # a failed read is not kept
    h.reset_control_cache()

    class _Broken(_Repo):
        async def grep_files(self, *a, **kw):
            raise RuntimeError("down")

    broken = _Broken(many=False)
    assert await h.positive_control(broken, ORG, NS) == (False, "arrayJoin")
    assert await h.positive_control(repo, ORG, NS) == (True, "arrayJoin")
    h.reset_control_cache()

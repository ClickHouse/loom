"""A bulk read and a history read locate under FINAL
and fetch without it; a single-row read stays one statement.

A point read's cost is the value column of the granule that holds the row,
whatever the statement's shape (measured: located first or not, FINAL or not,
one row costs one granule — the tables read 128-row granules). A bulk read
of N keys and a history read of N versions are different: the FINAL statement
that finds the heads costs the key columns only, and the rows are then read
by their exact (memory_id, version) pairs without FINAL, which reads the
value column for the matching rows alone (20 keys: 84 MB → 163 kB on a copy
of the table).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.store import Identity

_IDENT = Identity(org="o", namespace="n", agent="a", user_id="u", session_id="s")


def _located(key: str = "k", version: int = 7, mid: str = "", agent: str = "a") -> dict[str, Any]:
    return {
        "memory_id": mid or "00000000-0000-0000-0000-000000000001",
        "version": version,
        "agent": agent,
        "user_id": "u",
        "session_id": "s",
        "memory_key": key,
        "created_at": "2026-09-19 00:00:00",
    }


def _full(key: str = "k", version: int = 7, mid: str = "", agent: str = "a") -> dict[str, Any]:
    return {
        **_located(key, version, mid, agent),
        "org": "o",
        "namespace": "n",
        "memory_type": "semantic",
        "value": '{"text": "v"}',
        "value_json": "",
        "confidence": 1.0,
        "pinned": False,
        "created_at": "2026-09-19 00:00:00",
        "updated_at": None,
        "expires_at": None,
        "status": "active",
        "run_id": "-",
        "source_trace": "",
        "char_limit": 0,
        "tags": [],
        "entities": [],
        "content_sha256": "",
        "verified_by": None,
        "verified_at": None,
        "verified_sha256": None,
        "actor": "human:x",
        "source_class": "asserted",
    }


class _CH:
    """Answers the locating statement with key columns and the fetch with the
    row, recording every statement it saw."""

    def __init__(self, located: list[dict[str, Any]], full: list[dict[str, Any]]) -> None:
        self.sqls: list[str] = []
        self.params: list[dict[str, Any] | None] = []
        self.located = located
        self.full = full

    def _answer(self, sql: str) -> list[dict[str, Any]]:
        return (
            self.located
            if "FROM loom.memory FINAL" in sql
            or "memory_versions" in sql
            and "value" not in _select_list(sql)
            else self.full
        )

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sqls.append(sql)
        self.params.append(params)
        rows = self._answer(sql)
        return rows[0] if rows else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.sqls.append(sql)
        self.params.append(params)
        return list(self._answer(sql))


def _select_list(sql: str) -> str:
    """The outermost SELECT list of a statement."""
    m = re.search(r"SELECT\s+(.*?)\s+FROM", sql, re.S)
    assert m, sql
    return m.group(1)


def _selects_value(sql: str) -> bool:
    return re.search(r"\bvalue\b", _select_list(sql)) is not None


_MA = "00000000-0000-0000-0000-00000000000a"
_MB = "00000000-0000-0000-0000-00000000000b"


@pytest.mark.asyncio
async def test_bulk_get_fetches_the_located_identity_not_a_twin_at_the_same_version() -> None:
    """The entity pass stamps one version on every row of its batch, so agent
    b's row of key k can share agent a's (memory_key, version). The fetch
    names memory_id: agent a's bulk read returns agent a's row."""

    class _Pairs(_CH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
            rows = await super().query_all(sql, params)
            if "FROM loom.memory FINAL" in sql:
                return rows
            wanted = set((params or {}).get("pairs") or [])
            return [r for r in rows if (r["memory_id"], r["version"]) in wanted]

    ch = _Pairs([_located("k", 7, _MA)], [_full("k", 7, _MB, "b"), _full("k", 7, _MA)])
    rows = await memory_repo.get_many_by_keys(_IDENT, ["k"], ch=ch, scope=WHOLE_NAMESPACE)
    assert [(r.memory_id, r.agent) for r in rows] == [(_MA, "a")]


@pytest.mark.asyncio
async def test_bulk_get_locates_then_fetches_pairs_in_the_located_order() -> None:
    ch = _CH(
        [_located("b", 9, _MB), _located("a", 3, _MA)], [_full("a", 3, _MA), _full("b", 9, _MB)]
    )
    rows = await memory_repo.get_many_by_keys(_IDENT, ["b", "a"], ch=ch, scope=WHOLE_NAMESPACE)
    assert [r.memory_key for r in rows] == ["b", "a"]
    locate, fetch = ch.sqls
    assert "FROM loom.memory FINAL" in locate and not _selects_value(locate)
    assert (
        "FINAL" not in fetch
        and "(memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}" in fetch
    )
    assert ch.params[1] is not None and ch.params[1]["pairs"] == [(_MB, 9), (_MA, 3)]


@pytest.mark.asyncio
async def test_bulk_get_empty_locate_issues_no_fetch() -> None:
    ch = _CH([], [])
    assert await memory_repo.get_many_by_keys(_IDENT, ["x"], ch=ch, scope=WHOLE_NAMESPACE) == []
    assert all("FINAL" in sql for sql in ch.sqls)  # the locate and its consistent retry only


@pytest.mark.asyncio
async def test_history_reads_the_version_list_then_the_listed_values() -> None:
    class _Hist(_CH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
            self.sqls.append(sql)
            self.params.append(params)
            if _selects_value(sql):
                return [
                    {"memory_id": "m", "version": 7, "value": "v7"},
                    {"memory_id": "m", "version": 5, "value": "v5"},
                ]
            return [
                {"memory_id": "m", "version": 7, "confidence": 1.0, "created_at": "t7"},
                {"memory_id": "m", "version": 5, "confidence": 1.0, "created_at": "t5"},
            ]

    ch = _Hist([], [])
    rows = await memory_repo.list_history(_IDENT, "k", limit=8, ch=ch, scope=WHOLE_NAMESPACE)
    assert [(r.version, r.value) for r in rows] == [(7, "v7"), (5, "v5")]
    listing, values = ch.sqls
    assert (
        not _selects_value(listing)
        and "memory_versions" in listing
        and "LIMIT {limit:UInt32}" in listing
    )
    assert (
        "version IN {vs:Array(UInt64)}" in values
        and ch.params[1] is not None
        and ch.params[1]["vs"] == [7, 5]
    )
    assert "FINAL" not in values


@pytest.mark.asyncio
async def test_a_client_answering_full_rows_short_circuits_the_fetch() -> None:
    """Older fakes and a future combined read answer the locate with the rows;
    the repository then hydrates them and issues nothing more."""
    ch = _CH([_full()], [])
    rows = await memory_repo.get_many_by_keys(_IDENT, ["k"], ch=ch, scope=WHOLE_NAMESPACE)
    assert len(rows) == 1 and rows[0].value == {"text": "v"}
    assert len(ch.sqls) == 1


@pytest.mark.asyncio
async def test_get_by_key_stays_one_statement() -> None:
    """One row is one granule whatever the shape; the hottest read keeps its
    single FINAL statement (the PREWHERE test covers its form)."""
    ch = _CH([_full()], [])
    row = await memory_repo.get_by_key(_IDENT, "k", ch=ch, scope=WHOLE_NAMESPACE)
    assert row is not None and len(ch.sqls) == 1 and "FROM loom.memory FINAL" in ch.sqls[0]


def test_bulk_and_history_statements_never_select_value_under_final() -> None:
    """The statements themselves: the bulk read's FINAL statement selects the
    key columns, its fetch has no FINAL; the history listing has no value."""
    from server.memory.store import history, reads

    text = open(reads.__file__, encoding="utf-8").read()
    start = text.index("async def get_many_by_keys(")
    body = text[start : text.index("\nasync def ", start + 1)]
    assert "_LOCATE_COLUMNS" in body and "_MEMORY_FINAL_PROJECTION" not in body
    assert "FINAL" not in reads._BULK_FETCH_SQL
    text = open(history.__file__, encoding="utf-8").read()
    start = text.index("async def list_history(")
    body = text[start : text.index("\nasync def ", start + 1)]
    listing = body.split("if not rows")[0]
    assert re.search(r"cols = \"\"\"memory_id, version, confidence", listing), (
        "listing has no value column"
    )
    assert "FINAL" not in body


def test_memory_and_its_versions_read_128_row_granules() -> None:
    from tests.unit.schema_ddl import ddl

    for table in ("memory", "memory_versions"):
        settings = ddl(table).rsplit("\nSETTINGS ", 1)[1]
        assert settings.startswith("index_granularity = 128"), (table, settings)


@pytest.mark.asyncio
async def test_a_wildcard_agent_reads_the_keys_history_namespace_wide() -> None:
    """Agent "*" drops the writer predicates, and two writers'
    rows of one key keep their own values (keyed on memory_id + version)."""

    class _Hist(_CH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
            self.sqls.append(sql)
            self.params.append(params)
            if _selects_value(sql):
                return [
                    {"memory_id": "cc", "version": 7, "value": "from claude-code"},
                    {"memory_id": "cx", "version": 7, "value": "from codex"},
                ]
            return [
                {"memory_id": "cc", "version": 7, "agent": "claude-code", "created_at": "t"},
                {"memory_id": "cx", "version": 7, "agent": "codex", "created_at": "t"},
            ]

    wild = Identity(org="o", namespace="n", agent="*", user_id="-", session_id="-")
    ch = _Hist([], [])
    rows = await memory_repo.list_history(wild, "k", ch=ch, scope=WHOLE_NAMESPACE)
    assert [(r.agent, r.value) for r in rows] == [
        ("claude-code", "from claude-code"),
        ("codex", "from codex"),
    ]
    assert all("agent = {a:String}" not in sql for sql in ch.sqls)
    ch = _Hist([], [])
    await memory_repo.list_history(_IDENT, "k", ch=ch, scope=WHOLE_NAMESPACE)
    assert all("agent = {a:String}" in sql for sql in ch.sqls)


def _selected(sql: str) -> set[str]:
    """The column names (or aliases) a statement's SELECT list returns."""
    select = re.sub(r"--[^\n]*", "", sql.split("SELECT", 1)[1].split("FROM loom.memory", 1)[0])
    while "(" in select:
        select = re.sub(r"\([^()]*\)", "", select)
    return {re.findall(r"\w+", part)[-1] for part in select.split(",")}


@pytest.mark.asyncio
async def test_bulk_get_answers_what_memory_get_answers_for_the_same_row() -> None:
    # A column the plain fetch leaves out reads as its default: an empty `sensitivity`
    # let export-okf publish high rows. Every column the answer reads is off its default.
    from server.memory.app.get_output import row_output

    row = {
        **_full(),
        "pinned": True,
        "status": "duplicate",
        "expires_at": "2099-06-01 00:00:00",
        "on_expire": "tombstone",
        "expire_reason": "rotated",
        "updated_at": "2026-09-20 00:00:00",
        "valid_at": "2026-09-19 00:00:00",
        "valid_to": "2027-01-01 00:00:00",
        "temporal_anchor": "2026-09-18 00:00:00",
        "temporal_confidence": 0.5,
        "char_limit": 100,
        "tags": ["t"],
        "entities": ["e"],
        "duplicate_of": "k-canon",
        "superseded_by": "k-new",
        "recall_count": 3,
        "last_recalled_at": "2026-09-21 00:00:00",
        "verified_by": "human:x",
        "verified_at": "2026-09-20 00:00:00",
        "verified_sha256": "v" * 64,
        "sensitivity": "high",
        "source_class": "derived",
        "source_trace": "conversation:t1",
        "source_resource_id": "doc-1",
        "lock_mode": "no_delete",
        "lock_reason": "audit",
        "lock_until": "2099-01-01 00:00:00",
        "locked_by": "human:y",
        "locked_at": "2026-09-20 00:00:00",
    }

    class _Columns(_CH):
        """Answers a fetch with only the columns its SELECT list names, as ClickHouse does."""

        def _answer(self, sql: str) -> list[dict[str, Any]]:
            rows = super()._answer(sql)
            if rows is self.located:
                return rows
            return [{k: v for k, v in r.items() if k in _selected(sql)} for r in rows]

    answers = []
    for strong in (False, True):
        ch = _Columns([_located()], [row])
        got = await memory_repo.get_many_by_keys(
            _IDENT, ["k"], ch=ch, scope=WHOLE_NAMESPACE, strong=strong
        )
        r = got[0]
        out = row_output(
            r,
            sha="s",
            canonical_key=None,
            duplicate_of=r.duplicate_of,
            superseded_by=r.superseded_by,
        )
        answers.append(out.model_dump())
    plain, whole = answers
    assert plain["sensitivity"] == "high" and plain["valid_to"] is not None
    assert plain["duplicate_of"] == "k-canon" and plain["lock_mode"] == "no_delete"
    # A read-then-write (the dashboard's pin) sends these back to keep them.
    assert plain["source_trace"] == "conversation:t1" and plain["source_resource_id"] == "doc-1"
    assert plain == whole

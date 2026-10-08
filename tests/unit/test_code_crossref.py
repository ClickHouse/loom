"""crossref pass: registration regexes, df-damped confidence, row shaping."""

from __future__ import annotations

import re

import pytest

from server.code.domain.extract import crossref as x


def test_registration_regex_matches_real_shapes() -> None:
    reg = re.compile(x._REGISTRATION_RE)
    samples = {
        'factory.registerFunction<FunctionArrayJoin>("arrayJoin");': "arrayJoin",
        'factory.registerFunction("least", ...);': "least",
        'factory.registerAlias("visitParamHas", "simpleJSONHas");': "visitParamHas",
        'factory.registerStorage("Log", create);': "Log",
        'registerTableFunction("remote", ...)': "remote",
    }
    for text, want in samples.items():
        m = reg.search(text)
        assert m and m.group(1) == want, text
    # a registration without a string literal is not a whitelist entry FOR
    # THIS regex — the name-constant leg covers it (the dominant modern
    # form registers as registerFunction<Impl>(documentation)).
    assert reg.search("factory.registerFunction<FunctionFoo>(documentation);") is None
    nc = re.compile(x._NAME_CONST_RE)
    assert nc.search('static constexpr auto name = "arrayJoin";').group(1) == "arrayJoin"
    assert (
        nc.search('struct NameLeast { static constexpr auto name = "least"; };').group(1) == "least"
    )


def test_settings_regexes() -> None:
    assert (
        re.search(x._SETTINGS_DECL_RE, "DECLARE(UInt64, max_threads, 0, ...)").group(1)
        == "max_threads"
    )
    assert (
        re.search(
            x._SETTINGS_ALIAS_RE,
            "DECLARE_WITH_ALIAS(Bool, allow_experimental_analyzer, true, d, 0, enable_analyzer)",
        ).group(1)
        == "enable_analyzer"
    )


def test_sql_token_regexes() -> None:
    assert re.findall(x._SQL_CALL_RE, "SELECT arrayJoin([1]), count() FROM t") == [
        "arrayJoin",
        "count",
    ]
    assert re.findall(
        x._SQL_SETTING_RE, "SET max_threads = 4; SELECT 1 SETTINGS enable_analyzer=1"
    ) == [
        "max_threads",
        "enable_analyzer",
    ]


class _FakeCH:
    """Returns the whitelist rows then the use rows; records inserts."""

    def __init__(self, reg_rows, use_rows):
        self._responses = [reg_rows, use_rows]
        self.inserted: list[dict] = []

    async def query_all(self, sql, params=None):
        return self._responses.pop(0)

    async def insert(self, table, rows):
        self.inserted.extend(rows)


class _FakeRepo:
    def __init__(self, ch):
        self._ch = ch

    async def insert_rows(self, table, rows):
        await self._ch.insert(table, rows)


@pytest.mark.asyncio
async def test_pass_writes_damped_rows() -> None:
    reg = [
        {"name": "arrayJoin", "path": "src/Functions/array/arrayJoin.cpp"},
        {"name": "max_threads", "path": "src/Core/Settings.cpp"},
        {"name": "toString", "path": "src/Functions/toString.cpp"},
    ]
    uses = [
        {"test_path": "tests/queries/0_stateless/a.sql", "names": ["arrayJoin", "toString"]},
        {"test_path": "tests/queries/0_stateless/b.sql", "names": ["toString", "max_threads"]},
    ]
    ch = _FakeCH(reg, uses)
    res = await x.run_crossref_pass(_FakeRepo(ch), "o", "n")
    assert res.registrations == 3 and res.test_files_linked == 2
    rows = {(r["src_path"], r["test_path"]): r for r in ch.inserted}
    a = rows[("src/Functions/array/arrayJoin.cpp", "tests/queries/0_stateless/a.sql")]
    assert a["method"] == "content_reference" and a["evidence"] == "arrayJoin"
    # df=1 -> 0.9; toString appears in both files (df=2) -> damped below 0.9
    assert a["confidence"] == 0.9
    t = rows[("src/Functions/toString.cpp", "tests/queries/0_stateless/b.sql")]
    assert t["confidence"] < 0.9


@pytest.mark.asyncio
async def test_empty_whitelist_is_a_noop() -> None:
    ch = _FakeCH([], [])
    res = await x.run_crossref_pass(_FakeRepo(ch), "o", "n")
    assert res.rows_written == 0 and not ch.inserted

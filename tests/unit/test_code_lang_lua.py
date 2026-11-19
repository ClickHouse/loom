"""Lua functions are symbols, `f(…)` / `a.b(…)` are call sites bound by
`lane_units.bind_sites`, and `require("a.b")` is an include of `a/b.lua` or
`a/b/init.lua`."""

from __future__ import annotations

import time

import pytest

from server.code.domain.extract.lane_units import bind_sites, span_chunks
from server.code.domain.extract.lang_detect import detect_by_extension
from server.code.domain.extract.lang_lua import (
    CHUNK_OPTIONS,
    SITE_RULES,
    parse_lua,
    reference_sites,
    resolve_imports,
    wanted_paths,
)
from server.code.domain.extract.lang_text import text_lang_for_path
from tests.unit.code_fixtures import assert_linear

UTIL = b"""-- string helpers
local M = {}

local function trim(s)
  return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

function M.split(s, sep)
  local out = {}
  for part in string.gmatch(s, "[^" .. sep .. "]+") do
    table.insert(out, trim(part))
  end
  return out
end

M.join = function(parts, sep)
  return table.concat(parts, sep)
end

return M
"""

APP = b"""local util = require("lib.util")
local json = require "cjson"

--[[ commented out:
function old() end
]]

local App = {}

function App:new(o)
  o = setmetatable(o or {}, self)
  self.__index = self
  return o
end

function App.run(line)
  local s = "end function ("
  if line then
    repeat
      line = line .. "!"
    until #line > 3
  end
  print(util.split(line, ","))
  return helper(line)
end

function helper(x)
  return util.join({x}, "-")
end

return App
"""


def test_functions_are_symbols_qualified_by_the_module_path() -> None:
    pf = parse_lua("lib/util.lua", UTIL)
    assert [(s.qualified_name, s.name, s.kind, s.start_line, s.end_line) for s in pf.symbols] == [
        ("lib.util::trim", "trim", "function", 4, 6),
        ("lib.util::M.split", "split", "function", 8, 14),
        ("lib.util::M.join", "join", "function", 16, 18),
    ]
    assert [s.signature for s in pf.symbols] == [
        "function trim(s)",
        "function M.split(s, sep)",
        "function M.join(parts, sep)",
    ]


def test_a_method_comments_and_strings_and_nested_blocks() -> None:
    pf = parse_lua("app/app.lua", APP)
    by_name = {s.name: s for s in pf.symbols}
    # `old` sits in a long comment; the string holding `end function (` closes nothing
    assert sorted(by_name) == ["helper", "new", "run"]
    assert by_name["new"].qualified_name == "app.app::App.new"
    assert by_name["new"].signature == "function App:new(o)"
    run = by_name["run"]
    assert (run.start_line, run.end_line) == (16, 25)  # the `if` / `repeat … until` close inside
    assert pf.includes == ["lib.util", "cjson"]


def test_calls_skip_builtins_libraries_and_method_calls() -> None:
    sites = reference_sites(parse_lua("lib/util.lua", UTIL))
    # `s:gsub` is a method call, `string.gmatch` / `table.insert` / `table.concat` library
    # calls: only the repository's own `trim` is a site
    assert [(n, k) for _l, n, k in sites] == [("trim", "call")]
    app_sites = reference_sites(parse_lua("app/app.lua", APP))
    # `setmetatable` and `print` are Lua's own; `require` is an include, not a call
    assert [n for _l, n, _k in app_sites] == ["split", "helper", "join"]


def test_call_sites_bind_same_file_first_then_repository_unique() -> None:
    util = parse_lua("lib/util.lua", UTIL)
    app = parse_lua("app/app.lua", APP)
    table: dict[str, list[tuple[str, str, str]]] = {}
    for s in [*util.symbols, *app.symbols]:
        table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    edges = bind_sites(app, reference_sites(app), table, SITE_RULES)
    assert {
        (e.from_qualified_name, e.to_qualified_name, e.edge_kind, e.confidence) for e in edges
    } == {
        ("app.app::App.run", "lib.util::M.split", "calls", 0.6),
        ("app.app::App.run", "app.app::helper", "calls", 1.0),
        ("app.app::helper", "lib.util::M.join", "calls", 0.6),
    }


def test_a_name_defined_twice_in_the_repository_binds_to_nothing() -> None:
    a = parse_lua("a.lua", b"function go() end\n")
    b = parse_lua("b.lua", b"function go() end\n")
    c = parse_lua("c.lua", b"function main()\n  go()\n  unknown()\nend\n")
    table: dict[str, list[tuple[str, str, str]]] = {}
    for s in [*a.symbols, *b.symbols, *c.symbols]:
        table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    assert bind_sites(c, reference_sites(c), table, SITE_RULES) == []


def test_requires_resolve_beside_the_file_then_up_to_the_root() -> None:
    assert wanted_paths("game/scenes/menu.lua", ["lib.util"]) == [
        "game/scenes/lib/util.lua",
        "game/scenes/lib/util/init.lua",
        "game/lib/util.lua",
        "game/lib/util/init.lua",
        "lib/util.lua",
        "lib/util/init.lua",
    ]
    present = {"lib/util/init.lua", "game/ui.lua"}
    assert resolve_imports("game/scenes/menu.lua", ["lib.util", "cjson", "ui"], present) == [
        "lib/util/init.lua",
        "game/ui.lua",
    ]
    # a slash path and a `..` escape
    assert wanted_paths("a.lua", ["x/y"]) == ["x/y.lua", "x/y/init.lua"]
    assert wanted_paths("a.lua", ["../etc/passwd"]) == []


def test_lua_is_a_lane_not_plain_text() -> None:
    assert detect_by_extension("scripts/build.lua") == "lua"
    assert text_lang_for_path("scripts/build.lua", b"print(1)\n") == ""


def test_chunks_follow_the_function_spans() -> None:
    pf = parse_lua("lib/util.lua", UTIL)
    chunks = span_chunks(pf, 6000, **CHUNK_OPTIONS)
    # one chunk per function, after the leading `local M = {}` header
    assert [c.kind for c in chunks].count("function") == 3


@pytest.mark.parametrize(
    "unit",
    [
        "function a.b(x) return c(x) end\n",
        "local function f(\n",
        "M.f = function(\n",
        "--[[ ",
        "[==[ ",
        "if a then ",
        'require("',
    ],
)
def test_a_file_of_many_units_parses_in_linear_time(unit: str) -> None:
    def cost(n: int) -> float:
        src = (unit * n).encode()
        t0 = time.perf_counter()
        pf = parse_lua("a.lua", src)
        reference_sites(pf)
        return time.perf_counter() - t0

    assert_linear(cost, 4_000, 16_000)

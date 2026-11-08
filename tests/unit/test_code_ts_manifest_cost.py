"""The TypeScript lane parses a repository's manifests and barrels with patterns that
must cost time linear in the text: a committed file of unclosed `/*` or of blank lines
took minutes per read under the regular expressions it replaced, and a `paths` table or
`exports` map is bounded and read once."""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

from server.code.app.index import ingest_resolve
from server.code.domain.extract import lang_typescript_imports as lti
from server.code.domain.extract import py_layouts, ts_config, ts_reexports, ts_workspace
from tests.unit.code_fixtures import grows_linearly

# The patterns the linear forms replace, kept here to prove the output is unchanged.
_OLD_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_OLD_YAML_ITEM_RE = re.compile(r"^\s*-\s*['\"]?([^'\"#\n]+?)['\"]?\s*(?:#.*)?$", re.M)
_OLD_REEXPORT_RE = re.compile(
    r"""^\s*export\s+(?:type\s+)?(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s+from\s+["']([^"']+)["']""",
    re.M,
)

# Each took over 100 s at 100,000 units before; linear, 300,000 take about 0.1 s.
_BOUND_S = 5.0
_N = 300_000


def _timed(fn, text: str) -> float:
    t0 = time.perf_counter()
    fn(text)
    return time.perf_counter() - t0


def _random_texts(alphabet: str, count: int = 3000, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))) for _ in range(count)]


def test_comment_stripping_matches_the_regular_expression() -> None:
    for text in _random_texts("/*\n a'\""):
        assert ts_config.strip_comments(text) == _OLD_COMMENT_RE.sub("", text), repr(text)


def test_the_line_anchored_patterns_match_what_they_matched() -> None:
    # Compared as `workspace_globs` reads them, stripped: an item of only whitespace,
    # which strips to an empty glob, is no longer read.
    for text in _random_texts("- \t\n\r'#ab"):
        old = [g.strip() for g in _OLD_YAML_ITEM_RE.findall(text) if g.strip()]
        new = [g.strip() for g in ts_workspace._YAML_ITEM_RE.findall(text) if g.strip()]
        assert new == old
    barrels = ["\n  export * from './a'\n\n\texport {b} from \"./b\"\n"]
    for space in _random_texts(" \t\n", count=200) + barrels:
        text = space + "export * from './x'\n" + space + "export * from './y'" + space
        assert lti.reexport_specs(text) == list(dict.fromkeys(_OLD_REEXPORT_RE.findall(text)))


def test_unclosed_comment_openers_cost_linear_time() -> None:
    text = "/*x" * _N
    assert _timed(ts_config.parse_jsonc, text) < _BOUND_S
    assert _timed(lambda t: ts_workspace.extra_manifest_globs(t, t, t), text) < _BOUND_S
    assert _timed(lambda t: ts_reexports.declared_name(t, "default"), text) < _BOUND_S


def test_reexport_steps_cost_linear_time() -> None:
    for unit in ("export {\n", "import a\n", "import { a,\n", "export { a as b,\n"):
        text = unit * (_N // len(unit))
        assert _timed(lambda t: ts_reexports.reexport_steps(t, "b"), text) < _BOUND_S, unit
    # what they read is unchanged
    text = 'import A from "./a";\nimport { B as C } from "./b";\nexport { A as x, C as y };\n'
    text += 'export { z } from "./z";\nexport * from "./all";\n'
    assert ts_reexports.reexport_steps(text, "x") == [("./a", "default"), ("./all", "x")]
    assert ts_reexports.reexport_steps(text, "y") == [("./b", "B"), ("./all", "y")]
    assert ts_reexports.reexport_steps(text, "z") == [("./z", "z"), ("./all", "z")]


def test_blank_lines_cost_linear_time() -> None:
    yaml = "packages:\n  foo\n" + "  \n" * _N + "  x"
    assert _timed(lambda t: ts_workspace.workspace_globs(t, ""), yaml) < _BOUND_S
    assert _timed(lti.reexport_specs, "\n" * _N) < _BOUND_S
    assert _timed(lambda t: ts_reexports.declared_name(t, "default"), "\n" * _N) < _BOUND_S


def test_a_paths_table_is_capped_and_deduplicated_in_order() -> None:
    targets = ["b/*", "a/*", "b/*"] + [f"t{i}/*" for i in range(_N // 10)]
    patterns = {f"p{i}/*": ["x/*"] for i in range(ts_config.PATHS_PATTERNS_MAX + 10)}
    text = json.dumps({"compilerOptions": {"paths": {"*": targets, **patterns}}})
    t0 = time.perf_counter()
    cfg = ts_config.resolve_config("tsconfig.json", {"tsconfig.json": text}, lambda _: [], set())
    got = ts_config.paths_candidates("lib", cfg)
    assert time.perf_counter() - t0 < _BOUND_S
    assert len(cfg.tables[0].patterns) == ts_config.PATHS_PATTERNS_MAX
    assert got[:3] == ["b/lib", "a/lib", "t0/lib"]
    assert len(got) == ts_config.PATHS_TARGETS_MAX - 1  # the duplicate counted, then dropped


def test_member_globs_are_capped() -> None:
    present = {f"packages/p{i}/package.json" for i in range(3)}
    globs = [f"nope{i}/*" for i in range(ts_workspace.MEMBER_GLOBS_MAX)] + ["packages/*"]
    assert ts_workspace.member_dirs(globs, present) == []
    assert ts_workspace.member_dirs(globs[-2:], present) == [f"packages/p{i}" for i in range(3)]


def test_a_package_json_is_parsed_once_per_text(monkeypatch) -> None:
    loads: list[int] = []
    real = json.loads
    monkeypatch.setattr(lti.json, "loads", lambda s: loads.append(1) or real(s))
    manifest = json.dumps(
        {
            "exports": {
                ".": {"types": "./dist/index.d.ts", "import": "./dist/index.js"},
                "./utils/*": {"import": "./dist/utils/*.js"},
            }
        }
    )
    view = ingest_resolve._manifest_view
    first = lti.exports_candidates("@x/pkg", "packages/pkg", view(manifest))
    assert lti.exports_candidates("@x/pkg/utils/a", "packages/pkg", view(manifest))
    assert lti.exports_candidates("@x/pkg", "packages/pkg", view(manifest)) == first
    assert first and first[0].startswith("packages/pkg/")
    assert len(loads) == 1
    views = ingest_resolve._MANIFEST_VIEWS
    for i in range(views.maxsize + 1):
        view(json.dumps({"main": f"m{i}.js"}))
    assert len(views) == views.maxsize


def test_the_parsed_manifests_are_bounded_by_their_texts_length() -> None:
    views = ingest_resolve._MANIFEST_VIEWS
    pad = "x" * (ingest_resolve.MANIFEST_VIEWS_BYTES // 4)
    for i in range(10):
        ingest_resolve._manifest_view(json.dumps({"main": f"m{i}.js", "pad": pad}))
    assert views.weight <= ingest_resolve.MANIFEST_VIEWS_BYTES
    views.clear()
    wide = json.dumps({"main": "\U0001f600" * 10}, ensure_ascii=False)
    ingest_resolve._manifest_view(wide)
    assert views.weight == len(wide.encode())  # its UTF-8 bytes, not its characters
    assert 1 <= len(views) <= 4


_DEEP = "[" * 200_000  # past the parser's recursion limit


def test_a_manifest_nested_past_the_stack_is_no_manifest() -> None:
    assert ts_config.parse_jsonc(_DEEP) == {}
    assert ts_workspace.workspace_globs("", _DEEP) == []
    assert ts_workspace.extra_manifest_globs(_DEEP, _DEEP, _DEEP) == []
    assert lti.manifest_view(_DEEP) is None
    # nested deeper than the interpreter's own recursion limit, which the parser still reads
    for deep in (
        '{"exports":{".":' + '{"types":' * 1_200 + '"x"' + "}" * 1_202,
        '{"exports":{".":' + "[" * 1_200 + '"x"' + "]" * 1_200 + "}}",
    ):
        json.loads(deep)
        assert lti.manifest_view(deep) is None
    assert py_layouts._toml("a = " + "[" * 200_000) is None


def test_a_member_manifest_that_is_no_object_names_no_package() -> None:
    members = {"a": _DEEP, "b": "[]", "c": '"str"', "d": '{"name": "@x/d"}', "e": "{"}
    assert ts_workspace.registry(members) == {"@x/d": "d"}


def test_import_names_over_a_run_of_braces_cost_linear_time() -> None:
    from types import SimpleNamespace

    from server.code.app.index.ingest_edges import _import_names

    def run(n: int) -> None:
        _import_names(SimpleNamespace(source=("import " + "{" * n + ' from "x"').encode()), {})

    grows_linearly(run, 20_000, 80_000)


def test_an_import_list_holding_the_word_import_keeps_its_names() -> None:
    from types import SimpleNamespace

    from server.code.app.index.ingest_edges import _import_names

    src = 'import {\n  Foo, // eslint-disable-line import/no-deprecated\n  $import,\n} from "./foo";\n'
    got = _import_names(SimpleNamespace(source=src.encode()), {"Foo": "./foo", "$import": "./foo"})
    assert got == [("Foo", "Foo", "./foo"), ("$import", "$import", "./foo")]
    glob = 'const g = "/*";\nimport { A } from "./a";\n'  # a string's `/*` opens no comment
    assert _import_names(SimpleNamespace(source=glob.encode()), {"A": "./a"}) == [("A", "A", "./a")]
    assert [
        m.group(2) for m in ts_reexports._IMPORT_RE.finditer('import { $import } from "./a"')
    ] == ["./a"]


def test_a_named_export_list_run_resolves_in_linear_time() -> None:
    def run(n: int) -> None:
        ts_reexports.declared_name("export{X}\n" * n + 'import X from "y"', "X")

    grows_linearly(run, 5_000, 20_000)


def test_a_barrel_of_unclosed_comments_maps_its_aliases_in_linear_time() -> None:
    from server.code.domain.extract.lang_sfc import barrel_alias_map

    grows_linearly(lambda n: barrel_alias_map("/* " * n), 20_000, 80_000)


def test_an_anonymous_default_lookup_over_a_blank_run_costs_linear_time() -> None:
    for head in ("export default", "export default async"):
        grows_linearly(
            lambda n, head=head: ts_reexports.declared_name(head + " \n" * n + "-", "default"),
            5_000,
            20_000,
        )


def test_a_chain_of_conditions_that_picks_nothing_is_read_once_per_level(monkeypatch) -> None:
    calls = 0
    pick = lti._pick_condition

    def counted(target: Any) -> str:
        nonlocal calls
        calls += 1
        assert calls <= 1_000, "a level is read more than once"
        return pick(target)

    monkeypatch.setattr(lti, "_pick_condition", counted)
    text = '{"name":"@x/p","exports":{".":' + '{"types":' * 30 + "null" + "}" * 32
    assert lti.exports_candidates("@x/p", "p", lti.manifest_view(text)) == []
    assert calls == 31  # each object once and the leaf; read twice per level it was 2**30

"""The lanes' walks over a file, each with the text of about n characters it walks: the
shapes `test_code_regex_cost.py` times in a child process, kept apart so the child imports
only these and not every pattern the module-level scan loads. Also the class guard's data:
a sample per registered lane parser (`SAMPLES`), the generated shapes (`SHAPES`), the
per-lane timing (`registry_cost`) and the shapes still over linear (`KNOWN_SLOW`)."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable

from tests.unit.code_fixtures import linear_sample_context, untraced


def _markdown(text: str) -> object:
    from server.code.domain.extract.lang_markdown import markdown_chunks, parse_markdown

    return markdown_chunks(parse_markdown("a.md", text.encode()))


def _setup_py(text: str) -> object:
    from server.code.domain.extract.py_layouts import entry_points_from_manifest

    return entry_points_from_manifest("setup.py", text)


def _dockerfile(text: str) -> object:
    from server.code.domain.extract.lang_hosts import dockerfile_islands

    return dockerfile_islands(text.split("\n"))


def _jenkinsfile(text: str) -> object:
    from server.code.domain.extract.lang_hosts import jenkins_islands

    return jenkins_islands(text)


def _angular(text: str) -> object:
    from server.code.domain.extract.lang_angular import template_sites

    return template_sites(text)


def _angular_inline(text: str) -> object:
    from server.code.domain.extract.lang_angular import inline_template_sites
    from server.code.domain.extract.treesitter import ParsedFile

    return inline_template_sites(
        ParsedFile(path="a.component.ts", lang="typescript", source=text.encode())
    )


def _vue(text: str) -> object:
    from server.code.domain.extract.lang_vue import macro_symbols, parse_vue, template_sites

    pf = parse_vue("a.vue", text.encode())
    return template_sites(pf, {}), macro_symbols(pf, "A")


def _svelte(text: str) -> object:
    from server.code.domain.extract.lang_sfc import markup_sites, parse_svelte

    return markup_sites(parse_svelte("a.svelte", text.encode()), {})


def _py_routes(text: str) -> object:
    """The call-site route walks of the Python lane; the file's parse is not timed."""
    from server.code.domain.extract import py_routes as pr

    return pr._prefixes(text), pr._registrations(text, "a.py", pr._Collector())


def _py_decorator(text: str) -> object:
    from server.code.domain.extract import py_routes as pr

    return pr._one_decorator(text, "f", 1, pr._Collector(), {})


def _many_methods(opener: str) -> Callable[[int], str]:
    """A path of n/2 characters, then a methods list of n/2 characters."""
    return lambda n: opener + "a" * (n // 2) + "', methods=[" + _run("", "'a',")(n // 2) + "])"


def _env(text: str) -> object:
    from server.code.domain.extract.env_reads import env_facets
    from server.code.domain.extract.treesitter import ParsedFile

    return env_facets(ParsedFile(path="a.py", lang="python", source=text.encode()))


def _conftests(text: str) -> object:
    from server.code.domain.extract.py_layouts import layout_from_texts

    return layout_from_texts({}, text.split())


def _py_layout(path: str) -> Callable[[str], object]:
    def walk(text: str) -> object:
        from server.code.domain.extract.py_layouts import layout_from_texts

        return layout_from_texts({path: text}, [])

    return walk


def _dbt(text: str) -> object:
    from server.code.domain.extract.lang_sql_jinja import jinja_units

    return jinja_units("models/a.sql", "a", text)


def _cmake(text: str) -> object:
    from server.code.domain.extract.lang_cmake import commands

    return commands(text)


def _cmake_units(text: str) -> object:
    from server.code.domain.extract.lang_cmake import cmake_units

    return cmake_units("CMakeLists.txt", text)


def _variable_uses(n: int) -> str:
    """A variable of n/8 sources, used n/10 times by one target."""
    return "set(S " + "a.c " * (n // 8) + ")\nadd_library(t " + "${S} " * (n // 10) + ")\n"


def _glob_words(n: int) -> str:
    """A glob variable of n/4 words that name no source, used n/10 times by one target."""
    return "file(GLOB S " + "a " * (n // 4) + ")\nadd_library(t " + "${S} " * (n // 10) + ")\n"


def _file_set_entries(n: int) -> str:
    return "set(S " + "FILE_SET.c " * (n // 22) + ")\nadd_library(t " + "${S} " * (n // 10) + ")\n"


def _one_module_files(text: str) -> object:
    """A setup.cfg whose entry points all name one module, then that module once per
    4,000 characters of it."""
    from server.code.domain.extract.py_layouts import layout_from_texts, module_entry_facets
    from server.code.domain.extract.treesitter import ParsedFile

    layout = layout_from_texts({"setup.cfg": text}, [])
    pf = ParsedFile(path="m.py", lang="python", source=b"")
    return [len(module_entry_facets(pf, layout)) for _ in range(len(text) // 4000)]


def _entry_point_files(text: str) -> object:
    """A layout from one setup.py, then the entry points of one Python file per 4,000
    characters of it."""
    from server.code.domain.extract.py_layouts import layout_from_texts, module_entry_facets
    from server.code.domain.extract.treesitter import ParsedFile

    layout = layout_from_texts({"setup.py": text}, [])
    return [
        module_entry_facets(ParsedFile(path=f"r0/m{i}.py", lang="python", source=b""), layout)
        for i in range(len(text) // 4000)
    ]


def _entry_points(n: int) -> str:
    roots = "".join(f"find_packages('r{i}')\n" for i in range(64))
    return roots + '"console_scripts":[' + _numbered('"a{0}=m{0}:f",')(n) + "]"


def _doc_references(text: str) -> object:
    """A file's backticked mentions against one definition of the name per 100 characters."""
    from server.code.domain.extract.lang_markdown import (
        doc_reference_edges,
        doc_reference_sites,
        parse_markdown,
    )

    pf = parse_markdown("a.md", text.encode())
    table = {"abc": [(f"s{i}", f"m{i}::abc", "function") for i in range(len(text) // 100)]}
    return doc_reference_edges(pf, doc_reference_sites(pf), table)


def _pr_body(text: str) -> object:
    from server.code.domain.pr_lexemes import strip_pr_template

    return strip_pr_template(text)


def _linked_issues(text: str) -> object:
    from server.code.domain.pr_lexemes import linked_issues

    return linked_issues(text)


def _lane_includes(text: str) -> object:
    import asyncio

    from server.code.app.index.ingest_resolve import _resolve_simple_lane_includes
    from server.code.domain.extract.lang_markdown import parse_markdown

    class Repo:
        async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
            return set(paths)

    pf = parse_markdown("a/b/c/d/e/f/g/a.md", text.encode())
    return asyncio.run(_resolve_simple_lane_includes(Repo(), "o", "n", pf))  # type: ignore[arg-type]


def _python_parse(text: str) -> object:
    from server.code.domain.extract.lang_python import parse_python

    return parse_python("a.py", text.encode()).includes


def _rust_parse(text: str) -> object:
    from server.code.domain.extract.lang_rust import parse_rust

    return parse_rust("src/lib.rs", text.encode()).includes


def _edges(
    path: str, table_size: int = 0, context: dict[str, object] | None = None, own: bool = False
) -> Callable[[str], object]:
    """A file's call edges, against a name table of `table_size` definitions of `a` and of a
    class `Base` (with `own`, the file's own definitions too, as a re-index reads them)
    and the ingest's lane `context` (its present Python paths, as the file's includes).
    The parse is kept per text, so the best of three times the edges alone; its deadline
    is lifted in the walk's child process, where a loaded host would trip it."""
    parsed: dict[str, object] = {}

    def walk(text: str) -> object:
        import copy

        from server.code.app.index.ingest_parse import parse_for_ingest
        from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
        from server.settings import settings

        if text not in parsed:
            settings.code_parse_timeout_s = 600
            pf, err = parse_for_ingest(path, text.encode())
            assert pf is not None and pf.tree is not None, err
            parsed.clear()
            parsed[text] = pf
        table = {"a": [(f"s{i}", f"m{i}::a", "function") for i in range(table_size)]}
        table["Base"] = [(f"b{i}", f"m{i}::Base", "class") for i in range(table_size)]
        if own:
            for s in parsed[text].symbols:  # type: ignore[attr-defined]
                table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
        pf = copy.copy(parsed[text])
        if context:
            pf.includes = list(context.get("py_present", []))  # type: ignore[call-overload]
        return extract_edges_and_misses(pf, table, None, lane_context=context)  # type: ignore[arg-type]

    return walk


def _callee_targets(text: str) -> object:
    """The return-type round of call binding over a file's callees, against 2,000
    repository definitions of `a`."""
    from server.code.app.index.ingest_parse import parse_for_ingest
    from server.code.domain.extract.edges_treesitter import callee_targets
    from server.settings import settings

    settings.code_parse_timeout_s = 600  # the walk's child process only, as in `_edges`
    pf, err = parse_for_ingest("a.py", text.encode())
    assert pf is not None and pf.tree is not None, err
    return callee_targets(pf, {"a": [(f"s{i}", f"m{i}::a", "function") for i in range(2000)]})


def _callers(head: str, unit: str, caller: str) -> Callable[[int], str]:
    """n/2 characters of one name's definitions, then n/2 of functions that call it."""
    return lambda n: head + unit * (n // 2 // len(unit)) + _numbered(caller)(n // 2)


def _deep_proto_imports(text: str) -> object:
    from server.code.domain.extract.lang_proto import resolve_imports

    return resolve_imports("a/" * 500 + "x.proto", text.split(), set())


def _deep_includes(text: str) -> object:
    from server.code.app.index.ingest_headers import include_candidates

    return [include_candidates("a/" * 500 + "x.cpp", inc) for inc in text.split()]


def _go_replaces(text: str) -> object:
    from server.code.domain.extract.lang_go import go_module_index, go_wanted_paths

    gomod = "module m\n" + "".join(f"replace r{i} => ./d{i}\n" for i in range(3600))
    return go_wanted_paths(text.split(), go_module_index({"go.mod": gomod}))


def _ts_paths(text: str) -> object:
    from server.code.domain.extract import ts_config as tc

    table = tc.PathsTable(base="", patterns={f"@p{i}/*": [f"src/p{i}/*"] for i in range(1000)})
    cfg = tc.ResolvedConfig(dir="", tables=[table])
    return [tc.paths_candidates(spec, cfg) for spec in text.split()]


def _cmake_links(text: str) -> object:
    """A file's links, each against one definition of the name per 100 characters."""
    from server.code.domain.extract.lang_cmake import cmake_external_edges
    from server.code.domain.extract.treesitter import ParsedFile

    pf = ParsedFile(path="CMakeLists.txt", lang="cmake", source=b"")
    table = {"t": [(f"s{i}", f"q{i}::t", "target") for i in range(len(text) // 100)]}
    return cmake_external_edges(pf, [("t", "t", i) for i in range(len(text) // 20)], table)


def _wanted(lane: str) -> Callable[[str], object]:
    """The import probe of one lane over a file's specifiers, one per line."""

    def walk(text: str) -> object:
        from server.code.domain.extract import lang_go, lang_proto, lang_python, lang_rust
        from server.code.domain.extract import lang_typescript_imports as lang_ts

        specs = text.split()
        if lane == "python":
            return lang_python.wanted_paths("a/b/c.py", specs, ("r0", "r1"))
        if lane == "typescript":
            return lang_ts.wanted_paths("a/b/c.ts", specs)
        if lane == "rust":
            return lang_rust.wanted_paths("src/a/b.rs", specs)
        if lane == "go":
            return lang_go.go_wanted_paths(specs, [("m", "")])
        if lane == "thrift":
            from server.code.domain.extract import lang_thrift

            return lang_thrift.wanted_paths("a/b/c.thrift", specs)
        return lang_proto.wanted_paths("a/b/c.proto", specs)

    return walk


def _run(pre: str, unit: str, end: str = "") -> Callable[[int], str]:
    return lambda n: pre + (unit * (n // len(unit) + 1))[:n] + end


def _links(n: int) -> str:
    return "# a\n" + "".join(f"[a](a{i})\n" for i in range(n // 8))


def _numbered(unit: str) -> Callable[[int], str]:
    return lambda n: "".join(unit.format(i) for i in range(n // len(unit)))


def _where(n: int) -> str:
    return "[tool.setuptools.packages.find]\nwhere = [" + _numbered('"a{}",')(n) + "]\n"


def _ts_routes(text: str) -> object:
    from server.code.domain.extract.treesitter import ParsedFile
    from server.code.domain.extract.ts_routes import route_facets

    return route_facets(
        ParsedFile(path="src/c.controller.ts", lang="typescript", source=text.encode())
    )


def _inline_templates(n: int) -> str:
    return "".join(f"@Component({{template:`<a-b{i}>`}})class A{i}{{}}\n" for i in range(n // 40))


# (id, walk, the text of about n characters it walks)
WALKS: list[tuple[str, Callable[[str], object], Callable[[int], str]]] = [
    ("markdown-heading-spaces", _markdown, _run("# a", " ", "b")),
    ("markdown-headings", _markdown, _run("", "# a\n")),
    ("markdown-links", _markdown, _links),
    (
        "markdown-deep-long-titles",
        _markdown,
        lambda n: (
            "".join("#" * k + " " + "a" * 1000 + "\n" for k in range(1, 6))
            + "###### a\n" * (n // 9)
        ),
    ),
    (
        "markdown-long-parent-title",
        _markdown,
        lambda n: "# " + "a" * (n // 2) + "\n" + "## a\n" * (n // 10),
    ),
    ("setup-py-unclosed-lists", _setup_py, _run("", '"console_scripts":[')),
    ("setup-py-entry-points", _setup_py, _run('"console_scripts":[', '"a=b:c",\n', "]")),
    ("setup-py-roots", _py_layout("setup.py"), _numbered("find_packages('a{}')\n")),
    ("pyproject-roots", _py_layout("pyproject.toml"), _where),
    ("conftest-roots", _conftests, _numbered("d{}/conftest.py\n")),
    ("dockerfile-continued-rows", _dockerfile, _run("", "a \\\n")),
    ("dockerfile-flags", _dockerfile, _run("RUN ", "--a ", "b")),
    ("dockerfile-stages", _dockerfile, _run("", "FROM a AS b\n")),
    ("jenkinsfile-steps", _jenkinsfile, _run("", "sh 'a'\n")),
    ("angular-sites", _angular, _run("", "<a-b>\n")),
    ("angular-inline-templates", _angular_inline, _inline_templates),
    ("vue-sites", _vue, _run("<template>\n", "<A/>\n", "\n</template>\n")),
    ("vue-macro-members", _vue, _run("<script setup>\ndefineProps({", "a:1,", "})\n</script>\n")),
    ("svelte-sites", _svelte, _run("", "<A/>\n")),
    ("svelte-event-modifiers", _svelte, _run("<b ", "onx|", ">")),
    ("dbt-raw-blocks", _dbt, _run("", "{% raw %}a{% endraw %}")),
    ("dbt-unclosed-tags", _dbt, _run("{{ config(", "tags=[", ") }}")),
    ("cmake-rows", _cmake, _run("", "a\n")),
    ("cmake-variable-uses", _cmake_units, _variable_uses),
    ("cmake-unclosed-functions", _cmake_units, _run("", "function(f)\n")),
    ("cmake-glob-words", _cmake_units, _glob_words),
    (
        "cmake-long-entry",
        _cmake_units,
        lambda n: "set(S " + "a" * (n // 2) + ".c)\nadd_library(t " + "${S} " * (n // 10) + ")\n",
    ),
    ("cmake-file-set-entries", _cmake_units, _file_set_entries),
    ("python-entry-point-files", _entry_point_files, _entry_points),
    (
        "setup-cfg-entry-points-of-one-module",
        _one_module_files,
        lambda n: "[options.entry_points]\nconsole_scripts =\n" + _numbered("  a{0} = m:f\n")(n),
    ),
    ("setup-cfg-name-spaces", _py_layout("setup.cfg"), _run("[options]\na", " ", "b\n")),
    ("markdown-ambiguous-references", _doc_references, _run("# a\n", "`abc`\n")),
    ("pr-body-unclosed-comments", _pr_body, _run("", "<!--")),
    ("pr-body-fix-spaces", _linked_issues, _run("fix", " ", "b")),
    ("markdown-lane-includes", _lane_includes, _numbered("[a](x{}.md)\n")),
    ("python-imports", _wanted("python"), _numbered("m{}\n")),
    ("python-import-statements", _python_parse, _numbered("import m{}\n")),
    (
        "python-long-module-members",
        _python_parse,
        lambda n: "from " + "a." * (n // 4) + "a import " + _numbered("b{},")(n // 2) + "c\n",
    ),
    (
        "rust-long-use-prefix",
        _rust_parse,
        lambda n: "use " + "a::" * (n // 6) + "a::{" + _numbered("b{},")(n // 2) + "c};\n",
    ),
    ("typescript-imports", _wanted("typescript"), _numbered("./m{}\n")),
    ("rust-uses", _wanted("rust"), _numbered("crate::m{}\n")),
    ("rust-long-use", _wanted("rust"), _run("", "a::")),
    ("go-imports", _wanted("go"), _numbered("m/p{}\n")),
    ("proto-imports", _wanted("proto"), _numbered("p{}.proto\n")),
    ("thrift-includes", _wanted("thrift"), _numbered("t{}.thrift\n")),
    ("python-route-rows", _py_routes, _run("", "app.add_url_rule('a')\n")),
    ("python-unclosed-include-router", _py_routes, _run("", "a.include_router(a,")),
    (
        "ts-controller-prefix",
        _ts_routes,
        lambda n: (
            "@Controller('"
            + "a" * (n // 2)
            + "')\nclass C {\n"
            + _run("", "@Get('a') a(){}\n")(n // 2)
        ),
    ),
    ("python-unclosed-add-url-rule", _py_routes, _run("", "a.add_url_rule('a'")),
    ("python-unclosed-route", _py_routes, _run("import starlette.routing\n", "Route('a'")),
    ("python-env-reads", _env, _run("", "os.environ['A']\n")),
    ("python-route-methods", _py_routes, _many_methods("x.add_url_rule('/")),
    ("python-decorator-methods", _py_decorator, _many_methods("@app.route('/")),
    ("starlette-route-methods", _py_routes, _many_methods("import starlette.routing\nRoute('/")),
    ("python-classes", _python_parse, _numbered("class C{}: pass\n")),
    (
        "python-fixture-parse",
        _python_parse,
        _callers("", "def f(): pass\n", "def g{}(request): request.getfixturevalue('x')\n"),
    ),
    ("rust-trait-impls", _rust_parse, _numbered("struct S{0};\nimpl T for S{0} {{}}\n")),
    ("python-same-named-calls", _edges("a.py"), _callers("", "def a(): pass\n", "a()\n")),
    ("python-callers", _edges("a.py"), _callers("", "def a(): pass\n", "def g{}(): a()\n")),
    (
        "python-self-calls",
        _edges("a.py"),
        _callers("class C:\n", "    def a(self): pass\n", "    def g{}(self): self.a()\n"),
    ),
    ("python-table-calls", _edges("a.py", 2000), _run("", "a()\n")),
    ("python-receiver-calls", _edges("a.py", 2000), _numbered("o{}.a()\n")),
    ("python-callee-returns", _callee_targets, _numbered("x{0} = o{0}.a()\n")),
    (
        "python-inherited-calls",
        _edges("a.py", 2000),
        lambda n: (
            "from m import Base\nclass C(Base):\n" + _numbered("    def g{}(self): self.a()\n")(n)
        ),
    ),
    (
        "python-fixture-requests",
        _edges("a.py"),
        _callers("", "def f(): pass\n", "def g{}(request): request.getfixturevalue('x')\n"),
    ),
    (
        "python-long-module-calls",
        _edges(
            "a/b/c/d/e.py", 2, {"py_present": ["q.py"], "py_roots": [f"r{i}" for i in range(66)]}
        ),
        lambda n: "import " + ".".join(["m"] * 1000) + " as x\n" + _run("", "x.f()\n")(n),
    ),
    (
        "python-delegating-methods",
        _edges("a.py", own=True),
        _numbered("class A{0}:\n    def a(self):\n        return self.inner.a()\n"),
    ),
    (
        "go-callers",
        _edges("a.go"),
        _callers("package a\n", "func a() {}\n", "func g{}() {{ a() }}\n"),
    ),
    ("cpp-callers", _edges("a.cpp"), _callers("", "void a() {}\n", "void g{}() {{ a(); }}\n")),
    ("rust-callers", _edges("src/a.rs"), _callers("", "fn a() {}\n", "fn g{}() {{ a(); }}\n")),
    ("proto-deep-imports", _deep_proto_imports, _numbered("p{}.proto\n")),
    ("cpp-deep-includes", _deep_includes, _numbered("h{}.h\n")),
    ("go-replaced-modules", _go_replaces, _numbered("x/p{}\n")),
    ("ts-paths-patterns", _ts_paths, _numbered("q{}\n")),
    ("cmake-same-named-links", _cmake_links, _run("", "a")),
]
_WALK_N = 100_000


def walk_cost(
    name: str, *, diagnostics: list[dict[str, object]] | None = None
) -> tuple[float, float]:
    """The best of three times of the walk at N and 4N characters."""
    walk, text = next((w[1], w[2]) for w in WALKS if w[0] == name)

    def timed_cost(n: int) -> float:
        t = text(n)
        with untraced():  # a full collection costs in proportion to the heap, not to the text
            t0 = time.process_time()  # CPU time: a loaded host stretches wall time, not this
            walk(t)
            return time.process_time() - t0

    def cost(n: int) -> float:
        if diagnostics is None:
            return timed_cost(n)
        before = linear_sample_context()
        cpu_s = None
        try:
            cpu_s = timed_cost(n)
            return cpu_s
        finally:
            diagnostics.append(
                {"n": n, "cpu_s": cpu_s, "before": before, "after": linear_sample_context()}
            )

    return min(cost(_WALK_N) for _ in range(3)), min(cost(4 * _WALK_N) for _ in range(3))


# The class guard: every lane parser in the ingest's registry (`ingest_parse._PARSERS`)
# over generated shapes, so a walk added without a cost walk of its own is timed too.
# (path, header, the language the parse reports) per lane; the header makes a
# content-detected lane (a manifest, a template, a TLC config) read as that lane.
SAMPLES: dict[str, tuple[str, str, str]] = {
    "cpp": ("a.cpp", "", "cpp"),
    "python": ("a.py", "", "python"),
    "shell": ("a.sh", "", "shell"),
    "typescript": ("a.ts", "", "typescript"),
    "javascript": ("a.js", "", "javascript"),
    "rust": ("src/a.rs", "", "rust"),
    "prisma": ("a.prisma", "", "prisma"),
    "go": ("a.go", "package a\n", "go"),
    "java": ("A.java", "", "java"),
    "vue": ("a.vue", "", "javascript"),
    "svelte": ("a.svelte", "", "javascript"),
    "astro": ("a.astro", "", "typescript"),
    "angular_template": ("a.component.html", "", "typescript"),
    "proto": ("a.proto", "", "proto"),
    "thrift": ("a.thrift", "", "thrift"),
    "gotemplate": ("chart/templates/a.yaml", "{{ .Values.a }}\n", "gotemplate"),
    "hcl": ("a.tf", "", "hcl"),
    "k8s": ("a.yaml", "apiVersion: v1\nkind: ConfigMap\n", "k8s"),
    "jsonnet": ("a.jsonnet", "", "jsonnet"),
    "lua": ("a.lua", "", "lua"),
    "graphql": ("a.graphql", "", "graphql"),
    "tla": ("a.tla", "---- MODULE a ----\n", "tla"),
    "tla_config": ("a.cfg", "INIT Init\n", "tla_config"),
    "flatbuffers": ("a.fbs", "", "flatbuffers"),
    "cmake": ("CMakeLists.txt", "", "cmake"),
    "doc": ("a.md", "", "doc"),
}

_CHARS = " \t\na({[<\"'`#/*\\$:-=|.,;"
_REPEATED = (
    "a ",
    "a\n",
    "a(\n",
    "a.a",
    "a::",
    "a/",
    "import a\n",
    "use a;\n",
    '#include "a"\n',
    "include(a)\n",
    "`abc`\n",
    "[a](a)\n",
    "{{ a }}",
    "<a/>",
    "a: a\n",
    "- a\n",
    "a = a\n",
    "def a(): pass\n",
    "class A {}\n",
    "function(a)\n",
    "@a\n",
    "# a\n",
)
_NUMBERED = (
    "import m{}\n",
    "use m{};\n",
    "`a{}`\n",
    "a{}()\n",
    "[a](a{}.md)\n",
    '#include "a{}.h"\n',
)
_OPENERS = ("(", "{", "[", "<a>", "<!--", "/*", "{{", '"""', "${", "{%", "`", "'", '"')


def _fan_out(
    entry: str, use: str, head: str = "", mid: str = "\n", tail: str = ""
) -> Callable[[int], str]:
    """n/2 characters of entries, then n/2 of uses."""
    return lambda n: head + _run("", entry)(n // 2) + mid + _run("", use)(n // 2) + tail


SHAPES: list[tuple[str, Callable[[int], str]]] = [
    *((f"run {c!r}", _run("", c)) for c in _CHARS),
    *((f"names {u!r}", _run("", u)) for u in _REPEATED),
    *((f"unclosed {o!r}", _run("", o)) for o in _OPENERS),
    *((f"unclosed {o!r} words", _run("", o + "a ")) for o in _OPENERS),
    ("fan-out list", _fan_out("a,", "a ")),
    ("fan-out variable", _fan_out("a.c ", "${S} ", "set(S ", ")\nadd_library(t ", ")\n")),
    ("fan-out members", _fan_out("a,", "m.a\n", "from m import ")),
    ("fan-out definitions", _fan_out("def a(): pass\n", "a()\n")),
    # distinct names: a dedupe by a list's `in` or a scan per name is linear on one name
    *((f"numbered {u!r}", _numbered(u)) for u in _NUMBERED),
    ("numbered sources", lambda n: "add_library(t " + _numbered("a{}.c ")(n) + ")\n"),
]


class _Names:
    """A name table that holds two definitions of every name asked for."""

    async def name_candidates(
        self, org: str, ns: str, names: list[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        return {
            n: [(f"{n}1", f"a::{n}", "function"), (f"{n}2", f"b::{n}", "function")] for n in names
        }


def _extract(
    path: str, source: bytes, *, diagnostics: dict[str, str] | None = None
) -> tuple[object, bool]:
    """What the ingest derives from one file without the store: the parse, its chunks
    and facets, its call edges, and a tree-less lane's own edges against a name table,
    under the recursion limit the edge stage retries with. A parse that fails or a walk
    that raises costs what it ran and is reported as not finished; running out of memory
    fails."""
    import asyncio
    import sys

    from server.code.app.index import ingest_edges
    from server.code.app.index.ingest_chunks import _chunks_for
    from server.code.app.index.ingest_parse import (
        _LANE_INCLUDE_LANGS,
        LANE_UNIT_LANGS,
        parse_for_ingest,
    )
    from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
    from server.code.domain.extract.lang_prisma import prisma_edges

    pf, err = parse_for_ingest(path, source)
    if diagnostics is not None and err:
        diagnostics["parse"] = err
    if err.startswith("parse: MemoryError"):
        raise MemoryError(err)  # the parse reports it as a failed file
    if pf is None:
        return None, False
    repo = _Names()
    steps = [
        ("chunks", lambda: _chunks_for(pf, path, source, 6000)),
        ("edges", lambda: extract_edges_and_misses(pf, {}, None)),
    ]
    if pf.lang in _LANE_INCLUDE_LANGS:
        steps.append(
            (
                "simple_lane_edges",
                lambda: asyncio.run(ingest_edges._simple_lane_edges(repo, "o", "n", pf)),
            )
        )  # type: ignore[arg-type]
    if pf.lang in LANE_UNIT_LANGS:
        steps.append(
            (
                "lane_site_edges",
                lambda: asyncio.run(ingest_edges._lane_site_edges(repo, "o", "n", pf)),
            )
        )  # type: ignore[arg-type]
    if pf.lang == "prisma":
        steps.append(("prisma_edges", lambda: prisma_edges(pf)))
    finished = True
    before = sys.getrecursionlimit()
    sys.setrecursionlimit(ingest_edges._DEEP_RECURSION_LIMIT)
    try:
        for name, step in steps:
            try:
                step()
            except MemoryError:
                raise
            except Exception as error:  # noqa: BLE001 — the guard times walks; a crash is another test's
                finished = False
                if diagnostics is not None:
                    diagnostics[name] = type(error).__name__
    finally:
        sys.setrecursionlimit(before)
    return pf, finished and not err


_REGISTRY_N = 5_000
# Shared with test_code_regex_cost.py: RATIO and BOUND_S judge every timing test there,
# LANE_FLOOR_S the class guard (`_lane_over`).
RATIO = 8.0
LANE_FLOOR_S = 0.05
BOUND_S = 2.0
# A shape over this at 4N is not timed at 16N: a quadratic walk there takes seconds.
STEP_MAX_S = 0.25
# A shape over this at N is not timed at 4N: it is over any linear budget already.
N_MAX_S = 1.0
# An unlisted shape over this at 4N and over RATIO times its N time is timed again
# before it is judged; it sits under LANE_FLOOR_S so a shape near the floor is not
# failed on one slow run.
RETRY_FLOOR_S = 0.02


def judged_step(
    cost: Callable[[int], tuple[float, int, bool, bool]], n: int, listed: bool
) -> tuple[str, float, float, int, int] | None:
    """(the step judged: `N`, `4N` or `N, 16N unread` when the 16N text stopped early or
    read as another lane; the time at its two sizes; the bytes tree-sitter parsed at each)
    of one shape, or None when its text at `n` does not read as the lane. `cost(n)` is
    (the time, the bytes parsed, whether the text read as the lane, whether every parse and
    walk finished). An unlisted shape still over linear after the best of three, from
    LANE_FLOOR_S to STEP_MAX_S at 4N and under 16 times its N time (pure quadratic growth,
    which no cache step reaches), is judged from 4N to 16N instead."""
    small, bs, lane, _ = cost(n)
    if not lane:
        return None
    if small > N_MAX_S:
        return "N", small, float("inf"), bs, bs
    big, bb, _, _ = cost(4 * n)
    if (listed and big < LANE_FLOOR_S) or (
        not listed and big > RETRY_FLOOR_S and big > RATIO * small
    ):
        small = min(small, cost(n)[0], cost(n)[0])
        big = min(big, cost(4 * n)[0], cost(4 * n)[0])
    in_window = LANE_FLOOR_S < big < STEP_MAX_S
    in_band = RATIO * small < big < 16 * small
    if listed or not (in_window and in_band):
        return "N", small, big, bs, bb
    # The N walk can fit in the CPU cache and the 4N one not, so that step can grow over 8
    # times on a busy host; from 4N to 16N only a walk over linear does, unless a per-file
    # count cap falls between its 4N and 16N items, which also bounds its real cost.
    huge, bh, lane, finished = cost(16 * n)
    if not (lane and finished):  # a parse or walk that stopped early looks cheap
        return "N, 16N unread", small, big, bs, bb
    if RATIO * big < huge < BOUND_S:  # timed twice more when it looks over linear
        again = (cost(16 * n), cost(16 * n))
        huge = min([huge] + [t for t, _, read, done in again if read and done])
    return "4N", big, huge, bb, bh


def registry_cost(
    lang: str, *, diagnostics: dict[str, list[dict[str, object]]] | None = None
) -> list[tuple[str, str, float, float, int, int]]:
    """`judged_step` per shape the lane reads as its own (a manifest shape can read as a
    template), with the shape's name first. The parses' time is not counted, since each
    has its own CPU deadline (`bounded_parse`); the bytes they read are returned, so a walk
    that parses the file again per block is still seen."""
    from server.code.domain.extract import bounded_parse

    path, head, as_lang = SAMPLES[lang]
    real = bounded_parse.parse
    # the tree-sitter parses of one file: their time, the bytes they read, whether one raised
    parse_s, parse_bytes, parse_raised = 0.0, 0, False
    errors: dict[str, str] = {}

    def timed(parser: object, source: bytes) -> object:
        nonlocal parse_s, parse_bytes, parse_raised
        t0 = time.process_time()
        try:
            return real(parser, source)
        except BaseException as error:
            parse_raised = True
            if diagnostics is not None:
                errors.setdefault("native_parse", type(error).__name__)
            raise
        finally:
            parse_s += time.process_time() - t0
            parse_bytes += len(source)

    bounded_parse.parse = timed  # type: ignore[assignment]

    def cost(
        text: Callable[[int], str], n: int, *, samples: list[dict[str, object]] | None = None
    ) -> tuple[float, int, bool, bool]:
        nonlocal parse_s, parse_bytes, parse_raised, errors
        source = (head + text(n)).encode()
        parse_s, parse_bytes, parse_raised = 0.0, 0, False
        errors = {}
        # These boundaries include untraced setup and restoration, outside the timed walk.
        before = linear_sample_context() if samples is not None else None
        with untraced():
            t0 = time.process_time()  # CPU time, as in `walk_cost`
            if samples is None:
                pf, walked = _extract(path, source)
            else:
                pf, walked = _extract(path, source, diagnostics=errors)
            took = time.process_time() - t0 - parse_s
        lane, finished = getattr(pf, "lang", "") == as_lang, walked and not parse_raised
        if samples is not None:
            # Keep every attempt: the judge can select CPU minima from separate runs.
            samples.append(
                {
                    "n": n,
                    "source_bytes": len(source),
                    "cpu_s": took,
                    "before": before,
                    "after": linear_sample_context(),
                    "parsed_bytes": parse_bytes,
                    "lane": lane,
                    "finished": finished,
                    "tree": getattr(pf, "tree", None) is not None,
                    "errors": errors,
                }
            )
        return took, parse_bytes, lane, finished

    cost(lambda n: "a\n", 1)  # the first file of a lane loads its grammar and imports
    out = []
    for name, text in SHAPES:
        # distinct names run 4 times larger: a dedupe by a list's `in` is quadratic at C
        # speed, under the timer's floor at 20,000 characters
        n = 4 * _REGISTRY_N if name.startswith("numbered") else _REGISTRY_N
        listed = name in KNOWN_SLOW.get(lang, ())
        samples = diagnostics.setdefault(name, []) if diagnostics is not None else None
        got = judged_step(functools.partial(cost, text, samples=samples), n, listed)
        if got is not None:
            out.append((name, *got))
    return out


# lane -> the shapes still over linear there, each a follow-up fix. The guard fails once
# a listed shape runs clearly in linear time, so the entry goes with its fix.
KNOWN_SLOW: dict[str, tuple[str, ...]] = {}

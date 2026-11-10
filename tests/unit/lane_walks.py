"""The lanes' walks over a file, each with the text of about n characters it walks: the
shapes `test_code_regex_cost.py` times in a child process, kept apart so the child imports
only these and not every pattern the module-level scan loads."""

from __future__ import annotations

import time
from collections.abc import Callable


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
    ("markdown-lane-includes", _lane_includes, _numbered("[a](x{}.md)\n")),
    ("python-imports", _wanted("python"), _numbered("m{}\n")),
    ("python-import-statements", _python_parse, _numbered("import m{}\n")),
    ("typescript-imports", _wanted("typescript"), _numbered("./m{}\n")),
    ("rust-uses", _wanted("rust"), _numbered("crate::m{}\n")),
    ("rust-long-use", _wanted("rust"), _run("", "a::")),
    ("go-imports", _wanted("go"), _numbered("m/p{}\n")),
    ("proto-imports", _wanted("proto"), _numbered("p{}.proto\n")),
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
]
_WALK_N = 100_000


def walk_cost(name: str) -> tuple[float, float]:
    """The best of three times of the walk at N and 4N characters."""
    walk, text = next((w[1], w[2]) for w in WALKS if w[0] == name)

    def cost(n: int) -> float:
        t = text(n)
        t0 = time.perf_counter()
        walk(t)
        return time.perf_counter() - t0

    return min(cost(_WALK_N) for _ in range(3)), min(cost(4 * _WALK_N) for _ in range(3))

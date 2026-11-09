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
    from server.code.domain.extract.lang_python import parse_python
    from server.code.domain.extract.py_routes import py_facets

    return py_facets(parse_python("a.py", text.encode()))


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


def _run(pre: str, unit: str, end: str = "") -> Callable[[int], str]:
    return lambda n: pre + (unit * (n // len(unit) + 1))[:n] + end


def _links(n: int) -> str:
    return "# a\n" + "".join(f"[a](a{i})\n" for i in range(n // 8))


def _numbered(unit: str) -> Callable[[int], str]:
    return lambda n: "".join(unit.format(i) for i in range(n // len(unit)))


def _where(n: int) -> str:
    return "[tool.setuptools.packages.find]\nwhere = [" + _numbered('"a{}",')(n) + "]\n"


def _many_methods(n: int, opener: str, between: str) -> str:
    """A path of n/2 characters, then a methods list of n/2 characters."""
    return opener + "a" * (n // 2) + between + _run("", "'a',")(n // 2) + "])"


def _declared_prefix(n: int, opener: str, route: str, close: str) -> str:
    """One prefix of n/2 characters, then n/2 characters of routes under it."""
    return opener + "a" * (n // 2) + close + _run("", route)(n // 2)


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
    ("python-route-rows", _py_routes, _run("", "app.add_url_rule('a')\n")),
    ("python-unclosed-include-router", _py_routes, _run("", "a.include_router(a,")),
    (
        "ts-controller-prefix",
        _ts_routes,
        lambda n: _declared_prefix(n, "@Controller('", "@Get('a') a(){}\n", "')\nclass C {\n"),
    ),
    ("python-unclosed-add-url-rule", _py_routes, _run("", "a.add_url_rule('a'")),
    ("python-unclosed-route", _py_routes, _run("import starlette.routing\n", "Route('a'")),
    ("python-env-reads", _env, _run("", "os.environ['A']\n")),
    (
        "python-route-methods",
        _py_routes,
        lambda n: _many_methods(n, "x.add_url_rule('/", "', methods=["),
    ),
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

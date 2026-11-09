"""Every module-level regular expression the code index runs over repository content
costs time linear in the text.

Each compiled pattern in `server/code/domain/extract` and `server/code/app/index` is
searched (`finditer`, `search`, `match`) over adversarial texts a repository can commit:
blank lines, whitespace runs, every opener the pattern names with no closer, unclosed
comments and templates, and each keyword the pattern spells repeated. At N and 4N
characters the 4N search must take under 8 times the N one (a quadratic one takes 16)
and under an absolute bound. Composite texts, a prefix the pattern spells, a run of spaces
or of one keyword with an opener, and a closing character, catch the patterns whose
optional parts split one run every way or that search on from each keyword. A pattern
only ever used with `fullmatch` is anchored with `\\A`, so the searches here cost what
its use does. The lanes' own walks over a file (`_WALKS`) are timed the same way, each in
a child process with a deadline."""

from __future__ import annotations

import importlib
import json
import pathlib
import pkgutil
import re
import subprocess
import sys
import time
from collections.abc import Callable

import pytest

_PACKAGES = ("server.code.domain.extract", "server.code.app.index")
_N = 50_000
# Below this the timer's noise decides the ratio; a quadratic search at 4N = 200,000
# characters takes seconds, a linear one milliseconds.
_FLOOR_S = 0.1
_RATIO = 8.0
_BOUND_S = 2.0

_UNITS = (
    "\n",
    " \n",
    "  \n",
    " ",
    "\t",
    "a",
    "z_",
    "a ",
    "a\n",
    ".",
    ",",
    "-",
    "- ",
    "#",
    "*",
    "/* ",
    "//",
    "{",
    "{{",
    "{{ ",
    "[",
    "(",
    "<",
    "'",
    "a$",
    "a-",
    "'\\",
    '"\\',
    '"',
    "`",
    "$",
    "${a}",
    "|",
    ":",
    "export {\n",
    "import a\n",
    "const{",
    "package_dir={",
    "interface{",
    "usefixtures(",
    "FROM {",
    "<<A\n",
    '"${a}',
)


def _patterns() -> list[tuple[str, re.Pattern]]:
    seen: dict[tuple[object, int], tuple[str, re.Pattern]] = {}
    for pkg in _PACKAGES:
        root = importlib.import_module(pkg)
        for info in pkgutil.iter_modules(root.__path__):
            mod = importlib.import_module(f"{pkg}.{info.name}")
            for name, value in vars(mod).items():
                if isinstance(value, re.Pattern):
                    seen.setdefault((value.pattern, value.flags), (f"{mod.__name__}.{name}", value))
    return sorted(seen.values(), key=lambda t: t[0])


def _words(src: str) -> list[str]:
    """The words a pattern spells, in its order, its escapes (`\\b`, `\\s`) dropped first."""
    return list(dict.fromkeys(re.findall(r"[A-Za-z_]{3,}", re.sub(r"\\.", " ", src))))


def _openers(p: re.Pattern) -> list[str]:
    """The literal text each of a pattern's alternatives starts with (`{%`, `${{`,
    `@Controller(`), past its anchors and lookarounds: a unit that is not one is refused
    at its first character, so a pattern that rescans from each opener looks linear."""
    from re import _constants as c
    from re import _parser as parser

    def lead(items: list[tuple[object, object]]) -> list[str]:
        text = ""
        for op, av in items:
            if op is c.LITERAL:
                text += chr(av)  # type: ignore[arg-type]
            elif op in (c.AT, c.ASSERT, c.ASSERT_NOT):
                continue
            elif op in (c.MAX_REPEAT, c.MIN_REPEAT, c.POSSESSIVE_REPEAT) and not text:
                if av[0] == 0:  # type: ignore[index]
                    continue  # an optional run before the opener (`^\s*<!--`)
                break
            elif op is c.SUBPATTERN and not text:
                return lead(list(av[-1]))  # type: ignore[index]
            elif op is c.BRANCH and not text:
                return [t for alt in av[1] for t in lead(list(alt))]  # type: ignore[index]
            else:
                break
        return [text] if text else []

    src = p.pattern if isinstance(p.pattern, str) else p.pattern.decode("latin-1")
    return list(dict.fromkeys(lead(list(parser.parse(src, p.flags & ~re.U)))))[:8]


def _units(p: re.Pattern) -> list[str]:
    src = p.pattern if isinstance(p.pattern, str) else p.pattern.decode("latin-1")
    words = _words(src)
    out = list(_UNITS)
    for w in words:
        out += [w + " ", w + "\n", w + "(", w + " {", w + "=[", w]
    for o in _openers(p):
        out += [o, o + "a", o + "a(", o + "a ", o + " ", "a" + o]
    return list(dict.fromkeys(out))


def _text(unit: str, n: int, as_bytes: bool) -> str | bytes:
    text = (unit * (n // len(unit) + 1))[:n]
    return text.encode() if as_bytes else text


def _cost(p: re.Pattern, text: str | bytes) -> float:
    t0 = time.perf_counter()
    for _ in p.finditer(text):
        pass
    p.search(text)
    p.match(text)
    return time.perf_counter() - t0


_ALL = _patterns()


def test_the_scan_finds_the_patterns() -> None:
    names = {name for name, _ in _ALL}
    assert len(_ALL) > 300
    assert "server.code.domain.extract.ts_reexports._IMPORT_RE" in names
    assert "server.code.app.index.sql_names._STATED_CLASS_RE" in names


@pytest.mark.parametrize("name,pattern", _ALL, ids=[n for n, _ in _ALL])
def test_a_pattern_costs_linear_time(name: str, pattern: re.Pattern) -> None:
    as_bytes = isinstance(pattern.pattern, bytes)
    slow = []
    for unit in _units(pattern):
        small = _cost(pattern, _text(unit, _N, as_bytes))
        if small > _BOUND_S:
            slow.append((unit, small, None))
            continue
        big = _cost(pattern, _text(unit, 4 * _N, as_bytes))
        if big > _BOUND_S or (big > _FLOOR_S and big > _RATIO * small):
            # Timers jitter on a loaded host: the best of three runs of each counts.
            for _ in range(2):
                small = min(small, _cost(pattern, _text(unit, _N, as_bytes)))
                big = min(big, _cost(pattern, _text(unit, 4 * _N, as_bytes)))
            if big > _BOUND_S or (big > _FLOOR_S and big > _RATIO * small):
                slow.append((unit, round(small, 3), round(big, 3)))
    assert not slow, f"{name}: (unit, N s, 4N s) {slow}"


# A run after a prefix the pattern spells: a pattern that splits a run of spaces k ways
# costs N**k, and one that searches on from each repeated keyword to a closer that never
# comes costs N**2. The sizes grow 4 times a step and a run stops at the first step that
# grows more than 8 times, so a cubic pattern stops at 8,000 characters, not hours later.
_SIZES = (500, 2_000, 8_000, 32_000)
_PREFIXES = ("a", "- a", "a:a", "a = a", "export default a", "'a", "# a")
_SEPS = ("", " a", "a=", "a(", "a ", "#a ")  # after an opener: `*a=`, `@if(`, `{#if `


def _composites(p: re.Pattern) -> list[tuple[str, str, str]]:
    """(prefix, the unit repeated after it, the end)."""
    src = p.pattern if isinstance(p.pattern, str) else p.pattern.decode("latin-1")
    words = _words(src)
    pairs = [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
    prefixes = list(_PREFIXES) + words + pairs + [w + " a" for w in words]
    prefixes.append(" ".join(words) + " a")
    shapes = [(pre, " ", end) for pre in prefixes for end in ("b", "\n", ";b")]
    units = [w + " a " for w in words] + [w + " a(" for w in words] + [",a(", ",a{"]
    units += [f"{a} {b} a(" for a, b in zip(words, words[1:], strict=False)]
    openers = _openers(p)
    shapes += [(o + sep, " ", end) for o in openers for sep in _SEPS for end in ("b", "\n")]
    units += [o + "a" for o in openers] + [o + "1(" for o in openers]  # `1` names no call
    keyed = [(o + "{", w + ":'a',", "b") for o in openers for w in words]
    return shapes + keyed + [(" ".join(words) + " a ", unit, "b") for unit in units]


def _slow(cost: Callable[[int], float]) -> tuple[int, float, float] | None:
    """(n, the time at n / 4, the time at n) where the text first grows over linear."""
    small = cost(_SIZES[0])
    for n in _SIZES[1:]:
        big = cost(n)
        if big > _BOUND_S or (big > _FLOOR_S / 10 and big > _RATIO * small):
            # Timers jitter on a loaded host: the best of three runs of each counts.
            small = min(small, cost(n // 4), cost(n // 4))
            big = min(big, cost(n), cost(n))
            if big > _BOUND_S or (big > _FLOOR_S / 10 and big > _RATIO * small):
                return n, round(small, 4), round(big, 4)
        small = big
    return None


@pytest.mark.parametrize("name,pattern", _ALL, ids=[n for n, _ in _ALL])
def test_a_run_after_a_prefix_costs_linear_time(name: str, pattern: re.Pattern) -> None:
    as_bytes = isinstance(pattern.pattern, bytes)
    slow = []
    for pre, unit, end in _composites(pattern):

        def cost(n: int, pre: str = pre, unit: str = unit, end: str = end) -> float:
            out = pre + (unit * (n // len(unit) + 1))[:n] + end
            return _cost(pattern, out.encode() if as_bytes else out)

        if (got := _slow(cost)) is not None:
            slow.append((pre, unit, end, *got))
    assert not slow, f"{name}: (prefix, unit, end, n, n/4 s, n s) {slow}"


# The lanes' own walks over a file: a slice, a count from the top or a rescan per line,
# heading or site is quadratic, and a backtracking match holds the GIL so a thread
# timer cannot stop it. Each walk runs in a child process that dumps its stack and exits
# when it overruns, so a regression fails here instead of hanging the suite.
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


def _inline_templates(n: int) -> str:
    return "".join(f"@Component({{template:`<a-b{i}>`}})class A{i}{{}}\n" for i in range(n // 40))


# (id, walk, the text of about n characters it walks)
_WALKS: list[tuple[str, Callable[[str], object], Callable[[int], str]]] = [
    ("markdown-heading-spaces", _markdown, _run("# a", " ", "b")),
    ("markdown-headings", _markdown, _run("", "# a\n")),
    ("markdown-links", _markdown, _links),
    (
        "markdown-long-parent-title",
        _markdown,
        lambda n: "# " + "a" * (n // 2) + "\n" + "## a\n" * (n // 10),
    ),
    ("setup-py-unclosed-lists", _setup_py, _run("", '"console_scripts":[')),
    ("setup-py-entry-points", _setup_py, _run('"console_scripts":[', '"a=b:c",\n', "]")),
    ("setup-py-roots", _py_layout("setup.py"), _numbered("find_packages('a{}')\n")),
    ("pyproject-roots", _py_layout("pyproject.toml"), _where),
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
    ("python-unclosed-add-url-rule", _py_routes, _run("", "a.add_url_rule('a'")),
    ("python-unclosed-route", _py_routes, _run("import starlette.routing\n", "Route('a'")),
    ("python-env-reads", _env, _run("", "os.environ['A']\n")),
]
_WALK_N = 100_000
_WALK_BOUND_S = 5.0  # a linear walk over 4N takes about a second here at most
_WALK_DEADLINE_S = 60


def _walk_cost(name: str) -> tuple[float, float]:
    """The best of three times of the walk at N and 4N characters."""
    walk, text = next((w[1], w[2]) for w in _WALKS if w[0] == name)

    def cost(n: int) -> float:
        t = text(n)
        t0 = time.perf_counter()
        walk(t)
        return time.perf_counter() - t0

    return min(cost(_WALK_N) for _ in range(3)), min(cost(4 * _WALK_N) for _ in range(3))


_WALK_CHILD = f"""
import faulthandler, json, sys
faulthandler.dump_traceback_later({_WALK_DEADLINE_S}, exit=True)
from tests.unit.test_code_regex_cost import _walk_cost
print(json.dumps(_walk_cost(sys.argv[1])))
"""


@pytest.mark.parametrize("name", [w[0] for w in _WALKS])
def test_a_lane_walk_costs_linear_time(name: str) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _WALK_CHILD, name],
        capture_output=True,
        text=True,
        timeout=_WALK_DEADLINE_S * 2,
        cwd=pathlib.Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr[-4000:]
    small, big = json.loads(done.stdout)
    assert big < _WALK_BOUND_S and (big < _FLOOR_S or big < _RATIO * small), (name, small, big)

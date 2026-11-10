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
its use does. The lanes' own walks over a file (`lane_walks.py`) are timed the same way, each in
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

from tests.unit.lane_walks import WALKS

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
_WALK_BOUND_S = 5.0  # well above a linear walk at 4N, far below a quadratic one
_WALK_DEADLINE_S = 60


_WALK_CHILD = f"""
import faulthandler, json, sys
faulthandler.dump_traceback_later({_WALK_DEADLINE_S}, exit=True)
from tests.unit.lane_walks import walk_cost
print(json.dumps(walk_cost(sys.argv[1])))
"""


@pytest.mark.parametrize("name", [w[0] for w in WALKS])
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


# The class guard: every lane parser in the ingest's registry, over generated shapes (runs
# of one character, repeated names, unclosed openers, entries times uses), each lane in a
# child process with a deadline and a memory limit, which also bound a listed shape. A new
# lane, or a walk added to one without a cost walk of its own, is timed here. `KNOWN_SLOW`
# holds the shapes still over linear, each a follow-up fix.
_REGISTRY_CHILD = f"""
import faulthandler, json, resource, sys
faulthandler.dump_traceback_later({_WALK_DEADLINE_S * 3}, exit=True)
resource.setrlimit(resource.RLIMIT_AS, (3 << 30, 3 << 30))
from tests.unit.lane_walks import registry_cost
print(json.dumps(registry_cost(sys.argv[1])))
"""


def _registered_lanes() -> list[str]:
    from server.code.app.index.ingest_parse import _PARSERS

    return sorted(_PARSERS)


def test_every_registered_lane_has_a_sample_that_reads_as_it() -> None:
    from server.code.app.index.ingest_parse import parse_for_ingest
    from tests.unit.lane_walks import KNOWN_SLOW, SAMPLES, SHAPES

    assert set(SAMPLES) == set(_registered_lanes())
    assert set(KNOWN_SLOW) <= set(SAMPLES)
    shapes = {name for name, _text in SHAPES}
    assert all(set(listed) <= shapes for listed in KNOWN_SLOW.values())
    for path, head, lang in SAMPLES.values():
        pf, _err = parse_for_ingest(path, (head + "a\n").encode())
        assert pf is not None and pf.lang == lang, path


@pytest.mark.parametrize("lang", _registered_lanes())
def test_a_registered_lane_costs_linear_time(lang: str) -> None:
    from tests.unit.lane_walks import KNOWN_SLOW

    done = subprocess.run(
        [sys.executable, "-c", _REGISTRY_CHILD, lang],
        capture_output=True,
        text=True,
        timeout=_WALK_DEADLINE_S * 6,
        cwd=pathlib.Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr[-4000:]
    from tests.unit.lane_walks import SHAPES

    known = KNOWN_SLOW.get(lang, ())
    timed = json.loads(done.stdout)
    # a shape that reads as another lane is that lane's: most must read as this one
    assert len(timed) >= len(SHAPES) // 2 and set(known) <= {t[0] for t in timed}, lang
    slow, linear, reparsed = {}, set(), {}
    for shape, small, big, parsed_small, parsed_big in timed:
        if big > _BOUND_S or (big > _FLOOR_S / 2 and big > _RATIO * small):
            slow[shape] = (round(small, 3), round(big, 3))
        if big < _FLOOR_S / 2 and big < 4 * small:
            linear.add(shape)
        if parsed_big > 6 * parsed_small + 1000:  # a 4N file is 4 times the bytes
            reparsed[shape] = (parsed_small, parsed_big)
    new = {shape: t for shape, t in slow.items() if shape not in known}
    assert not new and not reparsed, f"{lang}: (N s, 4N s) {new}; (N bytes, 4N bytes) {reparsed}"
    fixed = sorted(set(known) & linear)  # clearly linear now, not a timer's doubt
    assert not fixed, f"{lang}: {fixed} run in linear time; drop them from KNOWN_SLOW"

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
its use does."""

from __future__ import annotations

import importlib
import pkgutil
import re
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


def _units(p: re.Pattern) -> list[str]:
    src = p.pattern if isinstance(p.pattern, str) else p.pattern.decode("latin-1")
    words = _words(src)
    out = list(_UNITS)
    for w in words:
        out += [w + " ", w + "\n", w + "(", w + " {", w]
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
_PREFIXES = ("a", "- a", "a:a", "a = a", "export default a", "'a")


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
    return shapes + [(" ".join(words) + " a ", unit, "b") for unit in units]


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

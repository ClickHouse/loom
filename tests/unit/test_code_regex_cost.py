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
its use does. The lanes' own walks over a file (`lane_walks.py`) are judged by their growth
from N to 4N alone, with the cyclic collector off, each in a child process with a deadline.
A class guard runs every registered lane parser over generated shapes with the collector
off, in a child process with a deadline and a memory limit; `KNOWN_SLOW` lists the shapes
still to fix."""

from __future__ import annotations

import functools
import importlib
import itertools
import json
import pathlib
import pkgutil
import re
import subprocess
import sys
import time
import types
from collections.abc import Callable

import pytest

from tests.unit.code_fixtures import middle
from tests.unit.lane_walks import BOUND_S, LANE_FLOOR_S, RATIO, WALKS, lane_over

_PACKAGES = ("server.code.domain.extract", "server.code.app.index")
_N = 50_000
# Below this the timer's noise decides the ratio; a quadratic search at 4N = 200,000
# characters takes seconds, a linear one milliseconds.
_FLOOR_S = 0.1

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
    t0 = time.process_time()  # CPU time: a loaded host stretches wall time, not this
    for _ in p.finditer(text):
        pass
    p.search(text)
    p.match(text)
    return time.process_time() - t0


def _over(small: float, big: float, floor: float = _FLOOR_S / 10) -> bool:
    """Whether `big`, the time at 4 times `small`'s size, grows over linear: over the
    bound, or over `floor` (10 ms for a composite text) and over 8 times `small`."""
    return big > BOUND_S or (big > floor and big > RATIO * small)


_ALL = _patterns()


def test_the_scan_finds_the_patterns() -> None:
    names = {name for name, _ in _ALL}
    assert len(_ALL) > 300
    assert "server.code.domain.extract.ts_reexports._IMPORT_RE" in names
    assert "server.code.app.index.sql_names._STATED_CLASS_RE" in names


@pytest.mark.parametrize("name,pattern", _ALL, ids=[n for n, _ in _ALL])
def test_a_pattern_costs_linear_time(name: str, pattern: re.Pattern) -> None:
    as_bytes = isinstance(pattern.pattern, bytes)
    over = functools.partial(_over, floor=_FLOOR_S)
    slow = []
    for unit in _units(pattern):

        def cost(n: int, unit: str = unit) -> float:
            return _cost(pattern, _text(unit, n, as_bytes))

        small = cost(_N)
        if small > BOUND_S:
            slow.append((unit, small, None))
            continue
        pairs = [(small, cost(4 * _N))]
        if over(*pairs[0]):
            # Timers jitter on a loaded host: two more pairs, and `middle` reads one.
            pairs += [(cost(_N), cost(4 * _N)) for _ in range(2)]
            small, big = middle(pairs, over)
            if over(small, big):
                slow.append((unit, round(small, 3), round(big, 3)))
    assert not slow, f"{name}: (unit, N s, 4N s) {slow}"


def test_the_pattern_judge_reads_pairs_timed_back_to_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def passes(times: list[float]) -> bool:
        """Whether a pattern passes when its runs, N then 4N in turn, take `times`."""
        runs = iter(times)
        monkeypatch.setitem(globals(), "_units", lambda p: ["a"])
        monkeypatch.setitem(globals(), "_cost", lambda p, text: next(runs))
        try:
            test_a_pattern_costs_linear_time("p", re.compile("a"))
        except AssertionError:
            return False
        return next(runs, None) is None  # every run was asked for

    assert passes([0.05, 0.2])  # a pair under 8 times is not timed again
    assert passes([0.01, 0.09])  # nor one under 0.1 s at 4N
    # one N run twice as fast as the two after it: its pair reads 9 times, the others 4.5
    assert passes([0.05, 0.45, 0.1, 0.45, 0.1, 0.45])
    assert not passes([0.05, 0.8, 0.1, 1.6, 0.05, 0.8])  # 16 times on every pair
    assert not passes([0.05, 0.8, 0.15, 0.8, 0.05, 0.8])  # and with one N run 3 times as slow
    assert not passes([2.5])  # over the bound at N: 4N is not timed


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
    """(n, the time at n / 4, the time at n) where the text first grows over linear, from
    the pair that grew least.

    Each step times n / 4 and n back to back, so a load spike that lasts a few runs
    slows both alike; a step that looks over linear is timed twice more that way and
    fails only if all three pairs are over. A spike on the n / 4 of any one pair can hide
    a step; nothing is carried, so the next step, or the next run, reports it."""
    for n in _SIZES[1:]:
        pairs = [(cost(n // 4), cost(n))]
        if _over(*pairs[0]):
            pairs += [(cost(n // 4), cost(n)) for _ in range(2)]
            if all(_over(small, big) for small, big in pairs):
                small, big = min(pairs, key=lambda p: p[1] / max(p[0], 1e-9))  # the mildest
                return n, round(small, 4), round(big, 4)
    return None


def test_slow_tells_a_load_spike_from_a_step_over_linear() -> None:
    def linear(n: int) -> float:
        return n * 1e-7

    def spiked(base: Callable[[int], float], calls: range) -> Callable[[int], float]:
        """`base`, with calls numbered `calls` (from 0) taking 10 times as long."""
        count = itertools.count()
        return lambda n: base(n) * (10 if next(count) in calls else 1)

    assert _slow(linear) is None
    # 500 is timed in the first pair: calls 0 and 1 are 500 and 2,000, and so on
    assert _slow(spiked(linear, range(5, 8))) is None  # 32,000, 8,000, 32,000
    assert _slow(spiked(linear, range(5, 6))) is None  # 32,000 alone, then two clean pairs

    def quadratic(n: int) -> float:
        return (n / 1000) ** 2 * 1e-3

    assert _slow(quadratic) == (8_000, 0.004, 0.064)
    # every call after the 8,000 step's first n / 4 slowed: still over linear
    assert _slow(spiked(quadratic, range(3, 8))) == (8_000, 0.04, 0.64)
    # a spike on the 8,000 step's n / 4 hides that step; the 32,000 one reports it
    assert _slow(spiked(quadratic, range(2, 3))) == (32_000, 0.064, 1.024)
    assert _slow(lambda n: (n / 2000) ** 3 * 2.5e-4) == (8_000, 0.0003, 0.016)  # cubic


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
# when it overruns, so a regression fails here instead of hanging the suite. Only the
# growth from N to 4N is judged, since a linear walk at 4N takes seconds on a slow
# runner: a walk whose quadratic part at N is over half its linear part grows more
# than 8 times, and a purely quadratic one grows 16 times or runs into the deadline.
_WALK_DEADLINE_S = 60


_WALK_CHILD = f"""
import faulthandler, json, sys
faulthandler.dump_traceback_later({_WALK_DEADLINE_S}, exit=True)
from tests.unit.lane_walks import walk_cost
samples = []
costs = walk_cost(sys.argv[1], floor={_FLOOR_S}, diagnostics=samples)
print(json.dumps({{"costs": costs, "samples": samples}}))
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
    packet = json.loads(done.stdout)
    small, big = packet["costs"]
    assert big < _FLOOR_S or big < RATIO * small, json.dumps(
        {"name": name, **packet}, sort_keys=True
    )


# The class guard: every lane parser in the ingest's registry, over generated shapes (runs
# of one character, repeated names, unclosed openers, entries times uses), each lane in a
# child process with a deadline and a memory limit, which also bound a listed shape. A new
# lane, or a walk added to one without a cost walk of its own, is timed here; `judged_step`
# says which step a shape is judged on. `KNOWN_SLOW` holds the shapes still over linear,
# each a follow-up fix.
_REGISTRY_CHILD = f"""
import faulthandler, json, resource, sys
faulthandler.dump_traceback_later({_WALK_DEADLINE_S * 3}, exit=True)
resource.setrlimit(resource.RLIMIT_AS, (3 << 30, 3 << 30))
from tests.unit.lane_walks import registry_cost
diagnostics = {{}}
timed = registry_cost(sys.argv[1], diagnostics=diagnostics)
print(json.dumps({{"timed": timed, "diagnostics": diagnostics}}))
"""


def test_judged_step_tells_a_cache_step_from_growth_over_linear() -> None:
    from tests.unit.lane_walks import judged_step

    # a time, or a time and "other" (read as another lane) or "stopped"
    Run = float | tuple[float, str]

    def judged(times: dict[int, list[Run]], listed: bool = False) -> object:
        """(the step, whether it is over linear) for a shape whose runs at each size are
        the next of `times`, the last one repeated."""

        def cost(n: int) -> tuple[float, int, bool, bool]:
            run = times[n].pop(0) if len(times[n]) > 1 else times[n][0]
            t, how = run if isinstance(run, tuple) else (run, "")
            return t, n, how != "other", how != "stopped"

        got = judged_step(cost, 1, listed)
        return got and (got[0], lane_over(got[1], got[2]))

    def quadratic(*at_16n: Run) -> dict[int, list[Run]]:
        return {1: [0.01], 4: [0.12], 16: list(at_16n) or [1.9]}  # 12 times, then 16

    assert judged({1: [0.01], 4: [0.04]}) == ("N", False)  # not timed at 16N
    # a run that stopped early at N or 4N is timed as it ran
    assert judged({1: [(0.01, "stopped")], 4: [0.04]}) == ("N", False)
    assert judged({1: [0.01], 4: [(0.1, "stopped")], 16: [0.4]}) == ("4N", False)
    assert judged({1: [0.01], 4: [0.1], 16: [0.4]}) == ("4N", False)  # 10 times once, then 4
    # 9 times once, then 4 on the two pairs after
    assert judged({1: [0.01], 4: [0.1], 16: [0.9, 0.4]}) == ("4N", False)
    assert judged(quadratic()) == ("4N", True)
    # the bytes parsed come from the two sizes judged (the fake parses n bytes at n)
    got = judged_step(lambda n: ({1: 0.01, 4: 0.12, 16: 1.9}[n], n, True, True), 1, False)
    assert got is not None and got[3:] == (4, 16)
    got = judged_step(lambda n: ({1: 0.01, 4: 0.04}[n], n, True, True), 1, False)
    assert got is not None and got[3:] == (1, 4)
    # a 16N run that stopped early or read as another lane leaves the N to 4N step
    assert judged(quadratic((0.1, "stopped"))) == ("N, 16N unread", True)
    assert judged(quadratic((0.1, "other"))) == ("N, 16N unread", True)
    assert judged(quadratic(1.5, (0.1, "stopped"), 1.6)) == ("4N", True)  # a retry, too
    assert judged(quadratic(2.5, 0.1)) == ("4N", True)  # over 2 s: not timed again
    assert judged(quadratic(), listed=True) == ("N", True)
    assert judged({1: [0.01], 4: [0.16]}) == ("N", True)  # 16 times from N: not timed at 16N
    assert judged({1: [0.02], 4: [0.3], 16: [1.2]}) == ("N", True)  # over 0.25 s at 4N
    assert judged({1: [0.004], 4: [0.045]}) == ("N", False)  # under 50 ms at 4N: not at 16N
    assert judged({1: [1.5]}) == ("N", True)  # over 1 s at N: 4N is not timed
    assert judged({1: [(0.01, "other")]}) is None
    # One N run about twice as fast as the two after it, as timed on a runner: the first
    # pair reads 8 to 10 times, the other two under 5.
    for fast_n in (
        {1: [0.047, 0.086, 0.087], 4: [0.397, 0.396, 0.398]},
        {1: [0.058, 0.113, 0.106], 4: [0.498, 0.486, 0.492]},
        {1: [0.033, 0.065, 0.076], 4: [0.338, 0.319, 0.365]},
        {1: [0.011, 0.019, 0.019], 4: [0.091, 0.091, 0.100], 16: [(0.001, "stopped")]},
    ):
        assert judged(fast_n) == ("N", False)
    # the step from 4N to 16N likewise: the 4N runs timed next to the later 16N ones count
    fast_4n = {1: [0.01], 4: [0.1, 0.1, 0.1, 0.2], 16: [0.9, 0.85]}
    assert judged(fast_4n) == ("4N", False)
    # quadratic growth is 16 times on every pair, whichever run was fast
    assert judged({1: [0.01, 0.02, 0.02], 4: [0.32, 0.33, 0.34]}) == ("N", True)
    assert judged({1: [0.01], 4: [0.12, 0.12, 0.12, 0.24], 16: [1.9, 3.8]}) == ("4N", True)
    # one pair whose N run was 3 times as slow does not hide a quadratic step; two do
    assert judged({1: [0.02, 0.06, 0.02], 4: [0.32]}) == ("N", True)
    assert judged({1: [0.02, 0.06, 0.06], 4: [0.32]}) == ("N", False)
    # A pair is over by the class guard's whole test, not by its growth alone. One under
    # 50 ms at 4N is not over, though it grew most: two of the three are not.
    assert judged({1: [0.007, 0.003, 0.015], 4: [0.060, 0.045, 0.060]}) == ("N", False)
    # And one over 2 s at 16N is over, though it grew least: two of the three are.
    spiked = {1: [0.01], 4: [0.12, 0.12, 0.12, 0.12, 0.4], 16: [1.0, 0.95, 2.1]}
    assert judged(spiked) == ("4N", True)
    sizes: list[int] = []
    times = {1: 0.01, 4: 0.1, 16: 0.4}

    def counted(n: int) -> tuple[float, int, bool, bool]:
        sizes.append(n)
        return times[n], n, True, True

    judged_step(counted, 1, False)
    # each pair back to back; a 16N run that looks linear is timed once
    assert sizes == [1, 4, 1, 4, 1, 4, 16]
    sizes.clear()
    times.update({4: 0.12, 16: 1.9})
    judged_step(counted, 1, False)
    assert sizes == [1, 4, 1, 4, 1, 4, 16, 4, 16, 4, 16]


def test_a_lane_parse_that_fails_is_not_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import ingest_parse
    from tests.unit.lane_walks import _extract

    def fails(path: str, source: bytes) -> object:
        raise ValueError("no parse")

    monkeypatch.setitem(ingest_parse._PARSERS, "hcl", fails)
    diagnostics: dict[str, str] = {}
    pf, finished = _extract("a.tf", b"a = 1\n", diagnostics=diagnostics)
    # reads as the lane, but its parse failed
    assert getattr(pf, "lang", "") == "hcl" and not finished
    assert diagnostics == {"parse": "parse: ValueError"}


def test_lane_walk_diagnostics_keep_samples_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import ingest_chunks, ingest_parse
    from server.code.domain.extract import edges_treesitter
    from tests.unit.lane_walks import _extract

    def parse(path: str, source: bytes) -> tuple[object, str]:
        return types.SimpleNamespace(lang="python", tree=object(), source=source), ""

    def chunks(pf: object, path: str, source: bytes, limit: int) -> None:
        if source == b"bad":
            raise ValueError("walk failed")

    edges = []
    monkeypatch.setattr(ingest_parse, "parse_for_ingest", parse)
    monkeypatch.setattr(ingest_chunks, "_chunks_for", chunks)
    monkeypatch.setattr(
        edges_treesitter, "extract_edges_and_misses", lambda pf, *_: edges.append(pf)
    )
    failed: dict[str, str] = {}
    succeeded: dict[str, str] = {}
    bad, finished_bad = _extract("a.py", b"bad", diagnostics=failed)
    good, finished_good = _extract("a.py", b"good", diagnostics=succeeded)
    assert not finished_bad and finished_good
    assert failed == {"chunks": "ValueError"} and succeeded == {}
    assert getattr(bad, "tree", None) is not None and getattr(good, "tree", None) is not None
    assert edges == [bad, good]


def test_a_parse_error_a_walk_swallows_is_not_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.domain.extract import bounded_parse
    from tests.unit import lane_walks

    def raises(parser: object, source: bytes) -> object:
        raise bounded_parse.ParseTimeout("deadline")

    def extract(
        path: str, source: bytes, *, diagnostics: dict[str, str] | None = None
    ) -> tuple[object, bool]:
        try:
            bounded_parse.parse(None, source)  # a walk that parses again and swallows it
        except bounded_parse.ParseTimeout:
            pass
        return types.SimpleNamespace(lang="hcl"), True

    runs: list[tuple[float, int, bool, bool]] = []

    def judged(cost: Callable[[int], tuple[float, int, bool, bool]], n: int, listed: bool) -> None:
        runs.append(cost(n))

    monkeypatch.setattr(bounded_parse, "parse", raises)
    monkeypatch.setattr(lane_walks, "_extract", extract)
    monkeypatch.setattr(lane_walks, "judged_step", judged)
    monkeypatch.setattr(lane_walks, "SHAPES", [("one", lambda n: "a\n" * n)])
    diagnostics: dict[str, list[dict[str, object]]] = {}
    lane_walks.registry_cost("hcl", diagnostics=diagnostics)
    assert [(lane, finished) for _t, _b, lane, finished in runs] == [(True, False)]
    assert len(diagnostics["one"]) == 1
    sample = diagnostics["one"][0]
    assert sample["n"] == lane_walks._REGISTRY_N
    assert sample["lane"] is True and sample["finished"] is False
    assert sample["errors"] == {"native_parse": "ParseTimeout"}


def _registered_lanes() -> list[str]:
    from server.code.app.index.ingest_parse import _PARSERS

    return sorted(_PARSERS)


def test_every_registered_lane_has_a_sample_that_reads_as_it() -> None:
    from server.code.app.index.ingest_parse import parse_for_ingest
    from tests.unit.lane_walks import KNOWN_SLOW, SAMPLES, SHAPES

    assert set(SAMPLES) == set(_registered_lanes()), "a lane needs a SAMPLES entry in lane_walks.py"
    assert set(KNOWN_SLOW) <= set(SAMPLES)
    shapes = {name for name, _text in SHAPES}
    assert all(set(listed) <= shapes for listed in KNOWN_SLOW.values())
    for path, head, lang in SAMPLES.values():
        pf, _err = parse_for_ingest(path, (head + "a\n").encode())
        assert pf is not None and pf.lang == lang, path


@pytest.mark.parametrize("lang", _registered_lanes())
def test_a_registered_lane_costs_linear_time(lang: str) -> None:
    from tests.unit.lane_walks import KNOWN_SLOW, SHAPES

    done = subprocess.run(
        [sys.executable, "-c", _REGISTRY_CHILD, lang],
        capture_output=True,
        text=True,
        timeout=_WALK_DEADLINE_S * 6,
        cwd=pathlib.Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr[-4000:]
    known = KNOWN_SLOW.get(lang, ())
    observed = json.loads(done.stdout)
    timed = observed["timed"]
    # a shape that reads as another lane is that lane's: at least half must read as this one
    assert len(timed) >= len(SHAPES) // 2 and set(known) <= {t[0] for t in timed}, lang
    slow, linear, reparsed = {}, set(), {}
    for shape, step, small, big, parsed_small, parsed_big in timed:
        if lane_over(small, big):
            slow[shape] = (step, round(small, 3), round(big, 3))
        if big < LANE_FLOOR_S and big < 4 * small:
            linear.add(shape)
        if parsed_big > 6 * parsed_small + 1000:  # the larger file is 4 times the bytes
            reparsed[shape] = (step, parsed_small, parsed_big)
    new = {shape: t for shape, t in slow.items() if shape not in known}
    failed_samples = {shape: observed["diagnostics"][shape] for shape in set(new) | set(reparsed)}
    assert not new and not reparsed, (
        f"{lang}, from the step's smaller size (N or 4N) to 4 times it: (step, s, s) {new}; "
        f"(step, bytes, bytes) {reparsed}; samples: {failed_samples}; "
        f"child diagnostics:\n{done.stderr[-4000:]}"
    )
    fixed = sorted(set(known) & linear)  # clearly linear now, not a timer's doubt
    assert not fixed, f"{lang}: {fixed} run in linear time; drop them from KNOWN_SLOW"

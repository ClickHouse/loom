"""Coverage truth for a change: where a test belongs, and whether a symbol, path or
setting is already exercised.

ClickHouse maintainers reject two things more than anything else a bot
files: a new stateless test file when the suite already has the right one
("move this to 02994_sanity_check_settings.sql"), and a coverage test for
something already covered ("I believe it's already tested in 03468_…").
Both rest on a claim about the test corpus, and a claim that "nothing
covers this" is a zero — indistinguishable from a probe that measured
nothing. So every coverage claim here carries a positive control: the same
read is run for a symbol known to be covered, and when the control comes
back empty the verdict is `unknown`, never `uncovered`.

Pure helpers live here (fingerprints, scoring, verdicts); the reads are the
code repository's (`grep_files` over the indexed tests tree, the test map
with its evidence classes, paths by stem).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

STATELESS = "tests/queries/0_stateless/"
# The stateful tree. On current ClickHouse master it is empty (its files moved into
# 0_stateless), but a namespace indexed at an older commit, a release branch or a fork
# still holds it, and a test there is a test.
STATEFUL = "tests/queries/1_stateful/"
INTEGRATION = "tests/integration/"
TEST_PREFIXES = (STATELESS, STATEFUL, INTEGRATION)

# Verdicts a caller acts on. `unknown` is silence: the probe did not measure.
ADD_CASE_TO = "add_case_to"
NEW_FILE_JUSTIFIED = "new_file_justified"
COVERED_BY = "covered_by"
# Tests were found, but none names the subject itself — a short-method grep (`readRow`,
# `startup`) or a path convention is all there is. Stated only when the control passed,
# like `uncovered`.
WEAK_EVIDENCE = "weak_evidence"
UNCOVERED = "uncovered"
UNKNOWN = "unknown"

# Evidence tiers, strongest first. A row's tier says what the evidence is; `covered_by`
# needs one of the strong tiers.
EXECUTED = "executed"  # an instrumented run measured the test executing the subject's lines
EXACT_SCOPED = "exact_scoped"  # a test names the qualified symbol or its SQL-level name
PR_COOCCURRENCE = "pr_cooccurrence"  # the test file changed in a PR with the source file
SHORT_NAME_GREP = "short_name_grep"  # a test contains the bare method name
FILE_REFERENCE = "file_reference"  # a test names another function the source file registers
PATH_CONVENTION = "path_convention"  # the test's name mirrors the source area
TIER_ORDER = (
    EXECUTED,
    EXACT_SCOPED,
    PR_COOCCURRENCE,
    SHORT_NAME_GREP,
    FILE_REFERENCE,
    PATH_CONVENTION,
)
STRONG_TIERS = frozenset({EXECUTED, EXACT_SCOPED, PR_COOCCURRENCE})

# A test FILE, not a fixture: stateless .sql/.sh/.py/.j2/.expect, integration
# test modules. Configs, reference files and helpers are never a home and
# never coverage.
_STATELESS_FILE_RE = re.compile(
    r"^tests/queries/[01]_state(?:less|ful)/\d{5}_[^/]+\.(sql|sh|py|j2|expect)$"
)
_INTEGRATION_FILE_RE = re.compile(r"^tests/integration/test_[^/]+/(test[^/]*\.py)$")
# How much a matched part says about the home. An engine name is in
# thousands of tests; a setting, a source stem or a symbol is specific.
PART_WEIGHT = {"setting": 1.0, "stem": 1.0, "symbol": 1.0, "engine": 0.4}

_SETTING_RE = re.compile(r"^[a-z][a-z0-9_]{2,80}$")
_ENGINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{2,60}$")
_STEM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,80}$")


@dataclass(frozen=True)
class Fingerprint:
    """A change's dedup fingerprint: file stems, settings, engine class."""

    stems: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()
    engine: str = ""
    symbols: tuple[str, ...] = ()

    @property
    def parts(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = [("stem", s) for s in self.stems]
        out += [("setting", s) for s in self.settings]
        if self.engine:
            out.append(("engine", self.engine))
        out += [("symbol", s) for s in self.symbols]
        return out


def normalize_fingerprint(
    stems: list[str], settings: list[str], engine: str, symbols: list[str]
) -> Fingerprint:
    """Drop malformed parts (a stem with a path, a setting with spaces, an
    engine with parameters) so every grep pattern is a literal identifier."""

    def _stem(raw: str) -> str:
        return raw.strip().rsplit("/", 1)[-1].split(".")[0]

    st = tuple(dict.fromkeys(_stem(s) for s in stems if _STEM_RE.match(_stem(s) or "")))
    se = tuple(dict.fromkeys(s.strip() for s in settings if _SETTING_RE.match(s.strip())))
    eng = engine.strip().split("(")[0]
    eng = eng if _ENGINE_RE.match(eng) else ""
    sy = tuple(
        dict.fromkeys(s.strip() for s in symbols if _STEM_RE.match(s.strip().rsplit("::", 1)[-1]))
    )
    return Fingerprint(stems=st[:10], settings=se[:10], engine=eng, symbols=sy[:10])


def grep_pattern(kind: str, value: str) -> str:
    """The literal a test file must contain to count as matching a part.
    A setting appears as `SET name` / `name = ` / `--name`; the bare name is
    the common substring. An engine appears as `ENGINE = Name`; a symbol or
    stem by its bare identifier."""
    if kind == "engine":
        return f"ENGINE = {value}"
    if kind == "symbol":
        return value.rsplit("::", 1)[-1]
    return value


def is_test_file(path: str) -> bool:
    return bool(_STATELESS_FILE_RE.match(path) or _INTEGRATION_FILE_RE.match(path))


_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
# CamelCase pieces that name nothing about the area.
_GENERIC_TOKENS = {
    "merge",
    "tree",
    "storage",
    "storages",
    "src",
    "impl",
    "base",
    "data",
    "info",
    "set",
    "get",
    "utils",
    "common",
    "core",
    "function",
    "functions",
}


def stem_tokens(stems: tuple[str, ...]) -> set[str]:
    """`MergeTreeIndexText` → {"text", "index"}: the words a suite file's
    name would carry for the same area (a stateless file is
    `NNNNN_text_index_…`). Generic pieces are dropped."""
    out: set[str] = set()
    for stem in stems:
        for tok in _CAMEL_RE.findall(stem):
            t = tok.lower()
            if len(t) >= 3 and t not in _GENERIC_TOKENS:
                out.add(t)
    return out


@dataclass
class HomeCandidate:
    path: str
    matched: list[str] = field(default_factory=list)
    map_confidence: float = 0.0
    chunks: int = 0
    # stem words the file NAME carries (02346_text_index_… for MergeTreeIndexText)
    name_tokens: int = 0

    @property
    def weight(self) -> float:
        return sum(PART_WEIGHT.get(m.split(":", 1)[0], 1.0) for m in self.matched)

    @property
    def score(self) -> float:
        # Specific parts dominate; a file whose name carries the area's words
        # and the test map break ties; a bigger file is a slightly better home.
        return (
            self.weight
            + min(self.name_tokens, 3) * 0.15
            + min(self.map_confidence, 0.9) * 0.3
            + min(self.chunks, 50) / 500.0
        )


def rank_homes(
    parts: list[tuple[str, str]],
    hits_by_part: dict[tuple[str, str], list[dict[str, Any]]],
    test_map: list[dict[str, Any]],
    *,
    limit: int,
) -> list[HomeCandidate]:
    """Test files by the weight of the fingerprint parts they contain, then
    by the test map's confidence. `hits_by_part` is complete per part (the
    handler resolves a truncated part by membership over the candidates of
    the rarest part), so an intersection here is exact."""
    cands: dict[str, HomeCandidate] = {}
    for part in parts:
        for h in hits_by_part.get(part, []):
            path = str(h.get("path") or "")
            if not is_test_file(path):
                continue
            c = cands.setdefault(path, HomeCandidate(path=path))
            label = f"{part[0]}:{part[1]}"
            if label not in c.matched:
                c.matched.append(label)
            c.chunks = max(c.chunks, int(h.get("chunks") or 0))
    for t in test_map:
        path = str(t.get("test_path") or "")
        if not is_test_file(path):
            continue
        c = cands.setdefault(path, HomeCandidate(path=path))
        c.map_confidence = max(c.map_confidence, float(t.get("confidence") or 0.0))
    words = stem_tokens(tuple(v for k, v in parts if k == "stem"))
    if words:
        for c in cands.values():
            name = c.path.rsplit("/", 1)[-1].lower()
            c.name_tokens = sum(1 for w in words if w in name)
    ordered = sorted(cands.values(), key=lambda c: (-c.score, c.path))
    return ordered[:limit]


def home_verdict(
    parts: list[tuple[str, str]],
    ranked: list[HomeCandidate],
    *,
    control_ok: bool,
    live_parts: int | None = None,
) -> str:
    """`add_case_to` when the best candidate carries at least one specific
    part (setting, stem, symbol) and, when two or more parts matched ANY
    test at all, at least two parts in all; `new_file_justified` when the
    corpus was measurable and nothing matches that well; `unknown` when the
    positive control failed. A part no test contains (a source file stem —
    tests name settings and functions, not `MergeTreeIndexText`) does not
    raise the bar; an engine match alone never names a home: every MergeTree
    test has it."""
    if not control_ok:
        return UNKNOWN
    if not parts:
        return UNKNOWN
    if not ranked:
        return NEW_FILE_JUSTIFIED
    best = ranked[0]
    specific = [m for m in best.matched if not m.startswith("engine:")]
    if not specific:
        return NEW_FILE_JUSTIFIED
    alive = len(parts) if live_parts is None else live_parts
    need = min(2, max(alive, 1))
    return ADD_CASE_TO if len(best.matched) >= need else NEW_FILE_JUSTIFIED


def tier_of_method(method: str, *, scoped: bool = False) -> str:
    """The tier one test-map method or grep hit stands for. `scoped` says the
    evidence names the subject itself (its SQL name or qualified form), which
    lifts a content reference or a grep to `exact_scoped`; an unscoped content
    reference names another function of the same file."""
    if method == "coverage":
        return EXECUTED
    if method in {"include_graph"}:
        return EXACT_SCOPED
    if method == "content_reference":
        return EXACT_SCOPED if scoped else FILE_REFERENCE
    if method == "grep":
        return EXACT_SCOPED if scoped else SHORT_NAME_GREP
    if method == "pr_cooccurrence":
        return PR_COOCCURRENCE
    if method == "path_convention":
        return PATH_CONVENTION
    return PATH_CONVENTION


def strongest(tiers: list[str]) -> str:
    """The strongest tier among `tiers`, '' for none."""
    present = set(tiers)
    for t in TIER_ORDER:
        if t in present:
            return t
    return ""


def row_tier(row: dict[str, Any]) -> str:
    """A merged evidence row's tier: its own `tier` when the reader set one,
    else derived from its methods."""
    t = str(row.get("tier") or "")
    if t:
        return t
    methods = [str(m) for m in (row.get("methods") or [])]
    if not methods and row.get("method"):
        methods = [str(row["method"])]
    return strongest([tier_of_method(m) for m in methods])


def coverage_verdict(
    rows: list[dict[str, Any]], *, control_ok: bool, measured_unexecuted: bool = False
) -> str:
    """`covered_by` needs a row a run measured executing the subject (executed), whose
    evidence names the subject (exact_scoped), or a test file that changed with the
    source in a PR (pr_cooccurrence); rows of weaker tiers alone are `weak_evidence`;
    none is `uncovered`; and nothing but `unknown` when the positive control failed.
    `measured_unexecuted` (a coverage run executed none of the subject's lines) turns
    `weak_evidence` into `uncovered`: a test that only mentions the file's names was
    measured not running the subject."""
    if not control_ok:
        return UNKNOWN
    if not rows:
        return UNCOVERED
    if any(row_tier(r) in STRONG_TIERS for r in rows):
        return COVERED_BY
    return UNCOVERED if measured_unexecuted else WEAK_EVIDENCE


def measured_execution(
    spans: dict[str, list[tuple[int, int]]], files: dict[str, list[tuple[int, int]]]
) -> dict[str, Any]:
    """What a coverage run says about a subject: `measured` when the run covered one
    of the subject's files, `executed` when any test executed a line of its span there,
    and the executed ranges inside the span per path. `spans` is path -> the subject's
    line spans (a path subject passes its whole file as [(1, max)]); `files` is path ->
    the run's executed ranges."""
    measured = [p for p in spans if p in files]
    hits: dict[str, list[list[int]]] = {}
    for p in measured:
        for start, end in spans[p]:
            for a, b in files[p]:
                if a <= end and start <= b:
                    hits.setdefault(p, []).append([max(a, start), min(b, end)])
    return {"measured": bool(measured), "executed": bool(hits), "paths": measured, "hits": hits}


def epistemic_of(rows: list[dict[str, Any]]) -> str:
    """`exact` when some test names the subject textually or the include
    graph reaches it; `lower_bound` when only mined co-occurrence or a path
    convention says so."""
    methods = {str(r.get("method") or r.get("why") or "") for r in rows}
    # A `calls` edge from a test unit, or a test function named for the subject, is
    # exact evidence too
    if methods & {
        "coverage",
        "content_reference",
        "include_graph",
        "grep",
        "calls_edge",
        "test_name",
    }:
        return "exact"
    return "lower_bound"


__all__ = [
    "ADD_CASE_TO",
    "COVERED_BY",
    "EXACT_SCOPED",
    "EXECUTED",
    "FILE_REFERENCE",
    "Fingerprint",
    "HomeCandidate",
    "INTEGRATION",
    "NEW_FILE_JUSTIFIED",
    "PATH_CONVENTION",
    "PR_COOCCURRENCE",
    "SHORT_NAME_GREP",
    "STATEFUL",
    "STATELESS",
    "STRONG_TIERS",
    "TEST_PREFIXES",
    "TIER_ORDER",
    "UNCOVERED",
    "UNKNOWN",
    "WEAK_EVIDENCE",
    "PART_WEIGHT",
    "coverage_verdict",
    "measured_execution",
    "epistemic_of",
    "grep_pattern",
    "home_verdict",
    "is_test_file",
    "normalize_fingerprint",
    "rank_homes",
    "row_tier",
    "stem_tokens",
    "strongest",
    "tier_of_method",
]

"""The post-fusion rank priors, deterministic and LLM-free: the fusion signals
(path noise, stem affinity, embedded identifiers, file coherence, the fan-in
prior, the settings-catalogue penalty), the chunk-kind prior, the outcome boost
and the personal tie-break. Each multiplies a relevance score by a bounded
factor; none decides abstention."""

from __future__ import annotations

import math
import re
from typing import Any

from server.code.app import fan_in as code_fan_in
from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.idents import embedded_idents
from server.code.domain.units import CodeUnit
from server.core.datetimes import naive_utc_now
from server.settings import settings


def _catalogue_penalty(work: dict[str, Any], *, prose: bool) -> float:
    """The fusion-signal multiplier for a settings-catalogue unit on this question —
    `code_search_catalogue_penalty` for the kinds in `code_search_catalogue_kinds`, else
    1.0 (untouched)."""
    from server.code.domain.index.question_kind import kind_allowed

    pen = float(settings.code_search_catalogue_penalty)
    if (
        pen == 1.0
        or not prose
        or not kind_allowed(
            str(work.get("question_kind") or "other"),
            settings.code_search_catalogue_kinds,
        )
    ):
        return 1.0
    work["catalogue_penalty_milli"] = int(round(pen * 1000))
    return pen


def _user_tiebreak(units: list[CodeUnit], recent_dirs: list[str]) -> tuple[list[CodeUnit], bool]:
    """Stable-sort adjacent near-ties (<5% relative score gap) toward paths
    sharing a directory prefix with the user's recent work. Never reorders
    across a real score gap."""
    applied = False
    out = list(units)
    i = 0
    while i < len(out) - 1:
        a, b = out[i], out[i + 1]
        gap = abs(a.score - b.score) / max(abs(a.score), 1e-9)
        if gap < 0.05:
            a_match = _dir_match(a.path, recent_dirs)
            b_match = _dir_match(b.path, recent_dirs)
            if b_match and not a_match:
                out[i], out[i + 1] = b, a
                applied = True
                i += 2
                continue
        i += 1
    return out, applied


def _dir_match(path: str, recent_dirs: list[str]) -> bool:
    parent = path.rsplit("/", 1)[0] if "/" in path else path
    return any(parent.startswith(d) or d.startswith(parent) for d in recent_dirs)


# ---- fusion rank priors: post-fusion path / definition signals that lift
# recall@10 over plain RRF, all deterministic and LLM-free. Path-noise prior:
# test/example/fuzzer code is indexed on purpose (agents ask "how is X tested"), but for
# a conceptual question the implementation should outrank its tests — demote, never
# drop.
_NOISE_DIR_RE = re.compile(r"(?:^|/)(?:tests?|examples?|fuzzers|benchmarks)(?:/|$)")


_NOISE_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+_(?:test|fuzz))\.(?:h|hpp|cpp|cc|cxx)$")


# 0.35 with tests/ and ci/ in the corpus: a grid on the C++ conceptual fixture (penalty
# {0.5, 0.35, 0.25}) read r@5 0.741/0.769/0.769, r@10 0.801/0.810/0.810 — knee at 0.35,
# recovering most of the dilution the larger corpus caused (0.778 without tests/ and
# ci/). Test-intent queries skip this penalty, so the multilang lane is unaffected
# (0.500 either way).
_NOISE_PENALTY = 0.35


# Settings catalogues — one definition per setting, every domain word of the repository
# on one file. For a responsibility question they are the nearest text and never the
# mechanism.
_CATALOGUE_FILE_RE = re.compile(
    r"(?:^|/)(?:Settings|SettingsChangesHistory|ServerSettings|MergeTreeSettings|"
    r"FormatFactorySettings|SettingsEnums|SettingsObsoleteMacros)\.(?:cpp|h)$"
)


# Prose files — documentation, release notes, changelogs — by extension or by a
# changelog-style name, never by a directory word (`src/Coordination/Changelog.cpp`
# is code). A bug report's words are the ones the release notes repeat, so prose
# outranks the source it describes: asked with public bug reports, a prose file
# ranked first on a quarter of them although every fix changed only source.
_PROSE_EXT_RE = re.compile(r"\.(?:md|mdx|markdown|rst|adoc|txt)$", re.I)
_PROSE_NAME_RE = re.compile(
    r"(?:^|/)(?:CHANGELOG|CHANGES|HISTORY|NEWS|RELEASE[-_]?NOTES)(?:\.[a-z]+)?$", re.I
)
_CODE_EXT_RE = re.compile(r"\.(?:py|pyi|h|hpp|c|cc|cpp|cxx|go|rs|ts|tsx|js|jsx|java|kt|sql|sh)$")

# Query tokens that mean the asker wants documentation; the prose penalty skips them.
_DOC_INTENT_TOKENS = frozenset(
    {
        "doc",
        "docs",
        "documentation",
        "documented",
        "readme",
        "changelog",
        "release",
        "releases",
        "guide",
        "tutorial",
        "manual",
    }
)


def is_prose_path(path: str) -> bool:
    """True for a documentation or release-notes file (`CMakeLists.txt` is build code)."""
    if path.rsplit("/", 1)[-1].lower() == "cmakelists.txt":
        return False
    if _PROSE_EXT_RE.search(path):
        return True
    return bool(_PROSE_NAME_RE.search(path)) and not _CODE_EXT_RE.search(path)


def _prose_penalty(*, eligible: bool) -> float:
    """The fusion-signal multiplier for a prose file on this question:
    `code_search_prose_penalty` unless the search is constrained to paths or is a
    symbol lookup (the caller chose what to read), else 1.0."""
    pen = float(settings.code_search_prose_penalty)
    return pen if eligible else 1.0


# Stem affinity: rank-time prior for query keywords matching the file stem /
# parent directory. The BM25 leg already indexes path tokens, but diluted
# among content lexemes; this rewards the path match at ranking strength.
_STEM_BOOST_CAP = 0.25  # x(1+cap) at full keyword<->path overlap


_QUERY_STOPWORDS = frozenset(
    "the a an and or of in on for to is are was does do how what when where "
    "why which who with that this its from into code files".split()
)


# File coherence: several independent chunks of one file scoring means the
# FILE is about the query; concentrate that evidence on its best chunk
# (the mirror image of _diversify, which spreads slots across units).
_COHERENCE_FRAC = 0.25  # best chunk of the strongest file gains <=25% of max


# Embedded-symbol affinity: an NL query that names an identifier inline ("how does PipelineExecutor
# schedule ...") should prefer units BELONGING to that symbol (u.symbol is the unit's qualified
# scope — real metadata, not a content regex). Measured ~neutral on the n=36 bench (the lexical leg
# + stem affinity already cover the common case); kept half-strength for corpora/configs where path
# tokens are weaker lexical evidence.
_EMBEDDED_SYMBOL_BOOST = 0.30  # x(1+boost) when the unit's scope matches


# Query tokens that mean the asker WANTS test/CI code. The path-noise penalty exists
# because test files drown out implementation answers for implementation questions — but
# with tests/ and ci/ indexed, a question ABOUT the tests would have its best candidates
# halved (on the multilang lane, r@10 goes 1/5 -> 4/5 with the penalty disabled for
# these queries).
_TEST_INTENT_TOKENS = frozenset(
    {
        "test",
        "tests",
        "testing",
        "pytest",
        "fixture",
        "fixtures",
        "integration",
        "stateless",
        "regression",
        "flaky",
        "harness",
        "suite",
        "ci",
        "workflow",
        "workflows",
        "praktika",
        "runner",
        "pipeline",
    }
)


def _apply_fusion_signals(
    ranked: list[CodeUnit],
    query: str,
    *,
    prior_cap: float = 0.0,
    catalogue_penalty: float = 1.0,
    prose_penalty: float = 1.0,
) -> list[CodeUnit]:
    """Path-noise penalty, stem affinity, embedded-identifier boost, file coherence —
    and, when `prior_cap` > 0, the fan-in prior: x(1 + cap * log1p(fan_in) / log1p(max
    fan_in in the pool)), read from CodeUnit.fan_in, skipped under test intent and on
    noise paths (the same two exemptions as the penalty), never applied to a unit
    without callers. Calibration ran before this on the raw fusion, so abstention is
    unaffected. Default cap 0 = the prior is not computed."""
    if not ranked:
        return ranked
    q_tokens = frozenset(code_tokens(query))
    test_intent = bool(q_tokens & _TEST_INTENT_TOKENS)
    if q_tokens & _DOC_INTENT_TOKENS:
        prose_penalty = 1.0
    keywords = frozenset(t for t in q_tokens if len(t) > 2 and t not in _QUERY_STOPWORDS)
    embedded = embedded_idents(query)
    max_fan_in = max((u.fan_in for u in ranked), default=0) if prior_cap > 0 else 0
    path_parts: dict[str, frozenset[str]] = {}
    scored: list[tuple[CodeUnit, float]] = []
    for u in ranked:
        s = u.score
        noisy = bool(_NOISE_DIR_RE.search(u.path) or _NOISE_FILE_RE.search(u.path))
        if not test_intent and noisy:
            s *= _NOISE_PENALTY
        if catalogue_penalty != 1.0 and _CATALOGUE_FILE_RE.search(u.path):
            s *= catalogue_penalty
        if prose_penalty != 1.0 and is_prose_path(u.path):
            s *= prose_penalty
        if max_fan_in and u.fan_in > 0 and not test_intent and not noisy:
            s *= code_fan_in.prior_factor(u.fan_in, max_fan_in, prior_cap)
        if keywords:
            parts = path_parts.get(u.path)
            if parts is None:
                head, _, name = u.path.rpartition("/")
                stem = name.rsplit(".", 1)[0]
                parent = head.rsplit("/", 1)[-1]
                parts = frozenset(code_tokens(f"{parent} {stem}"))
                path_parts[u.path] = parts
            hits = sum(1 for k in keywords if k in parts)
            if hits:
                s *= 1.0 + _STEM_BOOST_CAP * hits / len(keywords)
        if embedded and u.symbol:
            comps = {c.lstrip("~").lower() for c in u.symbol.split("::")}
            if comps & embedded:
                s *= 1.0 + _EMBEDDED_SYMBOL_BOOST
        scored.append((u, s))

    # coherence over the penalty/affinity-adjusted scores, so a saturated
    # test file cannot buy its way back in through chunk count
    max_s = max(s for _, s in scored)
    file_sum: dict[str, float] = {}
    best_idx: dict[str, int] = {}
    for i, (u, s) in enumerate(scored):
        file_sum[u.path] = file_sum.get(u.path, 0.0) + s
        if u.path not in best_idx or s > scored[best_idx[u.path]][1]:
            best_idx[u.path] = i
    max_file_sum = max(file_sum.values()) or 1.0

    out: list[CodeUnit] = []
    for i, (u, s) in enumerate(scored):
        if best_idx[u.path] == i:
            s += _COHERENCE_FRAC * max_s * (file_sum[u.path] / max_file_sum)
        out.append(u.model_copy(update={"score": round(s, 6)}))
    out.sort(key=lambda u: -u.score)
    return out


# ---- chunk-kind prior -------------------------------------
# The index carries five chunk kinds (chunker.py): function, class_skeleton,
# comment_block, file_header, other. A file_header is the include block and the
# namespace opener — it names every type the file touches, so for a conceptual question
# it ranks on vocabulary it merely mentions; the function that implements the mechanism
# ranks on the same words and adds nothing the header lacks except the answer. On a
# large namespace: function 852k chunks, other 653k, file_header 119k, class_skeleton
# 100k. The prior multiplies the fused, signal-adjusted score by a per-kind factor. It
# is multiplicative and bounded because uncapped additive structural boosts distort a
# ranking (a term worth more than the usable score band lifts every row that carries it
# over every row that does not). The strongest demotion is x0.6 and the strongest
# promotion x1.15, both inside the fusion-signal band. Gates, in order:
#   - the query names a file or a path ("src/IO/WriteBufferFromS3.cpp",
#     "parseQuery.cpp:120"): the asker wants that file's chunks whatever
#     their kind — no prior;
#   - test intent (see _TEST_INTENT_TOKENS): test bodies are mostly `other`
#     chunks (SQL, shell, pytest functions the C++ chunker never sees as
#     functions) — no prior, as with the path-noise penalty;
#   - structural phrasing ("the class that ...", "struct holding ...",
#     "function that ...", "method ..."): the asker named a kind; that kind
#     gets x(1 + _STRUCTURAL_KIND_BOOST) and the table is not applied — the
#     two would otherwise stack on functions and cancel on classes.
# Tables: "default" is the design's table; "flat" halves every distance from 1.0; "structural" is
# the identity table with only the phrasing rule. `flat` is the served table;
# CodeSearchInput.kind_prior overrides per request for A/Bs. The kind Enum8 is
# already selected by both legs: no SQL change, zero extra bytes.
_KIND_PRIOR_TABLES: dict[str, dict[str, float]] = {
    "default": {
        "function": 1.0,
        "class_skeleton": 0.95,
        "other": 0.85,
        "comment_block": 0.8,
        "file_header": 0.6,
    },
    "flat": {
        "function": 1.0,
        "class_skeleton": 0.975,
        "other": 0.925,
        "comment_block": 0.9,
        "file_header": 0.8,
    },
    "structural": {},
}


# The table served when the request names none: it cleared the paired gate on the issue lane.
_SERVED_KIND_PRIOR = "flat"

_STRUCTURAL_KIND_BOOST = 0.15  # x1.15 on the kind the query names


# query token -> chunk kind the asker means. "table function" and "window
# function" are ClickHouse SQL vocabulary, not C++ structure; the lexical
# leg still finds the implementing function, and the mined issue lane
# (where that vocabulary is common) is the arm that measures the cost.
_STRUCTURAL_KIND_TOKENS: dict[str, str] = {
    "class": "class_skeleton",
    "struct": "class_skeleton",
    "enum": "class_skeleton",
    "interface": "class_skeleton",
    "function": "function",
    "method": "function",
    "constructor": "function",
    "destructor": "function",
}


_FILE_MENTION_RE = re.compile(
    r"(?:^|[\s\"'`(])(?:[\w./-]*/)?[\w.-]+\."
    r"(?:h|hpp|cpp|cc|cxx|inl|ipp|py|sql|sh|go|rs|ts|tsx|js|java|kt|md|xml|yaml|yml)"
    r"(?::\d+)?(?=$|[\s\"'`),;:?!])",
    re.I,
)


def _kind_prior_table(requested: str | None) -> dict[str, float] | None:
    """The table a request runs under: the override when given, else `flat`;
    None (or {} for "structural") when off. An unknown
    name is treated as off — a typo in an A/B must not silently become an
    arm."""
    name = (requested if requested is not None else _SERVED_KIND_PRIOR) or "off"
    name = str(name).strip().lower()
    if name == "off" or name not in _KIND_PRIOR_TABLES:
        return None
    return _KIND_PRIOR_TABLES[name]


def query_names_file(query: str) -> bool:
    """True when the query carries a file name or path with a code extension
    ("WriteBufferFromS3.cpp", "src/IO/ReadBufferFromS3.h:40")."""
    return bool(_FILE_MENTION_RE.search(query))


def structural_kind(query: str) -> str:
    """The chunk kind a query's phrasing names ("the class that ..." ->
    class_skeleton; "function that ..." -> function), or "" when none. When
    the query names both a class and a function, the class wins: "the
    method of the class that ..." is a question about the class."""
    tokens = code_tokens(query)
    kinds = {_STRUCTURAL_KIND_TOKENS[t] for t in tokens if t in _STRUCTURAL_KIND_TOKENS}
    if not kinds:
        return ""
    return "class_skeleton" if "class_skeleton" in kinds else "function"


def _apply_kind_prior(
    ranked: list[CodeUnit], query: str, table: dict[str, float]
) -> tuple[list[CodeUnit], str]:
    """Multiply each unit's score by the table's factor for its kind (or by
    1 + _STRUCTURAL_KIND_BOOST on the kind the query names), stable-sort.
    Returns the reordered list and the score_explanation fragment."""
    label = next((k for k, v in _KIND_PRIOR_TABLES.items() if v is table), "custom")
    if not ranked:
        return ranked, f"kind_prior={label}"
    if query_names_file(query):
        return ranked, f"kind_prior={label}:skip_file"
    q_tokens = frozenset(code_tokens(query))
    if q_tokens & _TEST_INTENT_TOKENS:
        return ranked, f"kind_prior={label}:skip_test_intent"
    named = structural_kind(query)
    out: list[CodeUnit] = []
    if named:
        for u in ranked:
            f = 1.0 + _STRUCTURAL_KIND_BOOST if u.kind == named else 1.0
            out.append(u.model_copy(update={"score": round(u.score * f, 6)}) if f != 1.0 else u)
        out.sort(key=lambda u: -u.score)
        return out, f"kind_prior={label}:structural={named}"
    if not table:  # "structural": identity table, phrasing rule only
        return ranked, f"kind_prior={label}"
    for u in ranked:
        f = table.get(u.kind, 1.0)
        out.append(u.model_copy(update={"score": round(u.score * f, 6)}) if f != 1.0 else u)
    out.sort(key=lambda u: -u.score)
    return out, f"kind_prior={label}"


_BOOST_CAP = 0.35  # max ±35% score adjustment — relevance always dominates


_BOOST_HALF_LIFE_DAYS = 14.0


def outcome_boost_factor(positive: float, negative: float, days_since_positive: float) -> float:
    """Multiplicative score factor in [1-cap, 1+cap] from folded outcomes."""
    decay: float = 0.5 ** (max(days_since_positive, 0.0) / _BOOST_HALF_LIFE_DAYS)
    raw = 0.15 * math.log1p(positive) * decay - 0.15 * math.log1p(negative)
    return float(1.0 + max(-_BOOST_CAP, min(_BOOST_CAP, raw)))


def _apply_outcome_boost(
    units: list[CodeUnit], signals: dict[str, tuple[float, float, Any]]
) -> list[CodeUnit]:

    now = naive_utc_now()
    out = []
    for u in units:
        sig = signals.get(u.unit_id)
        if sig is None:
            out.append(u)
            continue
        pos, neg, last_pos = sig
        days = 3650.0
        if last_pos and getattr(last_pos, "year", 1970) > 1971:
            days = max((now - last_pos).total_seconds() / 86400.0, 0.0)
        factor = outcome_boost_factor(pos, neg, days)
        out.append(u.model_copy(update={"score": round(u.score * factor, 6)}))
    out.sort(key=lambda u: -u.score)
    return out

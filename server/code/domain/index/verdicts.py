"""A verdict, not an abstain, for the structural code ops.

`code.impact`, `code.outline` and `code.verify` walk a graph or a symbol table. An empty
walk is not an abstain (`abstain = 1` / `retrieval_risk = 'abstain'`, the words the
search legs use for "the index could not tell"): most empty walks are answers — around a
third of `code.impact`, `code.outline` and `code.verify` calls: a test-only diff touches
nothing the graph indexes (`.sql` / `.reference` / `.xml` define no function), a
factory-registered function has no static caller, a header of constants declares nothing
of the requested kinds. "Nothing calls this" is the answer; reading it as "unknown"
sends the consumer to a local grep it does not need.

The verdict names why the answer is empty, from a closed vocabulary shared
by the three ops, the response field and the trace column:

  no_indexed_paths   the request names no path the symbol table holds
                     declarations for (test files, data/config files, a
                     held file with none of the requested kinds) — the
                     graph is untouched; an answer
  no_seeds_in_hunks  indexed source paths, but every hunk falls outside
                     every function (includes, comments, constants) — an
                     answer
  no_static_callers  the seeds resolved and the walk from them was empty in
                     both tiers — an answer (the epistemic envelope still
                     says whether it is exact or a lower bound)
  not_covered        a graphed-language path the index does not hold at all
                     — the index cannot answer; abstain
  no_match           nothing resolved and the index cannot say why (a diff
                     that parsed to no path, a file with a parse error, a
                     literal absent from an incomplete chunk set) — abstain
  ''                 the walk returned rows

`abstain` stays true for `not_covered` and `no_match` only. Pure: the
handlers gather the reads, this module names the outcome.

The verdict also carries the paths behind it. `paths_seen` classifies every path a
diff-shaped request named — `test`, `doc`, `config`, `unindexed` (outside the indexed
prefixes, or a source path the index holds no file row for) or `src` — so a consumer can
act on the verdict without re-deriving it: `no_indexed_paths` over paths that are all
`test` is "skip the gap analyst on this PR", and it is `exact` (the graph holds no
function in any of them, whatever the tier covers).
"""

from __future__ import annotations

from typing import Any

VERDICT_NO_INDEXED_PATHS = "no_indexed_paths"
VERDICT_NO_SEEDS_IN_HUNKS = "no_seeds_in_hunks"
VERDICT_NO_STATIC_CALLERS = "no_static_callers"
VERDICT_NOT_COVERED = "not_covered"
VERDICT_NO_MATCH = "no_match"

VERDICTS: frozenset[str] = frozenset(
    {
        VERDICT_NO_INDEXED_PATHS,
        VERDICT_NO_SEEDS_IN_HUNKS,
        VERDICT_NO_STATIC_CALLERS,
        VERDICT_NOT_COVERED,
        VERDICT_NO_MATCH,
    }
)

# The two verdicts under which the index could not answer.
ABSTAIN_VERDICTS: frozenset[str] = frozenset({VERDICT_NOT_COVERED, VERDICT_NO_MATCH})

# Languages the call graph is built for; a path in any other language defines nothing
# the graph can reach (the external-calls rule).
GRAPHED_LANGS: frozenset[str] = frozenset({"cpp", "python", "shell"})


# Path classes on `paths_seen`. Order of the tuple is the order of precedence in
# classify_path.
PATH_CLASS_TEST = "test"
PATH_CLASS_DOC = "doc"
PATH_CLASS_CONFIG = "config"
PATH_CLASS_UNINDEXED = "unindexed"
PATH_CLASS_SRC = "src"
PATH_CLASSES: tuple[str, ...] = (
    PATH_CLASS_TEST,
    PATH_CLASS_DOC,
    PATH_CLASS_CONFIG,
    PATH_CLASS_UNINDEXED,
    PATH_CLASS_SRC,
)


def is_abstain(verdict: str) -> bool:
    """True when the verdict says the index could not answer."""
    return verdict in ABSTAIN_VERDICTS


def is_graphed_path(path: str) -> bool:
    from server.code.domain.extract.treesitter import lang_for_path

    return lang_for_path(path) in GRAPHED_LANGS


def impact_verdict(
    *,
    seed_paths: list[str],
    symbol_rows: list[dict[str, Any]],
    seeds: list[dict[str, Any]],
    impacted: dict[str, Any] | list[Any],
    unindexed: set[str],
) -> str:
    """The verdict of one `code.impact` walk.

    `seed_paths`: the paths the request named (files and diff headers);
    `symbol_rows`: live function rows the index holds in those paths;
    `seeds`: the rows the walk started from (hunk- or header-narrowed in
    diff shape, every row in files shape, the resolved symbol otherwise);
    `impacted`: what the walk reached; `unindexed`: graphed-language seed
    paths the index holds no file row for.
    """
    if impacted:
        return ""
    if seeds:
        return VERDICT_NO_STATIC_CALLERS
    if not seed_paths:
        return VERDICT_NO_MATCH
    if unindexed:
        return VERDICT_NOT_COVERED
    if symbol_rows:
        return VERDICT_NO_SEEDS_IN_HUNKS
    return VERDICT_NO_INDEXED_PATHS


def classify_path(
    path: str,
    *,
    prefixes: tuple[str, ...] = (),
    unindexed: frozenset[str] | set[str] = frozenset(),
) -> str:
    """One path's class for `paths_seen`.

    `test` (the repository's test conventions: tests/ and spec/ directories,
    test-named files, fixtures under tests/ included) wins over everything —
    a test is a leaf of the call graph whatever its language. Then
    `unindexed` for a path outside `prefixes` (the repo's indexed path
    prefixes; an empty tuple means the whole tree is indexed) or a graphed
    source path in `unindexed` (the index holds no file row). Then `doc`
    (.md/.rst), `src` (a language the graph is built for) and `config` for
    the rest (sql, xml/yaml/json/cmake, and any extension with no symbol
    semantics — .reference, .txt, .html)."""
    from server.code.domain.extract.treesitter import lang_for_path
    from server.code.domain.index.test_gate import is_test_path

    p = (path or "").strip().lstrip("./")
    if not p:
        return PATH_CLASS_CONFIG
    if is_test_path(p):
        return PATH_CLASS_TEST
    if prefixes and not any(p.startswith(pref) for pref in prefixes):
        return PATH_CLASS_UNINDEXED
    if p in unindexed:
        return PATH_CLASS_UNINDEXED
    lang = lang_for_path(p)
    if lang == "doc":
        return PATH_CLASS_DOC
    if lang in GRAPHED_LANGS:
        return PATH_CLASS_SRC
    return PATH_CLASS_CONFIG


def paths_seen(
    paths: list[str],
    *,
    prefixes: tuple[str, ...] = (),
    unindexed: frozenset[str] | set[str] = frozenset(),
    limit: int = 200,
) -> list[dict[str, str]]:
    """`[{path, class}]` for the paths a request named, request order,
    duplicates dropped, at most `limit` entries."""
    return [
        {"path": p, "class": classify_path(p, prefixes=prefixes, unindexed=unindexed)}
        for p in list(dict.fromkeys(x for x in paths if x))[:limit]
    ]


def verdict_is_exact(verdict: str, seen: list[dict[str, str]]) -> bool:
    """True when the verdict is exact by construction, whatever the compiler
    tier covers: `no_indexed_paths` over paths none of which is `src` — the
    graph holds no function in a test, a doc, a data file or a path outside
    the index, so nothing was walked and nothing was missed. A `src` path
    with no function rows (a header of constants, or a parse the index
    lost) keeps the envelope's own epistemic: it may be a gap."""
    if verdict != VERDICT_NO_INDEXED_PATHS:
        return False
    return all(e.get("class") != PATH_CLASS_SRC for e in seen)


def outline_verdict(*, path: str, rows: list[Any], file_row: dict[str, Any] | None) -> str:
    """The verdict of one `code.outline` read. `file_row` is the live
    `code_files` row for the path, None when the index holds none (or the
    read failed — fail-open to the lower verdict, never the higher)."""
    if rows:
        return ""
    if file_row is None:
        return VERDICT_NOT_COVERED if is_graphed_path(path) else VERDICT_NO_INDEXED_PATHS
    if str(file_row.get("index_error") or ""):
        return VERDICT_NO_MATCH
    return VERDICT_NO_INDEXED_PATHS


def verify_verdict(*, kind: str, claim_verdict: str, file_row: dict[str, Any] | None = None) -> str:
    """The index verdict behind a `code.verify` claim verdict. Only a
    `not_established` claim has one: the graph kinds ran a walk from
    resolved seeds and found no edge (an answer); the file kinds either had
    no file to read (not covered) or a file whose symbol table or chunk set
    cannot refute (no match)."""
    if claim_verdict != "not_established":
        return ""
    if kind in ("calls", "reaches", "unused"):
        return VERDICT_NO_STATIC_CALLERS
    return VERDICT_NOT_COVERED if file_row is None else VERDICT_NO_MATCH


def impact_reason(verdict: str, causes: list[str], seen: list[dict[str, str]]) -> str:
    """The `reason` behind an empty `code.impact` answer, one token the trace row can
    group by: '' when the walk reached something; the envelope's causes
    (`treesitter_only_files`, `ctor_edges_absent`, …) joined for `no_static_callers`,
    `no_edges_from_seeds` when it names none; the path classes the request touched for
    `no_indexed_paths` (`paths_test`, `paths_doc+test`, …); fixed names for the other
    two."""
    if not verdict:
        return ""
    if verdict == VERDICT_NO_STATIC_CALLERS:
        return ",".join(causes)[:64] if causes else "no_edges_from_seeds"
    if verdict == VERDICT_NO_INDEXED_PATHS:
        classes = sorted({str(e.get("class") or "") for e in seen if e.get("class")})
        return ("paths_" + "+".join(classes))[:64] if classes else "no_paths"
    if verdict == VERDICT_NOT_COVERED:
        return "paths_unindexed"
    if verdict == VERDICT_NO_SEEDS_IN_HUNKS:
        return "hunks_outside_functions"
    return verdict[:64]

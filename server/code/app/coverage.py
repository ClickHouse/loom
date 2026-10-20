"""code.test_home and code.coverage_claim.

Both are keyed reads over what the index already holds: the tests tree is
indexed (15k stateless files, 3.6k integration files on the ClickHouse
namespace), the test map carries four evidence classes, and code_files
has every live path. Every claim about "nothing covers this" runs a
positive control first; a failed control makes the verdict `unknown`.

Every evidence row carries its tier, `covered_by` needs a row that names the subject itself
(its qualified form or the SQL name its class registers) or a PR co-occurrence, a SQL-level
subject (`sumMap`) binds to its implementing class through the registration statement, and
the stateful tree is searched with the other two.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, cast

from server.code.api.models_testing import (
    CodeCoverageClaimOutput,
    CodeTestHomeOutput,
    CodeTestsForOutput,
)
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.files import attach_reference_siblings
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.domain.index import coverage_claim as cc
from server.code.domain.index import coverage_runs as cr
from server.code.domain.index import test_conventions as tcv
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.settings import settings

logger = logging.getLogger(__name__)

_PART_LIMIT = 300  # per test tree; a part past this is resolved by membership, not a list
# A grep literal in more test files than this is vocabulary (the crossref
# pass's `_MAX_DF`): its hits never count as naming the subject.
_VOCABULARY_DF = 1500


async def _grep_tests(
    repo: CodeRepo, org: str, ns: str, pattern: str, *, limit: int = _PART_LIMIT
) -> tuple[list[dict[str, Any]], int]:
    """Files under the test trees containing `pattern` (literal, case
    sensitive) — one prefix-scoped grep_files read per tree, run together
    (0_stateless, 1_stateful, tests/integration) — and the total distinct file count before
    the limit, so a caller can tell a complete list from a truncated one."""

    async def one(prefix: str) -> tuple[list[dict[str, Any]], int]:
        return await repo.grep_files(
            org, ns, pattern, regex=False, case_sensitive=True, path_prefix=prefix, limit=limit
        )

    got = await asyncio.gather(*(one(prefix) for prefix in cc.TEST_PREFIXES))
    return [f for files, _t in got for f in files], sum(int(t) for _f, t in got)


async def _grep_tests_many(
    repo: CodeRepo, org: str, ns: str, requests: list[tuple[str, int]]
) -> list[tuple[list[dict[str, Any]], int]]:
    """`_grep_tests` for a round of (pattern, limit) requests — one `grep_files_many`
    statement per distinct limit over every test tree, each answer assembled exactly as
    `_grep_tests` does: the trees' pages in `TEST_PREFIXES` order, the totals summed. A
    repository without the round-wide read answers per pattern."""
    if not requests:
        return []
    many = getattr(repo, "grep_files_many", None)
    if many is None:
        return list(
            await asyncio.gather(
                *(_grep_tests(repo, org, ns, pat, limit=lim) for pat, lim in requests)
            )
        )
    by_limit: dict[int, list[str]] = {}
    for pat, lim in requests:
        by_limit.setdefault(int(lim), []).append(pat)
    hits: dict[tuple[str, int], tuple[list[dict[str, Any]], int]] = {}
    for lim, pats in by_limit.items():
        got = await many(
            org, ns, list(dict.fromkeys(pats)), path_prefixes=list(cc.TEST_PREFIXES), limit=lim
        )
        for pat in pats:
            pages = [got.get((pat, pre), ([], 0)) for pre in cc.TEST_PREFIXES]
            hits[(pat, lim)] = (
                [f for files, _t in pages for f in files],
                sum(int(t) for _f, t in pages),
            )
    return [hits[(pat, int(lim))] for pat, lim in requests]


async def _resolve_parts(
    repo: CodeRepo,
    org: str,
    ns: str,
    parts: list[tuple[str, str]],
    hits: list[tuple[list[dict[str, Any]], int]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Complete hit sets per part. A part whose list was truncated (an engine
    name is in thousands of tests) is re-resolved as a membership check over
    the candidates of the rarest part, so the intersection is exact."""
    complete = {p: rows for p, (rows, total) in zip(parts, hits, strict=True) if total <= len(rows)}
    truncated = [p for p, (rows, total) in zip(parts, hits, strict=True) if total > len(rows)]
    if not truncated:
        return complete
    # the rarest complete part WITH hits is the candidate set; an empty part
    # says nothing about where the others intersect
    rare = sorted((kv for kv in complete.items() if kv[1]), key=lambda kv: len(kv[1]))
    cands = [str(h["path"]) for h in rare[0][1]] if rare else []
    out = dict(complete)
    for p in truncated:
        rows, _total = hits[parts.index(p)]
        if not cands:
            out[p] = rows  # nothing to intersect with: the page is the best we have
            continue
        present = set(await repo.paths_containing(org, ns, cands, cc.grep_pattern(*p)))
        out[p] = [{"path": c, "chunks": 1, "first_line": 0} for c in cands if c in present]
    return out


_control_cache: dict[tuple[str, str], tuple[bool, str, float]] = {}


def reset_control_cache() -> None:
    """Forget every cached positive control (tests; a fixture's repository
    changes its answer without the clock moving)."""
    _control_cache.clear()


async def positive_control(repo: CodeRepo, org: str, ns: str) -> tuple[bool, str]:
    """The same corpus read for a symbol every test suite of the repository names
    (`code_coverage_control_symbol`). Empty means the corpus is not measurable from
    here — no control configured, a missing tests tree, a lagging index, a broken
    read — and every claim on this call is `unknown`. The answer is kept per
    namespace for `code_coverage_control_ttl_s`; a failed read is not kept."""
    probe = str(settings.code_coverage_control_symbol)
    if not probe:
        return False, ""
    cached = _control_cache.get((org, ns))
    now = time.monotonic()
    if (
        cached is not None
        and cached[1] == probe
        and now - cached[2] < settings.code_coverage_control_ttl_s
    ):
        return cached[0], probe
    try:
        files, _total = await _grep_tests(repo, org, ns, cc.grep_pattern("symbol", probe), limit=3)
    except Exception:  # noqa: BLE001 — a failed control is a failed control
        return False, probe
    _control_cache[(org, ns)] = (bool(files), probe, now)
    return bool(files), probe


# fingerprint part kind -> facet kind; a stem has no facet
_PART_FACET_KIND = {"setting": "setting", "engine": "engine", "symbol": "function"}


def _facet_of_part(kind: str, value: str) -> tuple[str, str] | None:
    fk = _PART_FACET_KIND.get(kind)
    if not fk:
        return None
    name = value.rsplit("::", 1)[-1] if kind == "symbol" else value
    return (fk, name) if name else None


async def _facet_hits(
    repo: CodeRepo,
    org: str,
    ns: str,
    parts: list[tuple[str, str]],
) -> dict[tuple[str, str], tuple[list[dict[str, Any]], int]]:
    """Per part, the tests whose statement records carry it, in the grep
    hit shape (`path`, `chunks`, `first_line`) with the total before the
    limit. A part with no facet rows is left out so the grep answers it —
    on a namespace without any facet rows every part falls through."""
    if not await repo.facets_present(org, ns):
        return {}
    out: dict[tuple[str, str], tuple[list[dict[str, Any]], int]] = {}
    pairs = [(p, _facet_of_part(*p)) for p in parts]
    reads = await asyncio.gather(
        *(repo.tests_for_facets(org, ns, [f], limit=_PART_LIMIT) for _, f in pairs if f)
    )
    for (part, _f), (rows, total) in zip([x for x in pairs if x[1]], reads, strict=True):
        if not rows:
            continue
        out[part] = (
            [
                {
                    "path": str(r["path"]),
                    "chunks": len(r.get("ordinals") or []) or 1,
                    "first_line": min(r.get("lines") or [0]),
                }
                for r in rows
                if cc.is_test_file(str(r["path"]))
            ],
            total,
        )
    return out


async def handle_code_test_home(input: Any) -> Any:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    fp = cc.normalize_fingerprint(
        list(input.file_stems), list(input.settings), input.engine, list(input.symbols)
    )
    parts = fp.parts
    # A part the statement records name exactly (a setting, an engine, a function) is read
    # from the facets first — one keyed read instead of two prefix-scoped content scans; the
    # grep stays for the parts facets cannot name (a file stem) and for a namespace whose SQL
    # tree predates the records (no facet rows at all).
    facet_hits: dict[tuple[str, str], tuple[list[dict[str, Any]], int]] = {}
    if any(_facet_of_part(k, v) for k, v in parts):
        with code_span("test_home.facets", seeds=len(parts)):
            facet_hits = await _facet_hits(repo, org, ns, parts)
    grep_parts = [p for p in parts if p not in facet_hits]
    with code_span("test_home.grep", seeds=len(grep_parts)):
        gathered = await asyncio.gather(
            positive_control(repo, org, ns),
            _grep_tests_many(
                repo, org, ns, [(cc.grep_pattern(k, v), _PART_LIMIT) for k, v in grep_parts]
            ),
            repo.paths_by_stem(org, ns, list(fp.stems)) if fp.stems else _empty_list(),
            repo.max_stateless_test_number(org, ns),
        )
    control_ok, probe = cast(tuple[bool, str], gathered[0])
    grep_hits = cast(list[tuple[list[dict[str, Any]], int]], list(gathered[1]))
    by_part = dict(zip(grep_parts, grep_hits, strict=True))
    by_part.update(facet_hits)
    hits = [by_part[p] for p in parts]
    src_rows = cast(list[dict[str, Any]], gathered[2])
    max_number = int(cast(int, gathered[3]) or 0)
    src_paths = [str(r["path"]) for r in src_rows]
    with code_span("test_home.resolve", seeds=len(parts)):
        test_map, hits_by_part = await asyncio.gather(
            repo.test_map_for(org, ns, src_paths, limit=60) if src_paths else _empty_list(),
            _resolve_parts(repo, org, ns, parts, hits),
        )
    ranked = cc.rank_homes(parts, hits_by_part, test_map, limit=input.limit)
    live_parts = sum(
        1
        for p in parts
        if any(cc.is_test_file(str(h.get("path") or "")) for h in hits_by_part.get(p, []))
    )
    verdict = cc.home_verdict(parts, ranked, control_ok=control_ok, live_parts=live_parts)
    candidates = [
        {
            "path": c.path,
            "matched": c.matched,
            "map_confidence": round(c.map_confidence, 3),
            "score": round(c.score, 3),
        }
        for c in ranked
    ]
    # The expectation file next to each candidate — the convention a new case has to follow.
    await attach_reference_siblings(repo, org, ns, candidates)
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.test_home",
        query=" ".join(f"{k}:{v}" for k, v in parts) or "-",
        route="test_home",
        returned_ids=[],
        returned_paths=[c.path for c in ranked],
        returned_scores=[round(c.score, 3) for c in ranked],
        top_k=input.limit,
        tokens_returned=0,
        abstain=verdict == cc.UNKNOWN,
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(route="test_home", results=len(ranked), consumer=input.consumer)
    return CodeTestHomeOutput(
        verdict=verdict,
        home=ranked[0].path if ranked and verdict == cc.ADD_CASE_TO else "",
        candidates=candidates,
        parts=[f"{k}:{v}" for k, v in parts],
        src_paths=src_paths[:20],
        next_free_stateless_number=(max_number + 1) if max_number else 0,
        control={"symbol": probe, "ok": control_ok},
        trace_run_id=trace_run_id,
    )


async def _empty_list() -> list[Any]:
    return []


def default_terms(subject_kind: str, subject: str) -> dict[str, str]:
    """The grep literals for a subject and the tier a hit counts as, when no
    registration was read: a setting's name is its SQL-level name
    (`exact_scoped`); a symbol's bare name is a `short_name_grep`, and its
    `Class::method` tail — what a shell or Python test would write — is
    `exact_scoped`; a path is not grepped."""
    if subject_kind == "setting":
        return {subject: cc.EXACT_SCOPED}
    if subject_kind != "symbol":
        return {}
    segs = [x for x in subject.split("(", 1)[0].split("::") if x]
    if not segs:
        return {}
    terms = {segs[-1]: cc.SHORT_NAME_GREP}
    if len(segs) >= 2:
        terms["::".join(segs[-2:])] = cc.EXACT_SCOPED
    return terms


def _scoped_reference(evidence: str, exact_terms: set[str]) -> bool:
    """Does a content-reference row's evidence (the registered names the
    test used, comma-joined) name the subject's own SQL name?"""
    names = {n.strip() for n in evidence.split(",") if n.strip()}
    return bool(names & exact_terms)


def executed_rows(
    rows: list[dict[str, Any]], spans: dict[str, list[tuple[int, int]]] | None
) -> list[dict[str, Any]]:
    """The `coverage` rows that executed the subject: every row of a path the subject
    covers whole, else the rows whose ranges meet one of its spans, each re-stated as a
    test-map row whose evidence names the executed lines inside the span."""
    out: list[dict[str, Any]] = []
    for r in rows:
        src = str(r.get("src_path") or "")
        ranges = cr.evidence_ranges(str(r.get("evidence") or ""))
        want = (spans or {}).get(src)
        if want is not None:
            ranges = [
                (max(a, s0), min(b, s1))
                for a, b in ranges
                for s0, s1 in want
                if a <= s1 and s0 <= b
            ]
        if not ranges:
            continue
        shown = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges[:8])
        out.append(
            {
                "src_path": src,
                "test_path": str(r.get("test_path") or ""),
                "method": "coverage",
                "confidence": float(r.get("confidence") or 0.0),
                "evidence": f"executed {src} lines {shown}",
            }
        )
    return out


def subject_spans(rows: list[dict[str, Any]]) -> dict[str, list[tuple[int, int]]]:
    """path -> the line spans of a resolved symbol's definitions."""
    out: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        path, a, b = (
            str(r.get("path") or ""),
            int(r.get("start_line") or 0),
            int(r.get("end_line") or 0),
        )
        if path and a > 0:
            out.setdefault(path, []).append((a, max(a, b)))
    return out


async def measured_for(
    repo: CodeRepo,
    org: str,
    ns: str,
    src_paths: list[str],
    spans: dict[str, list[tuple[int, int]]] | None,
) -> dict[str, Any]:
    """What the newest imported coverage run says about the subject (`measured_execution`),
    {} when no run covered its files."""
    files = await repo.coverage_files_for(org, ns, src_paths) if src_paths else {}
    if not files:
        return {}
    ranges = {p: cr.json_ranges(str(f.get("lines") or "[]")) for p, f in files.items()}
    whole = {p: [(1, 10**9)] for p in src_paths}
    out = cc.measured_execution(spans if spans else whole, ranges)
    if not out["measured"]:
        return {}
    out["commit"] = str(next(iter(files.values())).get("commit_sha") or "")
    return out


async def claim_tests(
    repo: CodeRepo,
    org: str,
    ns: str,
    subject_kind: str,
    subject: str,
    src_paths: list[str],
    *,
    limit: int,
    terms: dict[str, str] | None = None,
    spans: dict[str, list[tuple[int, int]]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The coverage claim's reads and merge, without the verdict: the test
    map rows for the subject's source files and a textual grep over the test trees for each
    literal in `terms` (literal -> tier), merged per test file (map methods + `grep`), each
    row carrying the strongest `tier` of its evidence, strongest first, and the largest grep
    total before its cap. Shared with `code.conventions` so rule 7 asks the same question
    the op does, minus the trace row. Measured `coverage` rows count when their executed
    ranges meet the subject's span in `spans` (path -> line spans); a path absent from
    `spans`, or `spans` None, counts every row of the path."""
    cap = limit * 3
    terms = default_terms(subject_kind, subject) if terms is None else terms
    exact_terms = {t for t, tier in terms.items() if tier == cc.EXACT_SCOPED}
    ordered_terms = list(terms.items())
    # The terms' test-tree greps are one statement per claim.
    map_rows, greps, cov_rows = await asyncio.gather(
        repo.test_map_detail(org, ns, src_paths, limit=cap) if src_paths else _empty_list(),
        _grep_tests_many(repo, org, ns, [(t, cap) for t, _ in ordered_terms]),
        repo.coverage_rows_for(org, ns, src_paths) if src_paths else _empty_list(),
    )
    map_rows = list(map_rows) + executed_rows(cov_rows, spans)
    # The content references' evidence is registered vocabulary (the crossref
    # whitelist): a subject whose bare name is in it is itself a SQL-level
    # name, so its bare grep names the subject, not a short method name.
    registered = {
        n.strip()
        for r in map_rows
        if str(r.get("method") or "") == "content_reference"
        for n in str(r.get("evidence") or "").split(",")
        if n.strip()
    }
    bare = subject.split("(", 1)[0].split("::")[-1]
    if bare in registered:
        exact_terms.add(bare)
        ordered_terms = [(t, cc.EXACT_SCOPED if t == bare else tier) for t, tier in ordered_terms]
    tests: dict[str, dict[str, Any]] = {}

    def _row(tp: str) -> dict[str, Any]:
        return tests.setdefault(
            tp, {"test_path": tp, "methods": [], "tiers": [], "confidence": 0.0, "evidence": ""}
        )

    for r in map_rows:
        tp = str(r.get("test_path") or "")
        if not cc.is_test_file(tp):
            continue
        row = _row(tp)
        m = str(r.get("method") or "")
        ev = str(r.get("evidence") or "")
        if m and m not in row["methods"]:
            row["methods"].append(m)
        row["tiers"].append(cc.tier_of_method(m, scoped=_scoped_reference(ev, exact_terms)))
        row["confidence"] = max(row["confidence"], float(r.get("confidence") or 0.0))
        row["evidence"] = row["evidence"] or ev
    grep_total = 0
    for (term, tier), (grep_rows, total) in zip(ordered_terms, greps, strict=True):
        grep_total = max(grep_total, int(total))
        if tier == cc.EXACT_SCOPED and int(total) > _VOCABULARY_DF:
            # `any`, `count`: a registered name in more test files than the
            # crossref pass trusts is vocabulary, not a reference to the subject;
            # the caller's `terms` map is updated so the payload says so
            tier = cc.SHORT_NAME_GREP
            terms[term] = tier
        for f in grep_rows:
            tp = str(f.get("path") or "")
            if not cc.is_test_file(tp):
                continue
            row = _row(tp)
            if "grep" not in row["methods"]:
                row["methods"].append("grep")
            row["tiers"].append(tier)
            line_no = int(f.get("first_line") or 0)
            hit = f"names {term} at line {line_no}"
            if tier == cc.EXACT_SCOPED or not row["evidence"]:
                row["evidence"] = hit
            row["confidence"] = max(row["confidence"], 0.95 if tier == cc.EXACT_SCOPED else 0.8)
    out: list[dict[str, Any]] = []
    for row in tests.values():
        tiers = row.pop("tiers")
        row["tier"] = cc.strongest(tiers)
        out.append(row)
    rank = {t: i for i, t in enumerate(cc.TIER_ORDER)}
    ordered = sorted(out, key=lambda r: (rank.get(r["tier"], 9), -r["confidence"], r["test_path"]))
    return ordered, grep_total


async def _resolve_subject(
    repo: CodeRepo, org: str, ns: str, symbol: str
) -> tuple[list[dict[str, Any]], str]:
    """The symbol's definitions: the exact/suffix lookup, then the tolerant name forms and
    the registration rule (a SQL-level name binds to its implementing class;
    `resolved_by: registration`)."""
    rows = await repo.symbols_by_name(org, ns, symbol, limit=20)
    if rows:
        return rows, ""
    from server.code.app.index import symbol_forms

    return await symbol_forms.resolve_tolerant(repo, org, ns, symbol)


async def search_terms(
    repo: CodeRepo,
    org: str,
    ns: str,
    subject_kind: str,
    subject: str,
    rows: list[dict[str, Any]],
    resolved_by: str,
) -> dict[str, str]:
    """Literal -> tier for the grep. A SQL-level subject is its own exact term; a C++ symbol
    adds the SQL names its class registers (read from the class file's registration shapes)
    to the default terms."""
    from server.code.app.index import sql_names

    if subject_kind != "symbol":
        return default_terms(subject_kind, subject)
    if resolved_by == sql_names.RESOLVED_REGISTRATION:
        return {sql_names.split_sql_form(subject)[0]: cc.EXACT_SCOPED}
    terms = default_terms(subject_kind, subject)
    if not rows:
        return terms
    resolver = sql_names.Resolver(repo, org, ns)
    bare = subject.split("(", 1)[0].split("::")[-1]
    try:
        names, registered = await asyncio.gather(
            resolver.sql_names_for(rows),
            # a bare subject that is itself a registered name (`arrayJoin`,
            # `geohashesInBox` — also C++ free functions) names the subject
            resolver.is_registered(bare) if "::" not in subject.split("(", 1)[0] else _false(),
        )
    except Exception as e:  # noqa: BLE001 — the SQL names are an extra term, never the claim
        logger.info("coverage_claim: sql names read failed: %s", e)
        return terms
    for n in names:
        terms[n] = cc.EXACT_SCOPED
    if registered:
        terms[bare] = cc.EXACT_SCOPED
    return terms


# ---- a test is a unit that reaches the symbol -----------------------------------------
# Outside the ClickHouse trees the grep finds nothing and the control probe fails, which
# would leave every Go / Python / TypeScript claim `unknown`. Three legs read what the index
# already holds: the `calls` edges from test units into the subject's definitions
# (exact_scoped), a test function named for the subject (exact_scoped), and the test file
# that sits beside the source by the language's convention (path_convention).
_EDGE_LIMIT = 400
_DEPTH2_MAX_INTERMEDIATES = 50
_DEPTH2_WHEN_FEWER_THAN = 3
METHOD_CALLS_EDGE = "calls_edge"
METHOD_CALLS_EDGE_2 = "calls_edge_2"
METHOD_TEST_NAME = "test_name"
METHOD_SIBLING = "sibling_file"


def _unit_row(tp: str, method: str, tier: str, confidence: float, evidence: str) -> dict[str, Any]:
    return {
        "test_path": tp,
        "methods": [method],
        "tier": tier,
        "confidence": round(float(confidence), 3),
        "evidence": evidence,
    }


async def _test_callers(repo: CodeRepo, org: str, ns: str, ids: list[str]) -> list[dict[str, Any]]:
    """Leg a: `calls` edges from test-unit paths into `ids`; one more hop
    through non-test callers when the first hop found few test callers."""
    edges = await repo.edges_for_symbols(
        org,
        ns,
        ids,
        direction="callers",
        edge_kinds=["calls"],
        limit=_EDGE_LIMIT,
        min_confidence=0.6,
    )
    out: list[dict[str, Any]] = []
    middle: list[dict[str, Any]] = []
    for e in edges:
        path = str(e.get("path") or "")
        if tcv.is_test_unit_path(path):
            out.append(
                _unit_row(
                    path,
                    METHOD_CALLS_EDGE,
                    cc.EXACT_SCOPED,
                    float(e.get("confidence") or 0.0),
                    f"{e.get('from_qualified_name')} calls it at line {int(e.get('line') or 0)}",
                )
            )
        else:
            middle.append(e)
    if len(out) >= _DEPTH2_WHEN_FEWER_THAN or not middle:
        return out
    mids = list(
        dict.fromkeys(str(e.get("from_symbol_id") or "") for e in middle if e.get("from_symbol_id"))
    )[:_DEPTH2_MAX_INTERMEDIATES]
    names = {
        str(e.get("from_symbol_id") or ""): str(e.get("from_qualified_name") or "") for e in middle
    }
    hop2 = await repo.edges_for_symbols(
        org,
        ns,
        mids,
        direction="callers",
        edge_kinds=["calls"],
        limit=_EDGE_LIMIT,
        min_confidence=0.6,
    )
    for e in hop2:
        path = str(e.get("path") or "")
        if not tcv.is_test_unit_path(path):
            continue
        via = names.get(str(e.get("to_symbol_id") or ""), "a caller")
        out.append(
            _unit_row(
                path,
                METHOD_CALLS_EDGE_2,
                cc.SHORT_NAME_GREP,
                min(0.8, float(e.get("confidence") or 0.0)),
                f"{e.get('from_qualified_name')} calls it through {via} "
                f"at line {int(e.get('line') or 0)}",
            )
        )
    return out


async def _named_tests(repo: CodeRepo, org: str, ns: str, subject: str) -> list[dict[str, Any]]:
    """Leg b: a function named `Test<Bare>` (and the language's kin) in a
    test-unit path — one name-table read, one row read for the paths."""
    segs = [x for x in subject.split("(", 1)[0].split("::") if x]
    if not segs:
        return []
    names = tcv.test_unit_names(segs[-1], segs[-2] if len(segs) >= 2 else "")
    if not names:
        return []
    table = await repo.name_candidates(org, ns, names)
    wanted: dict[str, str] = {}
    for name, cands in table.items():
        for sid, _qn, kind in cands:
            if kind == "function":
                wanted[str(sid)] = str(name)
    if not wanted:
        return []
    rows = await repo.symbols_by_ids(org, ns, list(wanted)[:200])
    out: list[dict[str, Any]] = []
    for r in rows:
        path = str(r.get("path") or "")
        if tcv.is_test_unit_path(path):
            out.append(
                _unit_row(
                    path,
                    METHOD_TEST_NAME,
                    cc.EXACT_SCOPED,
                    0.9,
                    f"defines {wanted.get(str(r.get('symbol_id')), r.get('name'))}",
                )
            )
    return out


async def _sibling_tests(
    repo: CodeRepo, org: str, ns: str, src_paths: list[str]
) -> list[dict[str, Any]]:
    """Leg c: the conventional sibling test files of the source paths that
    the index holds live."""
    by_candidate: dict[str, str] = {}
    for sp in src_paths:
        for cand in tcv.sibling_test_paths(sp):
            by_candidate.setdefault(cand, sp)
    if not by_candidate:
        return []
    present = await repo.live_paths_present(org, ns, list(by_candidate))
    return [
        _unit_row(tp, METHOD_SIBLING, cc.PATH_CONVENTION, 0.6, f"sits beside {by_candidate[tp]}")
        for tp in sorted(present)
    ]


async def _leg(coro: Any, what: str) -> list[dict[str, Any]]:
    try:
        return list(await coro)
    except Exception as e:  # noqa: BLE001 — extra evidence, never the claim's failure
        logger.info("coverage_claim: %s leg failed: %s", what, e)
        return []


async def unit_tests_reaching(
    repo: CodeRepo,
    org: str,
    ns: str,
    rows: list[dict[str, Any]],
    subject: str,
    src_paths: list[str],
    *,
    subject_kind: str = "symbol",
) -> list[dict[str, Any]]:
    """The three unit-test legs for a subject, merged per test file
    (methods appended, the strongest tier, the highest confidence), in the
    shape `claim_tests` returns. A path subject takes only the sibling leg."""
    ids = list(dict.fromkeys(str(r.get("symbol_id") or "") for r in rows if r.get("symbol_id")))
    legs = [_leg(_sibling_tests(repo, org, ns, src_paths), "sibling")]
    if subject_kind == "symbol":
        if ids:
            legs.append(_leg(_test_callers(repo, org, ns, ids[:20]), "calls"))
        legs.append(_leg(_named_tests(repo, org, ns, subject), "names"))
    found = [r for part in await asyncio.gather(*legs) for r in part]
    return merge_test_rows([], found)


def merge_test_rows(
    base: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """`base` rows gain the methods, tier and confidence of `extra` rows on
    the same test path; new paths are appended; the order is claim_tests'
    (tier rank, confidence descending, path)."""
    merged: dict[str, dict[str, Any]] = {}
    for r in [*base, *extra]:
        tp = str(r.get("test_path") or "")
        cur = merged.get(tp)
        if cur is None:
            merged[tp] = {**r, "methods": list(r.get("methods") or [])}
            continue
        for m in r.get("methods") or []:
            if m not in cur["methods"]:
                cur["methods"].append(m)
        before = str(cur.get("tier") or "")
        cur["tier"] = cc.strongest([before, str(r.get("tier") or "")])
        cur["confidence"] = max(
            float(cur.get("confidence") or 0.0), float(r.get("confidence") or 0.0)
        )
        # the evidence of the strongest tier leads; a tie keeps the first
        if cur["tier"] != before or not cur.get("evidence"):
            cur["evidence"] = r.get("evidence") or cur.get("evidence") or ""
    rank = {t: i for i, t in enumerate(cc.TIER_ORDER)}
    return sorted(
        merged.values(),
        key=lambda r: (
            rank.get(str(r.get("tier") or ""), 9),
            -float(r["confidence"]),
            r["test_path"],
        ),
    )


async def _false() -> bool:
    return False


async def handle_code_coverage_claim(input: Any) -> Any:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    if sum(1 for v in (input.symbol, input.path, input.setting) if v) != 1:
        # after the gate, so a refused principal never learns the shape
        raise ValidationError("give exactly one of symbol, path, setting")
    repo = CodeRepo()
    subject_kind = "path" if input.path else ("setting" if input.setting else "symbol")
    subject = input.path or input.setting or input.symbol
    resolved_by = ""
    rows: list[dict[str, Any]] = []
    with code_span("coverage_claim.resolve", seeds=1):
        control_ok, probe = await positive_control(repo, org, ns)
        src_paths: list[str] = []
        if input.path:
            src_paths = [input.path]
        elif input.symbol:
            rows, resolved_by = await _resolve_subject(repo, org, ns, input.symbol)
            src_paths = list(
                dict.fromkeys(str(r.get("path") or "") for r in rows if r.get("path"))
            )[:20]
        terms = await search_terms(repo, org, ns, subject_kind, subject, rows, resolved_by)
    spans = subject_spans(rows) if subject_kind == "symbol" else None
    with code_span("coverage_claim.reads", seeds=len(src_paths)):
        (ordered, grep_total), units, measured = await asyncio.gather(
            claim_tests(
                repo,
                org,
                ns,
                subject_kind,
                subject,
                src_paths,
                limit=input.limit,
                terms=terms,
                spans=spans,
            ),
            unit_tests_reaching(repo, org, ns, rows, subject, src_paths, subject_kind=subject_kind)
            if subject_kind != "setting"
            else _empty_list(),
            measured_for(repo, org, ns, src_paths, spans),
        )
    if units:
        ordered = merge_test_rows(ordered, units)
    rows_for_epistemic = [{"method": m} for r in ordered for m in r["methods"]]
    control = {"symbol": probe, "ok": control_ok}
    if not control_ok and any(
        m in (METHOD_CALLS_EDGE, METHOD_TEST_NAME) for r in units for m in r["methods"]
    ):
        # The ClickHouse probe says nothing about a Go or Python tree; a test unit that
        # reaches the subject is the control.
        control_ok = True
        control = {"symbol": probe, "ok": True, "via": "unit_tests"}
    verdict = cc.coverage_verdict(
        ordered,
        control_ok=control_ok,
        measured_unexecuted=bool(measured) and not measured.get("executed"),
    )
    trace_run_id = new_trace_run_id()
    served = ordered[: input.limit]
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.coverage_claim",
        query=f"{subject_kind}:{subject}",
        route="coverage_claim",
        returned_ids=[],
        returned_paths=[r["test_path"] for r in served],
        returned_scores=[round(r["confidence"], 3) for r in served],
        top_k=input.limit,
        tokens_returned=0,
        abstain=verdict == cc.UNKNOWN,
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(route="coverage_claim", results=len(ordered), consumer=input.consumer)
    return CodeCoverageClaimOutput(
        subject=subject,
        subject_kind=subject_kind,
        verdict=verdict,
        tests=served,
        tests_total=max(len(ordered), int(grep_total)),
        src_paths=src_paths,
        epistemic=cc.epistemic_of(rows_for_epistemic) if ordered else "lower_bound",
        strongest_tier=cc.strongest([str(r.get("tier") or "") for r in ordered]),
        search_terms=terms,
        resolved_by=resolved_by,
        control=control,
        measured=measured,
        trace_run_id=trace_run_id,
    )


# Every facet kind a lane writes: the SQL statement facets, the shell islands' dialect and
# program, the migration tools' tool / migration / direction, the created objects and table
# references, and dbt's source / materialized / macro_call.
_FACET_KINDS = frozenset(
    {
        "setting",
        "function",
        "engine",
        "type",
        "system_table",
        "format",
        "error_code",
        "tag",
        "statement_kind",
        "dialect",
        "program",
        "tool",
        "migration",
        "direction",
        "creates",
        "table_read",
        "table_write",
        "source",
        "materialized",
        "macro_call",
        "column_read",
        "column_write",
        "http_route",  # `<METHOD> /path` at the handler
        "env_var",  # an environment variable at the line it is read
        "decorator",
        "modifier",  # a Python def's decorators and `async`
        # TLC configuration, FlatBuffers, CMake
        "checks",
        "root_type",
        "file_identifier",
        "builds",
        "tests",
        # Go directives, struct tags, cobra commands, kubebuilder markers
        "embed",
        "generate",
        "field_tag",
        "command",
        "marker",
        "crd",
        "manifest",  # a Kubernetes document's Kind
    }
)

# A `builds` question asked with a FILE PATH is answered by the CMake lane's source globs
# (`src/client/*.cpp`, confidence 0.9) matched at read time — the tree moves under a glob,
# so the expansion is never stored.
_BUILDS_KIND = "builds"
_GLOB_LEG_ROWS = 5000


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A CMake `file(GLOB)` / `GLOB_RECURSE` pattern as a full-match regex:
    `**` crosses directories, `*` and `?` do not."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


async def _builds_glob_leg(
    repo: Any, org: str, ns: str, pairs: list[tuple[str, str]], *, limit: int
) -> list[dict[str, Any]]:
    """Every asked facet is `builds` with a concrete path → the CMake lists whose source
    globs cover all of them, as test rows (`via` names the glob). Nothing asked that way →
    []."""
    wanted = [n for k, n in pairs if k == _BUILDS_KIND and "*" not in n and "/" in n]
    if not wanted or len(wanted) != len(pairs):
        return []
    globs = await repo.live_facets_of_kind(
        org, ns, _BUILDS_KIND, name_like="%*%", limit=_GLOB_LEG_ROWS
    )
    out: list[dict[str, Any]] = []
    for g in globs:
        pattern = str(g.get("name") or "")
        rx = glob_regex(pattern)
        if all(rx.match(w) for w in wanted):
            out.append(
                {
                    "path": str(g["path"]),
                    "ordinals": [int(g.get("ordinal") or 0)],
                    "lines": [int(g.get("line") or 0)],
                    "confidence": float(g.get("confidence") or 0.9),
                    "same_statement": True,
                    "via": f"glob:{pattern}",
                }
            )
    out.sort(key=lambda r: (-r["confidence"], r["path"]))
    return out[:limit]


def _facet_pairs(raw: list[dict[str, str]]) -> list[tuple[str, str]]:
    """Validate and normalize the request's facets: known kinds, non-empty
    names as written (no case folding — SQL names are case-sensitive in
    the tree even where the server is lenient)."""
    from server.core.errors import ValidationError

    pairs: list[tuple[str, str]] = []
    for f in raw:
        kind = str(f.get("kind") or "").strip()
        name = str(f.get("name") or "").strip()
        if kind not in _FACET_KINDS:
            raise ValidationError(
                f"code.tests_for: unknown facet kind {kind!r}; one of {sorted(_FACET_KINDS)}"
            )
        if not name or len(name) > 200:
            raise ValidationError("code.tests_for: every facet needs a name (1–200 chars)")
        pairs.append((kind, name))
    return list(dict.fromkeys(pairs))


async def handle_code_tests_for(input: Any) -> Any:
    """code.tests_for: the stateless tests whose statement records carry every named facet —
    one keyed read of `loom.code_test_facets` instead of a grep."""
    from server.code.app.common import index_as_of

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    pairs = _facet_pairs(list(input.facets))
    repo = CodeRepo()
    closest = bool(getattr(input, "closest", False))
    with code_span("tests_for.read", seeds=len(pairs), closest=closest) as sp:
        if closest:
            # The union of candidates, scored by facet-set similarity
            from server.code.store.index import closest_scores

            rows = await repo.facet_sets_sharing(org, ns, pairs, limit=max(400, input.limit))
            tests = closest_scores(pairs, rows, limit=input.limit)
            total = len(rows)
        else:
            tests, total = await repo.tests_for_facets(org, ns, pairs, limit=input.limit)
        glob_hits = await _builds_glob_leg(repo, org, ns, pairs, limit=input.limit)
        if glob_hits:
            known = {(str(t["path"]), tuple(t.get("ordinals") or [])) for t in tests}
            extra = [g for g in glob_hits if (g["path"], tuple(g["ordinals"])) not in known]
            tests = (list(tests) + extra)[: input.limit]
            total += len(extra)
        span_attrs(sp, results=len(tests), total=total)
    verdict = ""
    uncovered_kinds: list[str] = []
    if not tests:
        # An empty answer names why: a kind none of the namespace's lanes ever wrote = not
        # covered for it (abstain; grep is the fallback), every asked kind present but none
        # carrying these names = an answer. The control is per KIND, not "any facet row": a
        # TypeScript tree holds table_read rows and no settings, so `setting` there is
        # unknown, not absent.
        with code_span("tests_for.control"):
            present = await repo.facet_kinds_present(org, ns, [k for k, _ in pairs])
        uncovered_kinds = sorted({k for k, _ in pairs} - present)
        verdict = "not_covered" if uncovered_kinds else "no_match"
    paths = [str(t["path"]) for t in tests]
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.tests_for",
        query=" ".join(f"{k}:{n}" for k, n in pairs),
        route="closest" if closest else "facets",
        returned_ids=paths,
        returned_paths=paths,
        returned_scores=[float(t.get("similarity") or 0.0) for t in tests] if closest else [],
        top_k=input.limit,
        tokens_returned=sum(len(p) + 24 for p in paths) // 4,
        abstain=not tests,
        retrieval_risk="low" if tests else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(
        route="closest" if closest else "facets",
        results=len(tests),
        truncated=total > len(tests),
        abstain=not tests,
        retrieval_risk="low" if tests else "abstain",
        consumer=input.consumer,
        verdict=verdict,
    )
    return CodeTestsForOutput(
        tests=tests,
        total=total,
        truncated=total > len(tests),
        facets=[{"kind": k, "name": n} for k, n in pairs],
        verdict=verdict,
        abstain=verdict == "not_covered",
        uncovered_kinds=uncovered_kinds,
        as_of=await index_as_of(repo, org, ns),
        tokens_returned=sum(len(p) + 24 for p in paths) // 4,
        took_ms=int((time.monotonic() - started) * 1000),
        trace_run_id=trace_run_id,
    )

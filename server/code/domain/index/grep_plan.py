"""Regex/literal → required-literal plan for the code plane's substring grep.

The ngram skip index on ``lower(content)`` can only prune a granule when
the statement carries a predicate ClickHouse's index analysis understands: ``LIKE
'%lit%'``, ``match``, ``multiSearchAny``. A plain ``position(content, lit) > 0`` is
invisible to every skip index. This module turns a grep pattern into the *necessary*
literal conditions — every literal a matching chunk must contain — and emits them as
``lower(content) LIKE`` terms that sit in front of the exact matcher the op already
runs.

Soundness is the whole point: every
rewrite only ever *widens*. A plan may admit a chunk that does not match; it
must never refuse a chunk that does. The exact ``position`` /
``positionCaseInsensitive`` / ``match`` predicate stays in the statement and
decides, so the grep result is exactly the exact matcher's — the index only
chooses which granules are read. ``tests/unit/test_grep_plan.py`` checks the
property on generated patterns.

Why lowercase everywhere: the index is over ``lower(content)`` (ASCII fold),
so a literal is compared lowered whatever the op's case mode is. A
case-sensitive grep keeps its exact ``position`` re-check; the lowered LIKE
term is still a necessary condition for it. Only ASCII letters are folded, as
ClickHouse's ``lower`` folds only ASCII bytes — folding a non-ASCII letter
here and not there would be a false negative.

Regexes are parsed with the stdlib ``re`` parser, which agrees with RE2 (what
``match`` runs) on the constructs this walk reads, with two exceptions. POSIX
classes: the stdlib reads ``[[:alpha:]]x`` as a set followed by the literal ``]x``,
so any pattern holding ``[:`` plans to ``SCAN`` (an escaped ``\\[:`` too). Case
folding: under ``(?i)`` RE2 folds Unicode, so ``k`` and ``s`` also match the Kelvin
sign and the long s, which ``lower()`` (ASCII only) leaves alone; there, a run
plans to every spelling of its ``k``/``s`` letters (of its longest piece holding a
few of them, when it holds more), and a non-ASCII character breaks it. A pattern the
parser rejects, a top-level alternation whose branch has no literal, a class,
an anchor, a lookaround, a backreference and an optional group all contribute
nothing (``SCAN``) rather than guess.
"""

from __future__ import annotations

import importlib
import itertools
import re
from dataclasses import dataclass
from typing import Any, cast

# Python 3.11+: the stdlib regex parser lives at re._parser / re._constants —
# private modules with no stubs, so they are loaded by name and typed as Any.
_sre: Any = importlib.import_module("re._constants")
_sre_parser: Any = importlib.import_module("re._parser")

# Ngram size of the index; a shorter literal cannot select any n-gram, so it
# is not emitted (ClickHouse would ignore it for pruning anyway).
MIN_LITERAL_BYTES = 3
# Bound on emitted LIKE terms per statement: each is one index probe. 32 so a
# message-mode needle list (≤ 32 shingles) fits as one OR guard — an OR guard
# must carry EVERY needle or none; dropping a branch would narrow.
MAX_TERMS = 32

# The SETTINGS fragment for every statement whose content predicate rides the n-gram
# index under FINAL. `use_skip_indexes_if_final_exact_mode = 1` (the 26.x default)
# re-adds every granule whose primary-key range intersects a kept one — on a table whose
# parts each span the whole namespace that is ×parts (17 kept → 86 read on 6 parts in
# one measurement). It exists so a version of a key sitting in a skipped granule cannot
# change what FINAL picks; on `code_chunks` no version can, because every version of a
# key carries the same content (`VERSION_INVARIANT_COLS` in code/store/index/rows.py), so the
# predicate is constant per key and the kept set already holds every version of every
# matching key. `query_plan_direct_read_from_text_index = 0` because the direct read is
# unsafe under FINAL; the re-check reads the column anyway.
GREP_FINAL_SETTINGS = (
    "SETTINGS use_skip_indexes_if_final_exact_mode = 0, query_plan_direct_read_from_text_index = 0"
)


def ascii_lower(s: str) -> str:
    """Fold only ASCII letters, matching ClickHouse ``lower()`` on UTF-8 bytes."""
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


@dataclass(frozen=True)
class And:
    literals: tuple[str, ...]  # already ASCII-lowered, each ≥ MIN_LITERAL_BYTES


@dataclass(frozen=True)
class Or:
    branches: tuple[Plan, ...]  # never contains SCAN (an Or with a SCAN branch is SCAN)


class _Scan:
    """No literal is required: the index cannot narrow, the op scans."""

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return "SCAN"


SCAN = _Scan()
Plan = And | Or | _Scan


def is_scan(plan: Plan) -> bool:
    return plan is SCAN


# ---------------------------------------------------------------------------
# literal


def plan_literal(literal: str) -> Plan:
    """A literal grep requires itself."""
    lit = ascii_lower(literal)
    if len(lit.encode("utf-8")) < MIN_LITERAL_BYTES:
        return SCAN
    return And((lit,))


# ---------------------------------------------------------------------------
# regex


def plan_regex(pattern: str, *, ignore_case: bool = False, spell_folds: bool = True) -> Plan:
    """Required literals of an RE2/Python regex, or SCAN when nothing is required.

    ``ignore_case`` is the op's own case mode; a ``(?i)`` in the pattern counts
    the same. Flags other than ``i`` don't change which literals are required.
    ``spell_folds=False`` keeps only pieces without k/s instead of spelling them.
    """
    if "[:" in pattern:
        return SCAN
    try:
        parsed = _sre_parser.parse(pattern)
    except (re.error, ValueError, OverflowError, RecursionError):
        return SCAN
    ignore_case = ignore_case or bool(parsed.state.flags & re.IGNORECASE)
    try:
        # fewer k/s letters per run when the spellings outgrow MAX_TERMS
        for max_folds in _MAX_FOLDS if spell_folds else (0,):
            plan = _simplify(_walk_seq(list(parsed), ignore_case, max_folds))
            if not is_scan(plan):
                break
        return plan
    except RecursionError:  # pragma: no cover - pathological nesting
        return SCAN


def _sets_ignore_case(pattern: str) -> bool:
    """A leading global ``(?i)``."""
    try:
        return bool(_sre_parser.parse(pattern).state.flags & re.IGNORECASE)
    except (re.error, ValueError, OverflowError, RecursionError):
        return False


# Under (?i) RE2 also matches k and s to these; lower() leaves both alone.
_FOLD_PARTNERS = {"k": "\u212a", "s": "\u017f"}
# k/s letters one case-insensitive run may spell both ways (2**n spellings), tried in order.
_MAX_FOLDS = (4, 2, 0)


def _fold_literal(run: str, max_folds: int) -> Plan:
    """A case-insensitive ASCII run: a matching chunk holds one of its spellings
    with each k/s as itself or its fold partner. Past `max_folds` such letters only
    the longest piece holding `max_folds` of them is kept; it is required too."""
    low = ascii_lower(run)
    folds = [i for i, c in enumerate(low) if c in _FOLD_PARTNERS]
    if len(folds) > max_folds:
        edges = [-1, *folds, len(low)]
        windows = [
            (edges[i] + 1, edges[i + max_folds + 1]) for i in range(len(folds) - max_folds + 1)
        ]
        start, end = max(windows, key=lambda w: w[1] - w[0])
        low = low[start:end]
    options = [(c, _FOLD_PARTNERS[c]) if c in _FOLD_PARTNERS else (c,) for c in low]
    return _simplify(Or(tuple(plan_literal("".join(v)) for v in itertools.product(*options))))


def _walk_seq(items: list[Any], ignore_case: bool, max_folds: int) -> Plan:
    """A concatenation: literal runs become required literals; every other
    node flushes the run and contributes its own sub-plan. Runs never join
    across a node boundary (``a(bc)+d`` requires ``a``, ``bc`` and ``d``, not
    ``abcd``)."""
    terms: list[Plan] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            text = "".join(run)
            terms.append(_fold_literal(text, max_folds) if ignore_case else plan_literal(text))
            run.clear()

    for op, av in items:
        if op is _sre.LITERAL:
            c = chr(av)
            if ignore_case and not c.isascii():
                flush()  # its case partners are not ASCII-folded: it requires nothing
            else:
                run.append(c)
            continue
        flush()
        if op is _sre.SUBPATTERN:
            # (group, add_flags, del_flags, pattern)
            _, add_flags, del_flags, sub = av
            sub_ignore_case = (ignore_case or bool(add_flags & re.IGNORECASE)) and not (
                del_flags & re.IGNORECASE
            )
            terms.append(_walk_seq(list(sub), sub_ignore_case, max_folds))
        elif op is _sre.BRANCH:
            _, branches = av
            terms.append(
                _simplify(Or(tuple(_walk_seq(list(b), ignore_case, max_folds) for b in branches)))
            )
        elif op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT) or op is getattr(
            _sre, "POSSESSIVE_REPEAT", None
        ):
            lo, _hi, sub = av
            if lo >= 1:
                terms.append(_walk_seq(list(sub), ignore_case, max_folds))
            # lo == 0: optional → requires nothing
        elif op is getattr(_sre, "ATOMIC_GROUP", None):
            terms.append(_walk_seq(list(av), ignore_case, max_folds))
        else:
            # ANY, IN, NOT_LITERAL, AT (anchors), ASSERT / ASSERT_NOT
            # (lookarounds: deleting them widens), GROUPREF, CATEGORY, ...
            # contribute nothing.
            pass
    flush()
    return _simplify(_and_all(terms))


def _and_all(terms: list[Plan]) -> Plan:
    lits: list[str] = []
    ors: list[Or] = []
    for t in terms:
        if isinstance(t, And):
            lits.extend(t.literals)
        elif isinstance(t, Or):
            ors.append(t)
        # SCAN terms are simply dropped: AND with "anything" is the rest.
    if not lits and not ors:
        return SCAN
    if not ors:
        return And(tuple(dict.fromkeys(lits)))
    # Distribute: (A ∧ (B ∨ C)) → ((A∧B) ∨ (A∧C)). Bounded by MAX_TERMS via
    # _simplify; a plan that explodes degrades to its literal core.
    plan: Plan = And(tuple(dict.fromkeys(lits))) if lits else And(())
    for o in ors:
        branches: list[Plan] = []
        for b in o.branches:
            branches.append(_and_all([plan, b]))
        plan = _simplify(Or(tuple(branches)))
    return plan


def _simplify(plan: Plan) -> Plan:
    if isinstance(plan, And):
        lits = tuple(
            dict.fromkeys(x for x in plan.literals if len(x.encode("utf-8")) >= MIN_LITERAL_BYTES)
        )
        if not lits:
            return SCAN
        if len(lits) > MAX_TERMS:
            # keep the longest literals: fewest false granules per probe
            lits = tuple(sorted(lits, key=len, reverse=True)[:MAX_TERMS])
        return And(lits)
    if isinstance(plan, Or):
        branches: list[Plan] = []
        for b in plan.branches:
            s = _simplify(b)
            if is_scan(s):
                return SCAN  # one unindexable branch admits anything
            if isinstance(s, Or):
                branches.extend(s.branches)
            else:
                branches.append(s)
        uniq = tuple(dict.fromkeys(branches))
        if len(uniq) == 1:
            return uniq[0]
        if _term_count(Or(uniq)) > MAX_TERMS:
            return SCAN
        return Or(uniq)
    return plan


def scan_needles(plan: Plan, *, ignore_case: bool) -> list[str]:
    """The longest literal of each branch of an alternation: a matching chunk
    contains at least one. A single branch gets one only when ignoring case:
    `match` prefilters a case-sensitive regex on its required literal itself."""
    if isinstance(plan, Or):
        # _simplify leaves every branch a non-empty And
        return list(dict.fromkeys(max(cast(And, b).literals, key=len) for b in plan.branches))
    if isinstance(plan, And) and ignore_case:
        return [max(plan.literals, key=len)]
    return []


def _term_count(plan: Plan) -> int:
    if isinstance(plan, And):
        return len(plan.literals)
    if isinstance(plan, Or):
        return sum(_term_count(b) for b in plan.branches)
    return 0


# ---------------------------------------------------------------------------
# evaluation (tests, and the parity bench)


def admits(plan: Plan, text: str) -> bool:
    """Would a chunk with this content survive the plan's predicate?"""
    if isinstance(plan, And):
        low = ascii_lower(text)
        return all(lit in low for lit in plan.literals)
    if isinstance(plan, Or):
        return any(admits(b, text) for b in plan.branches)
    return True  # SCAN admits everything


# ---------------------------------------------------------------------------
# SQL


def like_escape(literal: str) -> str:
    return literal.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def to_sql(
    plan: Plan, *, column: str = "lower(content)", prefix: str = "gp"
) -> tuple[str, dict[str, Any]]:
    """Index-visible SQL fragment (no leading AND) and its bound params, or
    ("", {}) for SCAN. Params are named ``{prefix}_N`` so several plans can
    share one statement."""
    if is_scan(plan):
        return "", {}
    params: dict[str, Any] = {}

    def emit(p: Plan) -> str:
        if isinstance(p, And):
            parts = []
            for lit in p.literals:
                name = f"{prefix}_{len(params)}"
                params[name] = "%" + like_escape(lit) + "%"
                parts.append(f"{column} LIKE {{{name}:String}}")
            return " AND ".join(parts) if len(parts) == 1 else "(" + " AND ".join(parts) + ")"
        if isinstance(p, Or):
            return "(" + " OR ".join(emit(b) for b in p.branches) + ")"
        return "1"  # unreachable: SCAN branches were simplified away

    return emit(plan), params


def any_literal_guard(needles: list[str], *, prefix: str = "gn") -> tuple[str, dict[str, Any]]:
    """The index-visible guard for "any of these literals occurs"
    (`multiSearchAnyCaseInsensitive`, which no skip index reads): an OR of
    `lower(content) LIKE` terms, one per needle. Sound only when every needle
    is present, so a needle too short for an n-gram, or more needles than
    MAX_TERMS, yields ("", {}) and the statement scans."""
    plans = [plan_literal(n) for n in needles if n]
    if not plans or any(is_scan(p) for p in plans) or len(plans) > MAX_TERMS:
        return "", {}
    return to_sql(_simplify(Or(tuple(plans))), prefix=prefix)


def content_predicate(
    pattern: str,
    *,
    regex: bool,
    case_sensitive: bool,
    param: str = "pat",
    prefix: str = "gp",
    index_guard: bool = True,
) -> tuple[str, dict[str, Any]]:
    """The code plane's content predicate: the exact matcher the op runs,
    preceded by the index-visible necessary conditions when the pattern
    has any. Returns (SQL without leading AND, params).

    ``index_guard=False`` is for code.grep's own statements, which scan `content`
    instead of consulting the n-gram index: no `lower(content) LIKE` terms (they
    would only add a `lower()` over every row read). A regex still gets a
    `multiSearchAnyCaseInsensitive` over its `scan_needles` in front of `match`
    when `match` has no literal of its own to skip rows on (an alternation, or
    any case-insensitive regex); the needles come from k/s-free pieces, so one
    per branch suffices.

    Three exact shapes:
    ``match(content, pat)`` (``(?i)`` prepended when not case-sensitive),
    ``position(content, pat) > 0``, ``positionCaseInsensitive(content, pat) > 0``.
    """
    ignore_case = not case_sensitive
    if regex:
        exact = f"match(content, {{{param}:String}})"
        params: dict[str, Any] = {param: pattern if case_sensitive else "(?i)" + pattern}
        ignore_case = ignore_case or _sets_ignore_case(pattern)
        # the scan's needles skip the spellings: a long needle list costs more than RE2 saves
        plan = plan_regex(pattern, ignore_case=ignore_case, spell_folds=index_guard)
    elif case_sensitive:
        exact = f"position(content, {{{param}:String}}) > 0"
        params = {param: pattern}
        plan = plan_literal(pattern)
    else:
        exact = f"positionCaseInsensitive(content, {{{param}:String}}) > 0"
        params = {param: pattern}
        plan = plan_literal(pattern)
    if not index_guard:
        needles = scan_needles(plan, ignore_case=ignore_case) if regex else []
        if not needles:
            return exact, params
        params[f"{prefix}_any"] = needles
        return (
            f"multiSearchAnyCaseInsensitive(content, {{{prefix}_any:Array(String)}}) AND {exact}",
            params,
        )
    guard, gparams = to_sql(plan, prefix=prefix)
    if not guard:
        return exact, params
    params.update(gparams)
    return f"{guard} AND {exact}", params

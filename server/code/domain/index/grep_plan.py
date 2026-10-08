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
``match`` runs) on every construct this walk cares about. A pattern the
parser rejects, a top-level alternation whose branch has no literal, a class,
an anchor, a lookaround, a backreference and an optional group all contribute
nothing (``SCAN``) rather than guess.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any

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


def plan_regex(pattern: str) -> Plan:
    """Required literals of an RE2/Python regex, or SCAN when nothing is required.

    ``(?i)`` and every other flag are irrelevant here: the plan is compared
    against ``lower(content)`` regardless, and the exact ``match`` keeps the
    user's flags.
    """
    try:
        parsed = _sre_parser.parse(pattern)
    except (re.error, ValueError, OverflowError, RecursionError):
        return SCAN
    try:
        return _simplify(_walk_seq(list(parsed)))
    except RecursionError:  # pragma: no cover - pathological nesting
        return SCAN


def _walk_seq(items: list[Any]) -> Plan:
    """A concatenation: literal runs become required literals; every other
    node flushes the run and contributes its own sub-plan. Runs never join
    across a node boundary (``a(bc)+d`` requires ``a``, ``bc`` and ``d``, not
    ``abcd``)."""
    terms: list[Plan] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            terms.append(plan_literal("".join(run)))
            run.clear()

    for op, av in items:
        if op is _sre.LITERAL:
            run.append(chr(av))
            continue
        flush()
        if op is _sre.SUBPATTERN:
            # (group, add_flags, del_flags, pattern)
            sub = av[-1]
            terms.append(_walk_seq(list(sub)))
        elif op is _sre.BRANCH:
            _, branches = av
            terms.append(_simplify(Or(tuple(_walk_seq(list(b)) for b in branches))))
        elif op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT) or op is getattr(
            _sre, "POSSESSIVE_REPEAT", None
        ):
            lo, _hi, sub = av
            if lo >= 1:
                terms.append(_walk_seq(list(sub)))
            # lo == 0: optional → requires nothing
        elif op is getattr(_sre, "ATOMIC_GROUP", None):
            terms.append(_walk_seq(list(av)))
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

    ``index_guard=False`` returns the exact matcher alone (code.grep's own statements
    scan the namespace's `content` instead of consulting the n-gram index, so the
    `lower(content) LIKE` terms would only add a `lower()` over every row read).

    Three exact shapes:
    ``match(content, pat)`` (``(?i)`` prepended when not case-sensitive),
    ``position(content, pat) > 0``, ``positionCaseInsensitive(content, pat) > 0``.
    """
    if regex:
        exact = f"match(content, {{{param}:String}})"
        params: dict[str, Any] = {param: pattern if case_sensitive else "(?i)" + pattern}
        plan = plan_regex(pattern)
    elif case_sensitive:
        exact = f"position(content, {{{param}:String}}) > 0"
        params = {param: pattern}
        plan = plan_literal(pattern)
    else:
        exact = f"positionCaseInsensitive(content, {{{param}:String}}) > 0"
        params = {param: pattern}
        plan = plan_literal(pattern)
    guard, gparams = to_sql(plan, prefix=prefix) if index_guard else ("", {})
    if not guard:
        return exact, params
    params.update(gparams)
    return f"{guard} AND {exact}", params

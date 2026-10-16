"""The grep plan only widens: a chunk the plan refuses never matches.

Property checked on generated regexes and texts (seeded, no Hypothesis
dependency), plus the named shapes a trigram-grep planner's tests cover and the SQL
emission both grep ops and the constrained search rely on.
"""

from __future__ import annotations

import random
import re

import pytest

from server.code.domain.index import grep_plan as gp

# --------------------------------------------------------------------------
# named shapes


def test_literal_plan_requires_itself_lowered_and_short_literals_scan() -> None:
    assert gp.plan_literal("SettingsQuirks") == gp.And(("settingsquirks",))
    assert gp.is_scan(gp.plan_literal("ab"))
    assert gp.is_scan(gp.plan_literal(""))
    # a two-char UTF-8 literal can be ≥ 3 bytes: still one n-gram, still emitted
    assert gp.plan_literal("é!") == gp.And(("é!",))


def test_only_ascii_is_folded_like_clickhouse_lower() -> None:
    assert gp.ascii_lower("ÉCOLE Ab") == "École ab"


def test_regex_concat_of_literals_is_one_literal_and_classes_break_the_run() -> None:
    assert gp.plan_regex(r"SettingsQuirks::check\(") == gp.And(("settingsquirks::check(",))
    assert gp.plan_regex(r"max_threads\s*=\s*\d+") == gp.And(("max_threads",))
    assert gp.plan_regex(r"mutex.*mutex_lock") == gp.And(("mutex", "mutex_lock"))


def test_alternation_is_an_or_and_an_unindexable_branch_absorbs_it() -> None:
    assert gp.plan_regex(r"TODO|FIXME") == gp.Or((gp.And(("todo",)), gp.And(("fixme",))))
    assert gp.is_scan(gp.plan_regex(r"TODO|.*"))
    assert gp.is_scan(gp.plan_regex(r"FIXME|ab"))  # 'ab' too short → that branch admits anything


def test_optional_and_star_require_nothing_but_plus_keeps_its_body() -> None:
    assert gp.is_scan(gp.plan_regex(r"(abc)?"))
    assert gp.is_scan(gp.plan_regex(r"(abc)*"))
    assert gp.plan_regex(r"(abc)+") == gp.And(("abc",))
    assert gp.plan_regex(r"foo(bar)?baz") == gp.And(("foo", "baz"))


def test_repeats_do_not_join_runs_across_their_boundary() -> None:
    # a(bc)+d requires a, bc and d — never 'abcd' (abcbcd matches)
    plan = gp.plan_regex(r"a(bc)+d")
    assert gp.admits(plan, "xx abcbcd yy") is True  # no false refusal
    assert plan == gp.SCAN  # 'a', 'bc', 'd' are each under the n-gram size


def test_lookarounds_anchors_and_backreferences_are_dropped_not_refused() -> None:
    assert gp.plan_regex(r"^\s*throw Exception\((?!ErrorCodes)") == gp.And(("throw exception(",))
    assert gp.plan_regex(r"(?<=::)loadDataParts\b") == gp.And(("loaddataparts",))
    assert gp.plan_regex(r"(abc)\1") == gp.And(("abc",))


def test_unparseable_pattern_scans() -> None:
    assert gp.is_scan(gp.plan_regex(r"\Qliteral\E"))  # RE2-only escape
    assert gp.is_scan(gp.plan_regex(r"(unbalanced"))
    assert gp.is_scan(gp.plan_regex(r".*"))
    assert gp.is_scan(gp.plan_regex(r"[a-z]{3,}"))


def test_posix_classes_scan_because_the_stdlib_misreads_them() -> None:
    # RE2 reads [[:alpha:]] as one class; the stdlib would require the literal "]abc"
    assert gp.is_scan(gp.plan_regex(r"[[:alpha:]]abcd"))
    assert gp.is_scan(gp.plan_regex(r"TODO|[[:digit:]]+FIXME"))


def test_case_insensitive_regex_requires_nothing() -> None:
    # RE2 folds Unicode: (?i)kelvin matches "\u212aelvin", which no ASCII fold admits
    assert gp.is_scan(gp.plan_regex("kelvin", ignore_case=True))
    assert gp.is_scan(gp.plan_regex("(?i)kelvin|other"))
    assert gp.plan_regex("(?i:kelvin)_bound") == gp.And(("_bound",))
    assert gp.plan_regex("(?-i:kelvin)") == gp.And(("kelvin",))


def test_branch_needles_take_the_longest_literal_of_each_branch() -> None:
    assert gp.branch_needles(gp.plan_regex(r"join.*not supported|totals")) == [
        "not supported",
        "totals",
    ]
    assert gp.branch_needles(gp.plan_regex(r"getTotals\(\)|setTotals\(")) == [
        "gettotals()",
        "settotals(",
    ]
    assert gp.branch_needles(gp.plan_regex(r"^TODO$|\bFIXME\b")) == ["todo", "fixme"]
    assert gp.branch_needles(gp.plan_regex("café|naïve")) == ["café", "naïve"]
    assert gp.branch_needles(gp.plan_regex(r"mutex.*lock")) == []  # one branch: RE2 prefilters it
    assert gp.branch_needles(gp.plan_regex(r"TODO|.*")) == []
    assert gp.branch_needles(gp.plan_regex(r"\b[0-9a-f]{32}\b")) == []
    assert gp.branch_needles(gp.plan_regex("")) == []


def test_distribution_keeps_the_shared_literal_in_every_branch() -> None:
    plan = gp.plan_regex(r"positiveModulo(OrNull|Legacy)\(")
    assert isinstance(plan, gp.Or)
    for b in plan.branches:
        assert isinstance(b, gp.And)
        assert "positivemodulo" in b.literals


def test_too_many_terms_degrade_to_the_longest_literals_never_to_a_narrower_plan() -> None:
    pat = ".".join(f"lit{i:02d}" for i in range(40))
    plan = gp.plan_regex(pat)
    assert isinstance(plan, gp.And)
    assert len(plan.literals) == gp.MAX_TERMS
    assert gp.admits(plan, pat.replace(".", "-"))


# --------------------------------------------------------------------------
# SQL emission


def test_content_predicate_keeps_the_three_exact_shapes_and_puts_the_guard_first() -> None:
    sql, params = gp.content_predicate("Foo_Bar%", regex=False, case_sensitive=True)
    assert sql == "lower(content) LIKE {gp_0:String} AND position(content, {pat:String}) > 0"
    assert params == {"pat": "Foo_Bar%", "gp_0": "%foo\\_bar\\%%"}

    sql, params = gp.content_predicate("Foo", regex=False, case_sensitive=False)
    assert (
        sql
        == "lower(content) LIKE {gp_0:String} AND positionCaseInsensitive(content, {pat:String}) > 0"
    )
    assert params == {"pat": "Foo", "gp_0": "%foo%"}

    sql, params = gp.content_predicate("Fo+", regex=True, case_sensitive=False)
    assert sql == "match(content, {pat:String})"  # 'fo' is too short: no guard, the plain statement
    assert params == {"pat": "(?i)Fo+"}

    sql, params = gp.content_predicate(
        r"TODO|FIXME", regex=True, case_sensitive=True, param="sc_pat", prefix="sc"
    )
    assert (
        sql
        == "(lower(content) LIKE {sc_0:String} OR lower(content) LIKE {sc_1:String}) AND match(content, {sc_pat:String})"
    )
    assert params == {"sc_pat": "TODO|FIXME", "sc_0": "%todo%", "sc_1": "%fixme%"}


def test_scan_predicate_guards_only_a_case_sensitive_alternation() -> None:
    sql, params = gp.content_predicate(
        r"TODO|FIXME", regex=True, case_sensitive=True, index_guard=False
    )
    assert sql == (
        "multiSearchAnyCaseInsensitive(content, {gp_any:Array(String)}) AND match(content, {pat:String})"
    )
    assert params == {"pat": "TODO|FIXME", "gp_any": ["todo", "fixme"]}
    for pattern, case_sensitive in (
        (r"TODO|FIXME", False),
        (r"TODO.*FIXME", True),
        (r"[0-9]+|\s+", True),
    ):
        sql, params = gp.content_predicate(
            pattern, regex=True, case_sensitive=case_sensitive, index_guard=False
        )
        assert sql == "match(content, {pat:String})", pattern
    assert gp.content_predicate(
        "TODO|FIXME", regex=False, case_sensitive=True, index_guard=False
    ) == (
        "position(content, {pat:String}) > 0",
        {"pat": "TODO|FIXME"},
    )


def test_short_literal_emits_exactly_the_old_statement() -> None:
    assert gp.content_predicate("ab", regex=False, case_sensitive=True) == (
        "position(content, {pat:String}) > 0",
        {"pat": "ab"},
    )


# --------------------------------------------------------------------------
# the property


_ALPHA = "abcAB_(:"


def _gen_regex(rng: random.Random, depth: int = 0) -> str:
    kind = rng.random()
    if depth > 2 or kind < 0.35:
        n = rng.randint(1, 6)
        return "".join(rng.choice(_ALPHA) for _ in range(n)).replace("(", r"\(")
    if kind < 0.5:
        return _gen_regex(rng, depth + 1) + _gen_regex(rng, depth + 1)
    if kind < 0.62:
        return "(" + _gen_regex(rng, depth + 1) + "|" + _gen_regex(rng, depth + 1) + ")"
    if kind < 0.72:
        return (
            "("
            + _gen_regex(rng, depth + 1)
            + ")"
            + rng.choice(["?", "*", "+", "{2}", "{0,3}", "{1,}"])
        )
    if kind < 0.8:
        return (
            _gen_regex(rng, depth + 1)
            + rng.choice([".", r"\w", r"\s*", "[ab]", "[^c]"])
            + _gen_regex(rng, depth + 1)
        )
    if kind < 0.88:
        return (
            rng.choice(["^", r"\b", ""]) + _gen_regex(rng, depth + 1) + rng.choice(["$", r"\b", ""])
        )
    if kind < 0.94:
        return "(?=" + _gen_regex(rng, depth + 1) + ")" + _gen_regex(rng, depth + 1)
    return "(?:" + _gen_regex(rng, depth + 1) + ")" + _gen_regex(rng, depth + 1)


def _gen_text(rng: random.Random, seeds: list[str]) -> str:
    parts = []
    for _ in range(rng.randint(1, 6)):
        if seeds and rng.random() < 0.6:
            s = rng.choice(seeds)
            parts.append(s.swapcase() if rng.random() < 0.3 else s)
        else:
            parts.append("".join(rng.choice(_ALPHA + " \n") for _ in range(rng.randint(0, 8))))
    return "".join(parts)


@pytest.mark.parametrize("seed", range(8))
def test_plan_never_refuses_a_matching_text(seed: int) -> None:
    rng = random.Random(seed)
    checked = 0
    for _ in range(2500):
        pat = _gen_regex(rng)
        try:
            compiled = re.compile(pat)
            compiled_i = re.compile(pat, re.IGNORECASE)
        except re.error:
            continue
        plan = gp.plan_regex(pat)
        literal_seeds = [s for s in re.split(r"[^A-Za-z_:(]+", pat.replace("\\(", "(")) if s]
        for _ in range(6):
            text = _gen_text(rng, literal_seeds)
            for rx in (compiled, compiled_i):
                if rx.search(text) is not None:
                    checked += 1
                    assert gp.admits(plan, text), (pat, text, plan)
    assert checked > 500  # the generator produced real matches, not only refusals


@pytest.mark.parametrize("seed", range(8))
def test_branch_needles_never_refuse_a_matching_text(seed: int) -> None:
    rng = random.Random(500 + seed)
    checked = 0
    for _ in range(2500):
        pat = _gen_regex(rng) + "|" + _gen_regex(rng)
        try:
            compiled = re.compile(pat)
        except re.error:
            continue
        needles = gp.branch_needles(gp.plan_regex(pat))
        if not needles:
            continue
        literal_seeds = [s for s in re.split(r"[^A-Za-z_:(]+", pat.replace("\\(", "(")) if s]
        for _ in range(6):
            text = _gen_text(rng, literal_seeds)
            if compiled.search(text) is not None:
                checked += 1
                low = gp.ascii_lower(text)
                assert any(n in low for n in needles), (pat, text, needles)
    assert checked > 300


@pytest.mark.parametrize("seed", range(3))
def test_literal_plan_never_refuses_a_containing_text(seed: int) -> None:
    rng = random.Random(1000 + seed)
    for _ in range(3000):
        lit = "".join(rng.choice(_ALPHA + "%_\\é") for _ in range(rng.randint(1, 8)))
        text = _gen_text(rng, [lit, lit.upper(), lit.lower()])
        plan = gp.plan_literal(lit)
        if lit in text or gp.ascii_lower(lit) in gp.ascii_lower(text):
            assert gp.admits(plan, text), (lit, text)


def test_any_literal_guard_is_an_or_over_every_needle_or_nothing() -> None:
    sql, params = gp.any_literal_guard(["Block structure", "mismatch in"], prefix="gn")
    assert sql == "(lower(content) LIKE {gn_0:String} OR lower(content) LIKE {gn_1:String})"
    assert params == {"gn_0": "%block structure%", "gn_1": "%mismatch in%"}
    # one needle under an n-gram → no guard at all (an OR missing a branch would narrow)
    assert gp.any_literal_guard(["Block structure", "in"]) == ("", {})
    assert gp.any_literal_guard([]) == ("", {})
    assert gp.any_literal_guard([f"needle{i:02d}" for i in range(gp.MAX_TERMS + 1)]) == ("", {})
    # soundness on the property's texts
    plan = gp.Or(tuple(gp.plan_literal(n) for n in ["Block structure", "mismatch in"]))
    assert gp.admits(plan, "x BLOCK STRUCTURE y") and gp.admits(plan, "…mismatch in…")
    assert not gp.admits(plan, "nothing here")

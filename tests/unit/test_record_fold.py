"""Field-level fold of a typed record onto the record it supersedes."""

from __future__ import annotations

from server.memory.domain.record_fold import canonical, fold, is_empty, restate


def test_incoming_empty_keeps_the_survivor_value() -> None:
    r = fold({"amount": 120.5, "vendor": "ACME"}, {"amount": None, "vendor": ""})
    assert r.merged == {"amount": 120.5, "vendor": "ACME"}
    assert r.filled_from_prior == ["amount", "vendor"] and r.changed == []


def test_spelling_variants_are_agreement_not_conflict() -> None:
    r = fold({"vendor": "Acme  Corp", "city": "ﬁrenze"}, {"vendor": "acme corp", "city": "Firenze"})
    assert r.changed == [] and sorted(r.agreed) == ["city", "vendor"]
    assert r.merged["vendor"] == "acme corp", "incoming spelling is stored"


def test_real_disagreement_is_recorded_and_incoming_wins() -> None:
    r = fold({"amount": 100, "status": "open"}, {"amount": 120, "status": "open"})
    assert r.merged["amount"] == 120
    assert r.changed == [{"field": "amount", "prior": 100, "incoming": 120}]
    assert r.agreed == ["status"]


def test_lists_union_in_prior_order_and_dedupe_canonically() -> None:
    r = fold({"tags": ["Alpha", "beta"]}, {"tags": ["BETA", "gamma", " alpha "]})
    assert r.merged["tags"] == ["Alpha", "beta", "gamma"] and r.unioned == ["tags"]


def test_prior_empty_takes_incoming_as_added_and_new_fields_append() -> None:
    r = fold({"a": "", "b": 1}, {"a": "x", "b": 1, "c": [1]})
    assert r.merged == {"a": "x", "b": 1, "c": [1]}
    assert r.added == ["a", "c"] and r.agreed == ["b"]
    assert list(r.merged) == ["a", "b", "c"], "prior field order first, then new fields"


def test_numbers_and_bools_are_never_empty_and_compare_by_value() -> None:
    assert not is_empty(0) and not is_empty(False) and is_empty("  ") and is_empty(None)
    assert canonical(1.0) == canonical(1.0) and canonical(True) == "true"
    r = fold({"n": 1}, {"n": 1.0})
    assert r.changed == [{"field": "n", "prior": 1, "incoming": 1.0}] or r.agreed == ["n"]


def test_as_dict_is_wire_shaped() -> None:
    d = fold({"a": 1}, {"a": 2}).as_dict()
    assert set(d) == {"agreed", "filled_from_prior", "added", "unioned", "changed"}
    assert d["changed"][0]["field"] == "a"


def test_restate_keeps_nothing_of_the_prior_and_reports_the_change() -> None:
    prior = {"outcome": "rollback", "reasoning": "fast", "tags": ["a"]}
    incoming = {"outcome": "Hotfix", "reasoning": "", "tags": ["a"], "owner": "ops"}
    out = restate(prior, incoming)
    assert out.merged == incoming
    assert out.filled_from_prior == [] and out.unioned == []
    assert out.agreed == ["tags"] and out.added == ["owner"]
    assert out.changed == [{"field": "outcome", "prior": "rollback", "incoming": "Hotfix"}]

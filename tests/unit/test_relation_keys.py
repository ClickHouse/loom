"""A fact with one single-valued relation takes its key from
the relation, so two sessions mint the same key for the same attribute."""

from __future__ import annotations

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.keys import apply_relation_keys, relation_canonical_key


def _f(key: str, value: str, ents: list[str], rels: list[tuple[str, str, str]]) -> ExtractedFact:
    return ExtractedFact(key, value, entities=ents, relations=rels)


def test_single_valued_relation_names_the_key() -> None:
    a = _f(
        "charles_dickens_author_our_mutual_friend",
        "The author of Our Mutual Friend is Charles Dickens.",
        ["our mutual friend", "charles dickens"],
        [("our mutual friend", "author_of", "charles dickens")],
    )
    # author_of is multi-valued in the seed list: the model's key stands
    assert relation_canonical_key(a) is None
    b = _f(
        "apple_ceo",
        "The chief executive officer of Apple Inc. is Tim Cook.",
        ["apple inc", "tim cook"],
        [("apple inc", "ceo_of", "tim cook")],
    )
    c = _f(
        "tim_cook_position",
        "The chief executive officer of Apple Inc. is Vijay Mallya.",
        ["apple inc", "vijay mallya"],
        [("apple inc", "ceo_of", "vijay mallya")],
    )
    assert relation_canonical_key(b) == "apple_inc_ceo_of" == relation_canonical_key(c)
    # a namespace declaration makes author_of single-valued for that corpus
    assert relation_canonical_key(a, {"author_of": "one"}) == "our_mutual_friend_author_of"


def test_ambiguous_or_foreign_relations_keep_the_model_key() -> None:
    two = _f(
        "k",
        "Amy died in Camden Town and was born in London.",
        ["amy winehouse", "camden town", "london"],
        [("amy winehouse", "die_in", "camden town"), ("amy winehouse", "born_in", "london")],
    )
    assert relation_canonical_key(two) is None
    foreign = _f(
        "k",
        "Steamos was developed by ABB Group.",
        ["steamos", "abb group"],
        [("someone else", "ceo_of", "abb group")],
    )
    assert relation_canonical_key(foreign) is None
    none = _f("k", "User likes tea.", ["user"], [])
    assert relation_canonical_key(none) is None
    multi = _f(
        "k",
        "Melanie's friend is Caroline.",
        ["melanie", "caroline"],
        [("melanie", "friend_of", "caroline")],
    )
    assert relation_canonical_key(multi) is None


def test_apply_rewrites_only_what_has_a_canonical_key() -> None:
    b = _f(
        "apple_ceo",
        "The CEO of Apple Inc. is Tim Cook.",
        ["apple inc", "tim cook"],
        [("apple inc", "ceo_of", "tim cook")],
    )
    n = _f("user_tea", "User likes tea.", ["user"], [])
    out = apply_relation_keys([b, n])
    assert [f.memory_key for f in out] == ["apple_inc_ceo_of", "user_tea"]
    assert out[1] is n
    # the value's leading subject counts when the entity list missed it
    v = _f(
        "k",
        '"Andy Warhol died in the city of New York City."',
        ["new york city"],
        [("andy warhol", "die_in", "new york city")],
    )
    assert relation_canonical_key(v) == "andy_warhol_die_in"


def test_the_key_spells_the_relation_as_written_so_it_matches_a_model_key() -> None:
    """The relation's stem decides that two relations are the same; the key
    spells the verb as the model wrote it. A stemmed key (`italy_capit`) never
    equals the key a model writes for the same attribute (`italy_capital`), so
    an update keyed by the relation and its predecessor keyed by the model
    would land on two keys and never supersede."""
    from server.memory.app.extract.parse import _parse_fact_list

    text = (
        '{"facts":[{"key":"italy_capital","value":"The capital of Italy is Duluth.",'
        '"entities":["italy","duluth"],"relations":[{"s":"italy","v":"has_capital","o":"duluth"}]}]}'
    )
    fact = _parse_fact_list(text)[0]
    assert fact.relations == [("italy", "capit", "duluth")]  # the stem, for matching
    assert fact.relation_phrases == ["capital"]
    assert relation_canonical_key(fact) == "italy_capital"


def test_relation_keyed_statements_of_one_batch_keep_the_latest() -> None:
    from server.memory.domain.extracted_facts import keep_latest_relation_keyed

    facts = [
        {"key": "italy_capital", "value": "Rome", "key_from_relation": True},
        {"key": "assistant_recommended", "value": "a"},
        {"key": "italy_capital", "value": "Duluth", "key_from_relation": True},
        {"key": "assistant_recommended", "value": "b"},
    ]
    kept, dropped = keep_latest_relation_keyed(facts)
    assert [f["value"] for f in kept] == ["a", "Duluth", "b"]
    assert dropped == ["italy_capital"]


def test_a_key_one_holder_wrote_itself_is_not_collapsed() -> None:
    from server.memory.domain.extracted_facts import keep_latest_relation_keyed

    facts = [
        {"key": "italy_capital", "value": "Rome"},
        {"key": "italy_capital", "value": "Duluth", "key_from_relation": True},
    ]
    kept, dropped = keep_latest_relation_keyed(facts)
    assert kept == facts and dropped == []


def test_apply_relation_keys_marks_the_keys_it_set() -> None:
    f = _f(
        "apple_ceo",
        "The chief executive officer of Apple Inc. is Tim Cook.",
        ["apple inc", "tim cook"],
        [("apple inc", "ceo_of", "tim cook")],
    )
    g = _f("note", "Something else.", ["x"], [])
    out = apply_relation_keys([f, g])
    assert out[0].key_from_relation and not out[1].key_from_relation

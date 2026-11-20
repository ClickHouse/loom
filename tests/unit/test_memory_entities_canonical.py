"""Entities are canonical where they enter a row (the writer's input, the extractor's
reply), in the form a search's entity filter is canonicalised to. Canonicalisation is
idempotent, so a row that already holds canonical text keeps it."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.domain import graph_quality
from server.memory.domain.graph_quality import (
    MAX_ENTITY_CHARS,
    canonical_entities,
    canonical_entity_text,
)
from server.memory.workers.entity_extractor import _parse_entities


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("api responses", "api response"),
        ("sql databases", "sql database"),
        ("Billing Services", "billing service"),
        ("sizes", "size"),
        ("classes", "class"),
        ("buzzes", "buzz"),
        ("boxes", "box"),
        ("parties", "party"),
        ("shelves", "shelf"),
        ("The Ledger's", "ledger"),
        ("status", "status"),
    ],
)
def test_canonical_text_is_a_fixed_point(raw: str, canonical: str) -> None:
    assert canonical_entity_text(raw) == canonical
    assert canonical_entity_text(canonical) == canonical


def test_canonical_entities_fold_surface_forms_once_in_order() -> None:
    assert canonical_entities(["Billing Services", "billing service", "The Ledger", ""]) == [
        "billing service",
        "ledger",
    ]


def test_the_extractor_reply_is_canonical() -> None:
    assert _parse_entities('["API Responses", "api response", "Kafka"]') == [
        "api response",
        "kafka",
    ]


@pytest.mark.parametrize(
    ("plural", "singular"),
    [
        ("buses", "bus"),
        ("gases", "gas"),
        ("lenses", "lens"),
        ("irises", "iris"),
        ("statuses", "status"),
        ("viruses", "virus"),
        ("classes", "class"),
        ("aliases", "alias"),
        ("responses", "response"),
        ("databases", "database"),
        ("cases", "case"),
        ("uses", "use"),
        ("sizes", "size"),
        ("buzzes", "buzz"),
        ("waltzes", "waltz"),
        ("kits", "kit"),
        ("parties", "party"),
        ("boxes", "box"),
        ("breaches", "breach"),
        ("shelves", "shelf"),
    ],
)
def test_a_plural_and_its_singular_are_one_entity(plural: str, singular: str) -> None:
    assert canonical_entity_text(plural) == canonical_entity_text(singular) == singular


@pytest.mark.parametrize("raw", ['"The Ledger"', "(The Ledger)", "the the ledger", "[a] ledger"])
def test_an_article_behind_punctuation_is_dropped_once(raw: str) -> None:
    assert canonical_entity_text(raw) == "ledger"
    assert canonical_entity_text(canonical_entity_text(raw)) == "ledger"


@pytest.mark.parametrize("raw", ["re.S", "re.S.S.S.S.S", "Re.S.", "v2.x's", "(the) Ledgers."])
def test_trailing_punctuation_is_taken_and_a_dotted_name_kept(raw: str) -> None:
    once = canonical_entity_text(raw)
    assert canonical_entity_text(once) == once
    assert canonical_entity_text("re.S") == "re.s"


@pytest.mark.parametrize(
    ("raw", "canonical"), [("HTTPS", "https"), ("HTTP", "http"), ("DNS", "dns"), ("GCS", "gcs")]
)
def test_an_acronym_is_not_a_plural(raw: str, canonical: str) -> None:
    assert canonical_entity_text(raw) == canonical
    assert canonical_entities(["HTTP", "HTTPS"]) == ["http", "https"]


# each ligature is "fi" once normalised
@pytest.mark.parametrize("n", [MAX_ENTITY_CHARS // 2 + 1, MAX_ENTITY_CHARS])
def test_text_longer_than_the_cap_once_normalised_is_no_entity(n: int) -> None:
    assert canonical_entity_text("\ufb01" * n) == ""


@pytest.mark.parametrize(("prefix", "unit"), [("re", ".S"), ("", "x"), ("", "the "), ("", "s.")])
def test_a_name_at_the_cap_is_a_fixed_point(prefix: str, unit: str) -> None:
    once = canonical_entity_text((prefix + unit * MAX_ENTITY_CHARS)[:MAX_ENTITY_CHARS])
    assert once and canonical_entity_text(once) == once


def test_a_long_name_keeps_all_of_itself() -> None:
    prefix = "https://example.test/" + "x" * 179
    alpha, beta = canonical_entity_text(prefix + "/alpha"), canonical_entity_text(prefix + "/beta")
    assert alpha != beta and alpha.endswith("/alpha")


@pytest.mark.parametrize(
    "raw", ["İstanbul/" + "a" * 191, "İ" * 200, "ß" * 50 + "S", "\ufb01" * (MAX_ENTITY_CHARS // 2)]
)
def test_text_normalising_lengthens_is_a_fixed_point_too(raw: str) -> None:
    once = canonical_entity_text(raw)
    assert once and canonical_entity_text(once) == once


@pytest.mark.parametrize(
    ("raw", "canonical"),
    # "İ" and an accent: the dot goes and the "i" takes the accent ("í"); a second
    # dot goes too. U+03D4 is a symbol NFKC makes an upper-case letter of, and with
    # an accent its lower case is one letter ("ΰ").
    [
        ("x\u0130\u0301y", "x\u00edy"),
        ("x\u0130\u0307y", "xiy"),
        ("\u03d4", "\u03cb"),
        ("x\u03d4\u0301y", "x\u03b0y"),
    ],
)
def test_a_letter_normalising_changes_is_a_fixed_point(raw: str, canonical: str) -> None:
    assert canonical_entity_text(raw) == canonical
    assert canonical_entity_text(canonical) == canonical


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("The .NET Services", "net service"),
        ("the .the .net", "net"),
        (". a . the . ledgers", "ledger"),
        ("a.b", "a.b"),
        ("theory", "theory"),
    ],
)
def test_a_prefix_article_removal_exposes_is_taken_too(raw: str, canonical: str) -> None:
    assert canonical_entity_text(raw) == canonical
    assert canonical_entity_text(canonical) == canonical


@pytest.mark.parametrize(
    "raw",
    # A mark after a possessive: once the "'s" goes, the mark must join the letter
    # before it in the same pass, an upper-case "'S" included.
    ["x's\u0308y", "e'S\u0308e", "\u0130\u0307\u0307\u2019S\u0308", "1\u0386\u0307's\u0e38"],
)
def test_a_mark_after_a_possessive_is_a_fixed_point(raw: str) -> None:
    once = canonical_entity_text(raw)
    assert canonical_entity_text(once) == once


def test_canonical_text_is_a_fixed_point_for_any_mix_of_its_edge_pieces() -> None:
    import random

    pieces = ["the ", "a ", "an ", ".", " ", "'s", "s", "es", "ies", "net", "re", "x", "(", '"']
    pieces += ["error:", "commit:", "`", ")", "[", "]", "<", ">", "{", "}", "\t"]
    rng = random.Random(0)
    for _ in range(20000):
        raw = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 8)))
        once = canonical_entity_text(raw)
        assert canonical_entity_text(once) == once, raw


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("node.js", "node.js"),
        ("foo.rs", "foo.rs"),
        ("cyclic_aliases", "cyclic_alias"),
        ("system.databases", "system.database"),
        ("flaky-tests", "flaky-test"),
        ("Part::setColumnsSizes", "part::setcolumnssize"),
        ("my_status", "my_status"),
        ("foo/", "foo/"),
    ],
)
def test_only_the_last_segment_of_a_name_is_singular(raw: str, canonical: str) -> None:
    assert canonical_entity_text(raw) == canonical
    assert canonical_entity_text(canonical) == canonical


# Frozen examples from this module, test_graph_quality.py, and the combining-mark
# corpus in test_entity_stopwords_and_embed_batch.py, plus coercion and cap edges.
_TEXT_ONLY_CASES = [
    ("api responses", "api response", "api_response", ["api responses"]),
    ("The Ledger's", "ledger", "ledger", ["The Ledger's"]),
    (
        "Alice's   API responses",
        "alice api response",
        "alice_api_response",
        ["Alice's   API responses"],
    ),
    ("  an Acme, Inc.  ", "acme inc", "acme_inc", ["an Acme, Inc."]),
    ("I", "user", "user", ["I"]),
    ("node.js", "node.js", "node.js", ["node.js"]),
    ("system.databases", "system.database", "system.database", ["system.databases"]),
    ("error:Classes", "error:classes", "error:classes", ["error:Classes"]),
    ("दिल्ली", "दिल्ली", "दिल्ली", ["दिल्ली"]),
    ("กรุงเทพ", "กรุงเทพ", "กรุงเทพ", ["กรุงเทพ"]),
    ("Zu\u0308rich", "zürich", "zürich", ["Zu\u0308rich"]),
    ("İstanbul", "istanbul", "istanbul", ["İstanbul"]),
    ("The Straße", "straße", "straße", ["The Straße"]),
    ("Café-Bar!", "café-bar", "café-bar", ["Café-Bar!"]),
    ("ＴＯＫＹＯ", "tokyo", "tokyo", ["ＴＯＫＹＯ"]),
    ("ﬁnance", "finance", "finance", ["ﬁnance"]),
    ("x\u0130\u0301y", "x\u00edy", "x\u00edy", ["x\u0130\u0301y"]),
    ("", "", "", []),
    ("   ", "", "", []),
    (None, "", "", []),
    (False, "", "", []),
    (0, "", "", []),
    ([], "", "", []),
    ({}, "", "", []),
    ((), "", "", []),
    ("...", "", "", ["..."]),
    (
        "x" * MAX_ENTITY_CHARS,
        "x" * MAX_ENTITY_CHARS,
        "x" * MAX_ENTITY_CHARS,
        ["x" * MAX_ENTITY_CHARS],
    ),
    ("x" * (MAX_ENTITY_CHARS + 1), "", "", []),
    (
        "ﬁ" * (MAX_ENTITY_CHARS // 2),
        "fi" * (MAX_ENTITY_CHARS // 2),
        "fi" * (MAX_ENTITY_CHARS // 2),
        ["ﬁ" * (MAX_ENTITY_CHARS // 2)],
    ),
    ("ﬁ" * (MAX_ENTITY_CHARS // 2 + 1), "", "", []),
]


@pytest.mark.parametrize(("raw", "canonical", "entity_id", "aliases"), _TEXT_ONLY_CASES)
def test_text_only_normalization_preserves_text_and_structured_outputs(
    raw: Any, canonical: str, entity_id: str, aliases: list[str]
) -> None:
    entity = graph_quality.canonical_entity(raw)
    assert entity.text == canonical
    assert entity.entity_id == entity_id
    assert entity.aliases == aliases
    assert graph_quality.canonical_entity_text(raw) == entity.text


def test_text_only_normalization_constructs_no_discarded_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_type = graph_quality.CanonicalEntity
    allocations = 0

    def counted_entity(*args: Any, **kwargs: Any) -> Any:
        nonlocal allocations
        allocations += 1
        return entity_type(*args, **kwargs)

    monkeypatch.setattr(graph_quality, "CanonicalEntity", counted_entity)
    actual = [graph_quality.canonical_entity_text(raw) for raw, _, _, _ in _TEXT_ONLY_CASES]
    assert actual == [canonical for _, canonical, _, _ in _TEXT_ONLY_CASES]
    assert allocations == 0

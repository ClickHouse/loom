"""A triple's provenance tier is decided from the text that
carries it, deterministically."""

from __future__ import annotations

from server.memory.domain import triple_provenance as tp


def test_extracted_when_both_endpoints_are_in_the_sentence() -> None:
    assert (
        tp.classify(
            "t e lawrence",
            "university of oxford",
            tp.normalize("T. E. Lawrence is employed by University of Oxford."),
        )
        == "extracted"
    )
    # canonical underscores and prose punctuation meet in the normalised form
    assert (
        tp.classify(
            "new_york", "alan_jay_lerner", tp.normalize("Alan Jay Lerner died in New York City.")
        )
        == "extracted"
    )


def test_inferred_when_an_endpoint_is_absent_or_wrong() -> None:
    # the object contradicts the text
    assert (
        tp.classify(
            "alan jay lerner",
            "mansourah",
            tp.normalize("Alan Jay Lerner died in the city of New York City."),
        )
        == "inferred"
    )
    assert (
        tp.classify(
            "lme-bench",
            "material.io",
            tp.normalize("The App Bars component should be placed at the top."),
        )
        == "inferred"
    )


def test_whole_token_match_only() -> None:
    assert tp.endpoint_in_text("art", tp.normalize("She went to the party.")) is False
    assert (
        tp.endpoint_in_text("ph d", tp.normalize("preparing for their upcoming Ph.D. program"))
        is True
    )
    assert tp.endpoint_in_text("", "anything") is False


def test_carrier_text_flattens_structured_values() -> None:
    assert tp.carrier_text("plain") == "plain"
    assert (
        tp.carrier_text({"employer": "ClickHouse", "since": 2024, "tags": ["db", None]})
        == "employer ClickHouse since 2024 tags db"
    )
    carrier = tp.normalize(tp.carrier_text({"note": "User works at ClickHouse"}))
    assert tp.classify("user", "clickhouse", carrier) == "extracted"

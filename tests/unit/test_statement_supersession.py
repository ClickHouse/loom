"""A superseded row leaves the ask pool and the chunk or summary
sentence that restates it is masked; the extractor's verdict words are not
part of a key."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.app.ask.supersession import mask_stale_sentences, split_pool_by_state
from server.memory.app.key_supersession import strip_variant_suffix


def _hit(key, text, source="fact", status="active"):
    return SimpleNamespace(memory_key=key, content_excerpt=text, source=source, status=status)


def test_superseded_rows_leave_the_pool_and_chunks_stay() -> None:
    hits = [
        _hit("microsoft_ceo", "The chief executive officer of Microsoft is Steve Jobs."),
        _hit(
            "microsoft_ceo_of",
            "The chief executive officer of Microsoft is Satya Nadella.",
            status="superseded",
        ),
        _hit("chunk:c:2", "Some text.", source="chunk", status=""),
    ]
    cur, old = split_pool_by_state(hits)
    assert [h.memory_key for h in cur] == ["microsoft_ceo", "chunk:c:2"]
    assert [h.memory_key for h in old] == ["microsoft_ceo_of"]


def test_stale_sentences_are_masked_in_chunks_and_summaries_only() -> None:
    old = [
        _hit(
            "microsoft_ceo",
            "The chief executive officer of Microsoft is Satya Nadella.",
            status="superseded",
        )
    ]
    hits = [
        _hit("microsoft_ceo", "The chief executive officer of Microsoft is Steve Jobs."),
        _hit(
            "summary:chunk-00002",
            "The chief executive officer of Microsoft is Satya Nadella. Windows Phone was developed by Microsoft. "
            "The chief executive officer of Microsoft is Steve Jobs.",
            source="summary",
            status="",
        ),
        _hit(
            "chunk:chunk-00002:1", "Satya Nadella is a citizen of India.", source="chunk", status=""
        ),
    ]
    out, n = mask_stale_sentences(hits, old)
    assert n == 1
    assert out[0].content_excerpt.endswith("Steve Jobs.")  # fact rows untouched
    assert "Nadella" not in out[1].content_excerpt.split("Windows")[0]
    assert "Windows Phone was developed by Microsoft." in out[1].content_excerpt
    assert "Steve Jobs" in out[1].content_excerpt  # the current statement stays
    # A sentence about the same entity that does not restate the attribute survives.
    assert out[2].content_excerpt == "Satya Nadella is a citizen of India."


def test_masking_is_silent_without_superseded_rows_or_distinguishing_tokens() -> None:
    hits = [_hit("summary:s", "The capital of France is Paris.", source="summary", status="")]
    assert mask_stale_sentences(hits, []) == (hits, 0)
    # Superseded row identical in content to its successor: nothing distinguishes it, nothing masked.
    old = [_hit("k", "The capital of France is Paris.", status="superseded")]
    same = [_hit("k", "The capital of France is Paris."), *hits]
    assert mask_stale_sentences(same, old)[1] == 0


def test_verdict_words_are_stripped_from_keys() -> None:
    assert strip_variant_suffix("france_capital_misstatement") == "france_capital"
    assert strip_variant_suffix("japan_official_language_claim") == "japan_official_language"
    assert strip_variant_suffix("user_city_error") == "user_city"
    assert strip_variant_suffix("user_belief") == "user_belief"  # two parts: whole key


def test_templated_conflicts_resolve_to_the_later_row_on_single_valued_attributes() -> None:
    from server.memory.app.ask.supersession import resolve_templated_conflicts

    def fact(key, text, created):
        return SimpleNamespace(
            memory_key=key, content_excerpt=text, source="fact", status="active", created_at=created
        )

    hits = [
        fact("capital_france", "The capital of France is Paris.", "2026-09-13 16:46:22"),
        fact("capital_france_harare", "The capital of France is Harare.", "2026-09-13 16:46:23"),
        # A different attribute of a different subject with the same template stays.
        fact(
            "apple_inc_ceo_of",
            "The chief executive officer of Apple Inc. is Tim Cook.",
            "2026-09-13 16:40:00",
        ),
        fact(
            "twitter_ceo_of",
            "The chief executive officer of Twitter is Bernard Arnault.",
            "2026-09-13 16:41:00",
        ),
        # A multi-valued preference is not an attribute: both survive.
        fact("melanie_likes_pottery", "Melanie likes pottery.", "2026-09-13 16:00:00"),
        fact("melanie_likes_camping", "Melanie likes camping.", "2026-09-13 16:01:00"),
        SimpleNamespace(
            memory_key="chunk:c:1",
            content_excerpt="The capital of France is Paris.",
            source="chunk",
            status="",
            created_at="",
        ),
    ]
    kept, retired = resolve_templated_conflicts(hits)
    assert [h.memory_key for h in retired] == ["capital_france"]
    assert "capital_france_harare" in [h.memory_key for h in kept]
    assert {
        "apple_inc_ceo_of",
        "twitter_ceo_of",
        "melanie_likes_pottery",
        "melanie_likes_camping",
        "chunk:c:1",
    } <= {h.memory_key for h in kept}
    # The retired row then masks the chunk restating it.
    out, n = mask_stale_sentences(kept, retired)
    assert n == 1 and "Paris" not in out[-1].content_excerpt


def test_same_value_for_different_subjects_is_not_a_conflict() -> None:
    from server.memory.app.ask.supersession import (
        resolve_templated_conflicts,
        templated_conflict,
    )

    # Two subjects sharing a long value must not retire each other
    # (MemoryAgentBench single-hop abstains on rows the pool dropped).
    assert not templated_conflict(
        "Hal Needham is a citizen of United States of America.",
        "Robert A. Heinlein is a citizen of United States of America.",
    )
    assert not templated_conflict(
        "The chief executive officer of Apple Inc. is Tim Cook.",
        "The chief executive officer of Twitter is Bernard Arnault.",
    )
    assert not templated_conflict(
        "The headquarters of The University of the Arts is located in the city of Philadelphia.",
        "The headquarters of University of California, Berkeley is located in the city of Berkeley.",
    )
    # One subject, one attribute, two values — with the value at the end, in
    # the middle, or sharing a token.
    assert templated_conflict("The capital of France is Paris.", "The capital of France is Harare.")
    assert templated_conflict(
        "Robert Parish plays the position of center.",
        "Robert Parish plays the position of quarterback.",
    )
    assert templated_conflict(
        "The author of Our Mutual Friend is Charles Dickens.",
        "The author of Our Mutual Friend is Charles Darwin.",
    )
    hits = [
        SimpleNamespace(
            memory_key="hal_needham_citizen_of",
            content_excerpt="Hal Needham is a citizen of United States of America.",
            source="fact",
            status="active",
            created_at="2026-09-13 17:15:03",
        ),
        SimpleNamespace(
            memory_key="robert_a_heinlein_citizen_of",
            content_excerpt="Robert A. Heinlein is a citizen of United States of America.",
            source="fact",
            status="active",
            created_at="2026-09-13 17:15:04",
        ),
    ]
    kept, retired = resolve_templated_conflicts(hits)
    assert retired == [] and len(kept) == 2


def test_mask_does_not_drop_an_update_the_extractor_missed() -> None:
    from server.memory.app.ask.supersession import (
        mask_stale_sentences,
        resolve_templated_conflicts,
    )

    # The only fact row is the old value; the update lives in a chunk alone.
    # A retired row without a known successor masks only near-verbatim
    # restatements, so the chunk's update sentence stays.
    retired = [
        SimpleNamespace(
            memory_key="kingdom_of_ireland_religion",
            content_excerpt="Kingdom of Ireland is affiliated with the religion of Catholic Church.",
            source="fact",
            status="superseded",
        )
    ]
    chunk = SimpleNamespace(
        memory_key="chunk:c:2",
        content_excerpt="260. Kingdom of Ireland is affiliated with the religion of Zoroastrianism. 261. Kingdom of Ireland is affiliated with the religion of Catholic Church.",
        source="chunk",
        status="",
    )
    out, n = mask_stale_sentences([chunk], retired)
    assert n == 1
    assert "Zoroastrianism" in out[0].content_excerpt and "Catholic" not in out[0].content_excerpt
    # A same-text twin (a same-key re-extraction) has no old value: nothing masked.
    twin = [
        SimpleNamespace(
            memory_key="k",
            content_excerpt="The capital of France is Paris.",
            source="fact",
            status="superseded",
            successor_text="The capital of France is Paris.",
        )
    ]
    c2 = SimpleNamespace(
        memory_key="chunk:c:3",
        content_excerpt="The capital of France is Paris. The user likes it.",
        source="chunk",
        status="",
    )
    out2, n2 = mask_stale_sentences([c2], twin)
    assert n2 == 0 and out2[0].content_excerpt == c2.content_excerpt
    # The resolver's retiree carries its successor, so the old value is exact.
    hits = [
        SimpleNamespace(
            memory_key="capital_france",
            content_excerpt="The capital of France is Paris.",
            source="fact",
            status="active",
            created_at="2026-09-13 16:46:22",
        ),
        SimpleNamespace(
            memory_key="capital_france_harare",
            content_excerpt="The capital of France is Harare.",
            source="fact",
            status="active",
            created_at="2026-09-13 16:46:23",
        ),
        SimpleNamespace(
            memory_key="chunk:c:4",
            content_excerpt="The capital of France is Harare. The capital of France is Paris.",
            source="chunk",
            status="",
            created_at="",
        ),
    ]
    kept, gone = resolve_templated_conflicts(hits)
    assert gone[0].successor_text == "The capital of France is Harare."
    out3, n3 = mask_stale_sentences(kept, gone)
    assert n3 == 1 and out3[-1].content_excerpt == "The capital of France is Harare."


def test_write_order_breaks_a_created_at_tie_by_version_and_long_values_still_conflict() -> None:
    from server.memory.app.ask.supersession import (
        resolve_templated_conflicts,
        templated_conflict,
    )

    # Two rows from one extraction batch carry the same created_at date; the
    # ReplacingMergeTree version (nanosecond write stamp) says which came later.
    hits = [
        SimpleNamespace(
            memory_key="christianity_foundation_taipei",
            content_excerpt="Christianity was founded in the city of Taipei.",
            source="fact",
            status="active",
            created_at="2026-09-13",
            version=1789321188632661750,
        ),
        SimpleNamespace(
            memory_key="christianity_foundation_jerusalem",
            content_excerpt="Christianity was founded in the city of Jerusalem.",
            source="fact",
            status="active",
            created_at="2026-09-13",
            version=1789321188619526163,
        ),
    ]
    kept, retired = resolve_templated_conflicts(hits)
    assert [h.memory_key for h in retired] == ["christianity_foundation_jerusalem"]
    assert kept[0].memory_key == "christianity_foundation_taipei"
    # A titled value ("Tony Hall, Baron Hall of Birkenhead") is still one value.
    assert templated_conflict(
        "The director of British Broadcasting Corporation is Tony Hall, Baron Hall of Birkenhead.",
        "The director of British Broadcasting Corporation is Narendra Modi.",
    )


def test_the_key_tag_on_an_ask_excerpt_is_not_part_of_the_statement() -> None:
    from server.memory.app.ask.supersession import _stale_tokens, templated_conflict

    # Live pool rows read `"<statement>" (#<key>)`; the resolver must not go
    # inert on them because the tags' tokens overlap.
    assert templated_conflict(
        '"Christianity was founded in the city of Jerusalem." (#christianity_foundation_jerusalem)',
        '"Christianity was founded in the city of Taipei." (#christianity_foundation_taipei)',
    )
    assert templated_conflict(
        '"The capital of France is Paris." (#capital_france)',
        '"The capital of France is Harare." (#capital_france_harare)',
    )
    assert _stale_tokens('"The capital of France is Paris." (#capital_france_paris)') == {
        "capital",
        "france",
        "paris",
    }

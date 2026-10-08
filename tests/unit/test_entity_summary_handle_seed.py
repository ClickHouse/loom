"""_seed_entity_summary_entities: the pre-built entity-summary read leg must be
scoped from cheap salient-@handle extraction, not only the enrichment-LLM entity
filter (which is empty on the cheap query path people/handle queries take)."""

from server.memory.app.search.side_legs import _seed_entity_summary_entities as seed


def test_seeds_bare_canonical_handle_from_query():
    # @handle in the query -> canonical (lowercased, @-stripped) entity, even
    # when enrichment produced no filter (a people / handle question on the cheap path).
    out = seed("What do we know about @rdev1 review expectations?", [])
    assert "rdev1" in out


def test_tolerates_space_broken_handle():
    out = seed("What are @ ahmadov's known expectations on our PRs?", [])
    assert "ahmadov" in out


def test_excludes_bare_numbers():
    # PR/issue numbers are salient identifiers but must NOT become entity-summary
    # lookups — only @handle entities get a per-person summary.
    out = seed("what is the state of PR 117841 and issue 117588", [])
    assert out == []


def test_unions_with_enrichment_filter_without_dupes():
    # An enrichment-derived filter is preserved; the handle is added once.
    out = seed("expectations for @tvega", ["tvega", "clickhouse"])
    assert out.count("tvega") == 1
    assert "clickhouse" in out and "tvega" in out


def test_empty_query_is_noop_passthrough():
    assert seed("", ["existing"]) == ["existing"]
    assert seed("no handles here", []) == []


# --- bare-name matching against the summary-plane vocabulary ------------------


def test_matches_bare_name_in_known_entities():
    # A maintainer named WITHOUT an @ is seeded only when it is a real summary
    # entity (present in known_entities).
    known = frozenset({"mirandav", "rdev1", "jordan-reed"})
    out = seed(
        "review lessons and doctrine for maintainer mirandav on ClickHouse PRs",
        [],
        known_entities=known,
    )
    assert "mirandav" in out


def test_matches_hyphenated_bare_name():
    known = frozenset({"jordan-reed"})
    out = seed("who is jordan-reed and what are his review expectations?", [], known_entities=known)
    assert "jordan-reed" in out


def test_bare_word_not_in_vocabulary_is_ignored():
    # A content word that is NOT a summary entity must never be seeded — this is
    # what keeps token-matching precise without an LLM/NER pass.
    known = frozenset({"mirandav"})
    out = seed("what are the review expectations and doctrine", [], known_entities=known)
    assert out == []


def test_no_known_entities_falls_back_to_handles_only():
    # Without a vocabulary, only @handles are seeded (prior behaviour).
    out = seed("doctrine for maintainer mirandav", [], known_entities=None)
    assert out == []
    out2 = seed("doctrine for @mirandav", [], known_entities=None)
    assert "mirandav" in out2


def test_handle_and_bare_name_together():
    known = frozenset({"rdev1", "alexmorgan42"})
    out = seed("reviewer doctrine for alexmorgan42 and @rdev1", [], known_entities=known)
    assert "alexmorgan42" in out and "rdev1" in out
    assert out.count("rdev1") == 1


def test_matches_snake_case_technical_entity():
    # A snake_case test/check name is a real summary entity (per-failure
    # consolidation); the tokenizer must NOT split it on underscores, or the
    # consolidation that links the failure to its fix-PR never surfaces.
    known = frozenset({"00084_external_aggregation", "mirandav"})
    out = seed(
        "status of the 00084_external_aggregation memory limit failure and its fix PR",
        [],
        known_entities=known,
    )
    assert "00084_external_aggregation" in out


def test_snake_case_non_entity_still_ignored():
    # Underscores in the tokenizer must not admit non-entity snake phrases —
    # precision still comes from known_entities membership.
    known = frozenset({"00084_external_aggregation"})
    out = seed("what is the memory_limit_exceeded remedy", [], known_entities=known)
    assert out == []

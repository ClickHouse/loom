from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.entity_resolution import (
    choose_canonical_candidate,
    lookup_canonical_entities,
    resolve_entities_for_write,
)


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ):
        self.insert_calls.append((table, rows, settings or {}))


def test_choose_canonical_candidate_merges_high_similarity_low_entropy() -> None:
    decision = choose_canonical_candidate(
        "Northwind AI",
        ["northwindai", "unrelated product"],
    )

    assert decision.canonical_text == "northwindai"
    assert decision.strategy == "fuzzy"
    assert decision.confidence >= 0.95


def test_choose_canonical_candidate_keeps_ambiguous_short_name_separate() -> None:
    decision = choose_canonical_candidate("acme", ["acme labs", "acme corp"])

    assert decision.canonical_text == "acme"
    assert decision.strategy == "self"


@pytest.mark.asyncio
async def test_resolve_entities_for_write_is_namespace_scoped_and_persists() -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "mention_text": "northwindai",
                "canonical_text": "northwindai",
                "confidence": 1.0,
                "evidence_count": 2,
            }
        ]
    )

    resolved = await resolve_entities_for_write(
        ch,
        org="clickhouse",
        namespace="private-agent",
        entity_texts=["Northwind AI"],
    )

    assert resolved["northwind ai"] == "northwindai"
    sql, params = ch.query_all_calls[0]
    assert "namespace = {ns:String}" in sql
    assert params["ns"] == "private-agent"
    table, rows, settings = ch.insert_calls[0]
    assert table == "loom.entity_canonical_map"
    assert rows[0]["mention_text"] == "northwind ai"
    assert rows[0]["canonical_text"] == "northwindai"
    assert rows[0]["strategy"] == "fuzzy"
    # Synchronous insert: later sessions of the same multi-session ingest
    # must be able to read this resolution back immediately.
    assert settings["async_insert"] == 0
    assert settings["wait_for_async_insert"] == 1


@pytest.mark.asyncio
async def test_lookup_canonical_entities_read_only_expands_aliases() -> None:
    ch = StubCH()
    ch.query_all_returns.append([{"canonical_text": "northwindai"}])

    out = await lookup_canonical_entities(
        ch,
        org="clickhouse",
        namespaces=["private-agent"],
        entity_texts=["Northwind AI"],
    )

    assert out == ["northwind ai", "northwindai"]
    assert ch.insert_calls == []
    sql, params = ch.query_all_calls[0]
    assert "namespace IN {namespaces:Array(String)}" in sql
    assert params["namespaces"] == ["private-agent"]


# ----------------------------------------------------------------------
# Short-name over-merge guard
# ----------------------------------------------------------------------


def test_short_name_mismatch_blocks_near_identical_short_heads() -> None:
    """High string similarity on short tokens joins genuinely distinct
    names (a one-char edit flips the referent): the fuzzy gate must
    refuse, leaving the mention self-canonical."""
    decision = choose_canonical_candidate("qin dynasty", ["qing dynasty"])

    assert decision.canonical_text == "qin dynasty"
    assert decision.strategy == "self"


def test_short_name_guard_allows_separator_variants() -> None:
    """Pure spacing/punctuation variants compact to the same surface and
    must keep merging."""
    decision = choose_canonical_candidate("white water rafting", ["whitewater rafting"])

    assert decision.canonical_text == "whitewater rafting"
    assert decision.strategy == "fuzzy"


def test_short_name_guard_allows_long_token_typos() -> None:
    """Long-token near-duplicates (typos) still merge; direction is
    handled separately by evidence weighting."""
    decision = choose_canonical_candidate("naomi alderman", ["naomi alderdman"])

    assert decision.strategy == "fuzzy"


# ----------------------------------------------------------------------
# Evidence-weighted canonical direction
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeat_observation_flips_canonical_away_from_typo() -> None:
    """A correct spelling repeatedly observed must overtake a typo'd
    canonical that was merely written first: the cluster re-points to the
    better-attested variant."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {  # the typo's self-row: directly observed once
                "mention_text": "naomi alderdman",
                "canonical_text": "naomi alderdman",
                "confidence": 1.0,
                "evidence_count": 1,
                "created_at": None,
            },
            {  # the correct spelling, folded INTO the typo
                "mention_text": "naomi alderman",
                "canonical_text": "naomi alderdman",
                "confidence": 0.96,
                "evidence_count": 1,
                "created_at": None,
            },
        ]
    )

    resolved = await resolve_entities_for_write(
        ch,
        org="clickhouse",
        namespace="private-agent",
        entity_texts=["Naomi Alderman"],
    )

    # This observation makes the correct spelling's attestation (2) exceed
    # the typo's (1) -> direction flips.
    assert resolved["naomi alderman"] == "naomi alderman"
    _, rows, _ = ch.insert_calls[0]
    by_mention = {r["mention_text"]: r for r in rows}
    assert by_mention["naomi alderman"]["canonical_text"] == "naomi alderman"
    assert by_mention["naomi alderman"]["strategy"] == "self"
    assert by_mention["naomi alderman"]["evidence_count"] == 2
    # The old canonical's cluster (its own self-row) is re-pointed.
    assert by_mention["naomi alderdman"]["canonical_text"] == "naomi alderman"
    assert by_mention["naomi alderdman"]["evidence_count"] == 1


@pytest.mark.asyncio
async def test_better_attested_canonical_keeps_direction() -> None:
    """When the existing canonical's direct attestation still dominates,
    the mapping is reused unchanged (no flip, no re-point)."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "mention_text": "naomi alderdman",
                "canonical_text": "naomi alderdman",
                "confidence": 1.0,
                "evidence_count": 5,
                "created_at": None,
            },
            {
                "mention_text": "naomi alderman",
                "canonical_text": "naomi alderdman",
                "confidence": 0.96,
                "evidence_count": 1,
                "created_at": None,
            },
        ]
    )

    resolved = await resolve_entities_for_write(
        ch,
        org="clickhouse",
        namespace="private-agent",
        entity_texts=["Naomi Alderman"],
    )

    assert resolved["naomi alderman"] == "naomi alderdman"
    _, rows, _ = ch.insert_calls[0]
    assert len(rows) == 1
    assert rows[0]["strategy"] == "existing"
    assert rows[0]["canonical_text"] == "naomi alderdman"


@pytest.mark.asyncio
@pytest.mark.parametrize("novel", [False, True], ids=["known", "novel-uuid"])
async def test_payload_map_normalization_work_is_linear_once(monkeypatch, novel) -> None:
    """A map row needs its two text fields normalized, not six full parses."""
    from copy import deepcopy
    from datetime import datetime
    from types import SimpleNamespace

    from bench.cell_trigger_gate import _payload
    from server.core.ch_writes import committed_insert_settings
    from server.memory.app import entity_resolution as owner
    from server.memory.app.projections import resolve_projection_entity_map
    from server.memory.app.set_entities import set_entities_for_projection

    input = SimpleNamespace(
        org="clickhouse", namespace="private-agent", entities=None, tags=None, source_trace=""
    )
    created, now = datetime(2024, 1, 2), datetime(2025, 1, 2)
    seed = f"gate-{'0' * 32}-0-0"
    known = set_entities_for_projection(input, _payload(seed))
    mentions = dict.fromkeys(known)
    for index in range(1, 33):
        key = f"gate-{'0' * 32}-0-{index}"
        mentions.update(dict.fromkeys(set_entities_for_projection(input, _payload(key))))
        if len(mentions) >= 32:
            break
    map_mentions = list(mentions)[:32]
    assert len(map_mentions) == 32 and set(known) <= set(map_mentions)
    rows = [
        {
            "mention_text": mention,
            "canonical_text": mention,
            "confidence": 1.0,
            "evidence_count": 2,
            "created_at": created,
        }
        for mention in map_mentions
    ]
    before = deepcopy(rows)
    entities = (
        set_entities_for_projection(input, _payload(f"gate-{'f' * 32}-7-607")) if novel else known
    )
    assert bool(set(entities) - set(map_mentions)) is novel
    ch = StubCH()
    ch.query_all_returns.append(rows)
    normalize = owner.canonical_entity_text
    calls = 0

    def counted_normalize(raw):
        nonlocal calls
        calls += 1
        return normalize(raw)

    monkeypatch.setattr(owner, "canonical_entity_text", counted_normalize)
    monkeypatch.setattr(owner, "naive_utc_now", lambda: now)
    resolved = await resolve_projection_entity_map(input, entities, ch=ch)
    assert resolved == {mention: mention for mention in entities}
    expected = [
        {
            "org": "clickhouse",
            "namespace": "private-agent",
            "mention_text": mention,
            "canonical_text": mention,
            "confidence": 1.0,
            "strategy": "existing" if mention in map_mentions else "self",
            "evidence_count": 3 if mention in map_mentions else 1,
            "created_at": created if mention in map_mentions else now,
            "updated_at": now,
            "tombstoned_at": None,
        }
        for mention in sorted(set(entities))
    ]
    assert ch.insert_calls == [("loom.entity_canonical_map", expected, committed_insert_settings())]
    sql, params = ch.query_all_calls[0]
    assert len(ch.query_all_calls) == 1
    assert "FROM loom.entity_canonical_map FINAL" in sql
    assert "org = {org:String}" in sql and "namespace = {ns:String}" in sql
    assert "tombstoned_at IS NULL" in sql and "LIMIT {lim:UInt32}" in sql
    assert params == {"org": "clickhouse", "ns": "private-agent", "lim": 5000}
    assert rows == before
    # Each fetched row contributes two normalized text fields. Mentions retain
    # their existing normalization, matching and similarity work allowance.
    assert calls <= 2 * len(rows) + 8 * len(entities) + 8
    # Equal rendered row fields share one parse; distinct fields still need both.
    assert calls <= len(rows) + 4 * len(entities)


@pytest.mark.asyncio
@pytest.mark.parametrize("self_evidence", [1, 5], ids=["flip-cluster", "keep-best-attested"])
async def test_normalized_map_preserves_duplicate_rows_and_attestation(
    monkeypatch, self_evidence
) -> None:
    from copy import deepcopy
    from datetime import datetime

    from server.core.ch_writes import committed_insert_settings
    from server.memory.app import entity_resolution as owner

    created, duplicate_created, now = (
        datetime(2024, 1, 2),
        datetime(2024, 2, 3),
        datetime(2025, 1, 2),
    )
    rows = [
        {
            "mention_text": "naomi alderdman",
            "canonical_text": "naomi alderdman",
            "confidence": 1.0,
            "evidence_count": self_evidence,
            "created_at": created,
        },
        {
            "mention_text": "naomi alderdman",
            "canonical_text": "naomi alderdman",
            "confidence": 1.0,
            "evidence_count": 1,
            "created_at": duplicate_created,
        },
        {
            "mention_text": "naomi alderman",
            "canonical_text": "naomi alderdman",
            "confidence": 0.85,
            "evidence_count": 0,
            "created_at": created,
        },
        {
            "mention_text": " Naomi Alderman ",
            "canonical_text": "naomi alderdman",
            "confidence": 0.96,
            "evidence_count": 1,
            "created_at": duplicate_created,
        },
        {
            "mention_text": "",
            "canonical_text": "",
            "confidence": 1.0,
            "evidence_count": 0,
            "created_at": None,
        },
    ]
    before = deepcopy(rows)
    ch = StubCH()
    ch.query_all_returns.append(rows)
    monkeypatch.setattr(owner, "naive_utc_now", lambda: now)
    resolved = await owner.resolve_entities_for_write(
        ch, org="clickhouse", namespace="private-agent", entity_texts=["Naomi Alderman"]
    )
    flip = self_evidence == 1
    expected_canonical = "naomi alderman" if flip else "naomi alderdman"
    assert resolved == {"naomi alderman": expected_canonical}
    expected = [
        {
            "org": "clickhouse",
            "namespace": "private-agent",
            "mention_text": "naomi alderman",
            "canonical_text": expected_canonical,
            "confidence": 1.0 if flip else 0.96,
            "strategy": "self" if flip else "existing",
            "evidence_count": 2,
            "created_at": duplicate_created,
            "updated_at": now,
            "tombstoned_at": None,
        }
    ]
    if flip:
        expected.extend(
            {
                "org": "clickhouse",
                "namespace": "private-agent",
                "mention_text": "naomi alderdman",
                "canonical_text": "naomi alderman",
                "confidence": 28 / 29,
                "strategy": "fuzzy",
                "evidence_count": 1,
                "created_at": born,
                "updated_at": now,
                "tombstoned_at": None,
            }
            for born in (created, duplicate_created)
        )
    assert ch.insert_calls == [("loom.entity_canonical_map", expected, committed_insert_settings())]
    assert rows == before
    assert ch.query_all_calls[0][1] == {"org": "clickhouse", "ns": "private-agent", "lim": 5000}
    fuzzy = owner.choose_canonical_candidate("Northwind AI", ["northwindai", "unrelated product"])
    assert fuzzy.canonical_text == "northwindai" and fuzzy.strategy == "fuzzy"
    assert fuzzy.confidence >= 0.95
    ambiguous = owner.choose_canonical_candidate("acme", ["acme labs", "acme corp"])
    assert ambiguous.canonical_text == "acme" and ambiguous.strategy == "self"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Northwind AI", "northwindai"),
        ("white water rafting", "whitewater rafting"),
        ("naomi alderman", "naomi alderdman"),
        ("qin dynasty", "qing dynasty"),
        ("Aster's cloud", "aster cloud"),
        ("Aster’s cloud", "aster-cloud"),
        ("ＴＯＫＹＯ cloud", "tokyo cloud"),
        ("İstanbul service", "istanbul-service"),
        ("cafe\u0301 cluster", "cafécluster"),
        ("user", "I"),
        ("", "northwind"),
        ("...", "---"),
    ],
)
def test_similarity_preserves_frozen_raw_scorer(left: str, right: str) -> None:
    from difflib import SequenceMatcher

    from server.memory.app import entity_resolution as owner

    # The prefilter oracle imports the live scorer, so retain an independent one.
    a_canon = owner.canonical_entity_text(left)
    b_canon = owner.canonical_entity_text(right)
    if not a_canon or not b_canon:
        expected = 0.0
    elif a_canon == b_canon:
        expected = 1.0
    else:
        compact_a = owner.compact_entity_text(a_canon)
        compact_b = owner.compact_entity_text(b_canon)
        expected = float(
            max(
                [
                    SequenceMatcher(None, a_canon, b_canon).ratio(),
                    SequenceMatcher(None, compact_a, compact_b).ratio()
                    if compact_a and compact_b
                    else 0.0,
                ]
            )
        )
    assert owner.entity_similarity(left, right) == expected
    assert owner.compact_entity_text(left) == "".join(c for c in a_canon if c.isalnum())
    assert owner.compact_entity_text(right) == "".join(c for c in b_canon if c.isalnum())


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("Northwind AI", "northwindai", 1.0),
        ("naomi alderman", "naomi alderdman", 28 / 29),
        ("ＴＯＫＹＯ cloud", "tokyo cloud", 1.0),
        ("", "northwind", 0.0),
    ],
    ids=["compact-pair", "typo-pair", "equal-control", "empty-control"],
)
def test_similarity_normalizes_only_raw_inputs(monkeypatch, left, right, expected) -> None:
    from server.memory.app import entity_resolution as owner

    original = owner.canonical_entity_text
    calls = []

    def counted(raw):
        calls.append(raw)
        return original(raw)

    monkeypatch.setattr(owner, "canonical_entity_text", counted)
    assert owner.entity_similarity(left, right) == expected
    # The compact filter receives values already normalized above.
    assert calls == [left, right]


def _frozen_raw_entity_similarity(left, right):
    from difflib import SequenceMatcher

    from server.memory.domain.graph_quality import canonical_entity_text

    left = canonical_entity_text(left)
    right = canonical_entity_text(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    compact_left = "".join(char for char in left if char.isalnum())
    compact_right = "".join(char for char in right if char.isalnum())
    return float(
        max(
            [
                SequenceMatcher(None, left, right).ratio(),
                SequenceMatcher(None, compact_left, compact_right).ratio()
                if compact_left and compact_right
                else 0.0,
            ]
        )
    )


def _frozen_raw_entity_decision(
    mention_text, candidates, *, min_score=0.95, max_entropy=0.55, min_gap=0.02
):
    from server.memory.app import entity_resolution as owner
    from server.memory.domain.graph_quality import canonical_entity_text

    mention = canonical_entity_text(mention_text)
    if not mention:
        return owner.EntityResolutionDecision("", "", 0.0, "empty")
    raw = [candidate[0] if isinstance(candidate, tuple) else candidate for candidate in candidates]
    unique = sorted({canonical_entity_text(candidate) for candidate in raw if candidate})
    if mention in unique:
        return owner.EntityResolutionDecision(mention, mention, 1.0, "exact")
    scored = sorted(
        ((candidate, _frozen_raw_entity_similarity(mention, candidate)) for candidate in unique),
        key=lambda item: item[1],
        reverse=True,
    )
    if not scored:
        return owner.EntityResolutionDecision(mention, mention, 1.0, "self")
    best, best_score = scored[0]
    second = scored[1][1] if len(scored) > 1 else 0.0
    entropy = owner.normalized_entropy([score for _, score in scored if score >= min_score])
    if (
        best_score >= min_score
        and best_score - second >= min_gap
        and entropy <= max_entropy
        and not owner._short_name_mismatch(mention, best)
    ):
        return owner.EntityResolutionDecision(mention, best, best_score, "fuzzy")
    return owner.EntityResolutionDecision(mention, mention, 1.0, "self")


@pytest.mark.parametrize("shape", ["prepared", "tuples", "raw"])
def test_prepared_scoring_reuses_pair_surfaces_and_preserves_ratio_order(monkeypatch, shape):
    from difflib import SequenceMatcher

    from server.memory.app import entity_resolution as owner

    raw = ["northwindai", "north-wind-ai"]
    prepared = owner._prepare_candidates(raw)
    assert len(prepared) == 2
    assert all(len(candidate) == 4 for candidate in prepared)
    expected = _frozen_raw_entity_decision("Northwind AI", raw)
    assert expected == owner.EntityResolutionDecision("northwind ai", "northwind ai", 1.0, "self")
    candidates = raw if shape == "raw" else list(prepared) if shape == "tuples" else prepared
    normalize = owner.canonical_entity_text
    normalizations, ratios = [], []

    def counted_normalize(value):
        normalizations.append(value)
        return normalize(value)

    def observed_ratio(*args, **kwargs):
        ratios.append((args[1], args[2]))
        return SequenceMatcher(*args, **kwargs)

    monkeypatch.setattr(owner, "canonical_entity_text", counted_normalize)
    monkeypatch.setattr(owner, "SequenceMatcher", observed_ratio)
    assert owner.choose_canonical_candidate("Northwind AI", candidates) == expected
    # Both compact-equal candidates must still be scored, in stable canonical order.
    assert ratios == [
        surface
        for canonical, compact, _length, _compact_length in prepared
        for surface in (("northwind ai", canonical), ("northwindai", compact))
    ]
    # Mention parsing/compacting and raw candidate preparation remain unchanged;
    # scoring must not normalize either already prepared surface again.
    assert normalizations == ["Northwind AI", *(raw if shape == "raw" else []), "northwind ai"]


@pytest.mark.parametrize("shape", ["prepared", "tuples", "raw"])
@pytest.mark.parametrize(
    ("mention", "raw"),
    [
        ("Northwind AI", ["northwindai", "unrelated product"]),
        ("Northwind AI", ["northwindai", "north-wind-ai"]),
        ("naomi alderman", ["naomi alderdman"]),
        ("qin dynasty", ["qing dynasty"]),
        ("white water rafting", ["whitewater rafting"]),
        ("ＴＯＫＹＯ cloud", ["tokyo cloud"]),
        ("...", ["---"]),
        ("ClickHouse", [" CLICKHOUSE "]),
        ("novel entity", []),
    ],
)
def test_prepared_decision_matches_independent_raw_scorer(shape, mention, raw):
    from server.memory.app import entity_resolution as owner

    prepared = owner._prepare_candidates(raw)
    candidates = raw if shape == "raw" else list(prepared) if shape == "tuples" else prepared
    assert owner.choose_canonical_candidate(mention, candidates) == _frozen_raw_entity_decision(
        mention, raw
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("commit:ABC123s", "commit:abc123s"),
        ("Aster’s service", "aster-service"),
        ("cafe\u0301 cluster", "cafécluster"),
    ],
)
def test_generic_similarity_keeps_raw_normalization_contract(monkeypatch, left, right):
    from server.memory.app import entity_resolution as owner

    expected = _frozen_raw_entity_similarity(left, right)
    normalize = owner.canonical_entity_text
    calls = []

    def counted(value):
        calls.append(value)
        return normalize(value)

    monkeypatch.setattr(owner, "canonical_entity_text", counted)
    assert owner.entity_similarity(left, right) == expected
    assert calls == [left, right]


@pytest.mark.asyncio
@pytest.mark.parametrize("self_evidence", [0, 2], ids=["redirect", "attested-fuzzy"])
async def test_prepared_scoring_preserves_complete_resolver_rows(monkeypatch, self_evidence):
    from copy import deepcopy
    from datetime import datetime

    from server.core.ch_writes import committed_insert_settings
    from server.memory.app import entity_resolution as owner

    created, now = datetime(2024, 1, 2), datetime(2025, 1, 2)
    rows = [
        {
            "mention_text": "northwindai",
            "canonical_text": "northwindai",
            "confidence": 1.0,
            "evidence_count": self_evidence,
            "created_at": created,
        },
        {
            "mention_text": "previous alias",
            "canonical_text": "northwindai",
            "confidence": 0.8,
            "evidence_count": 3,
            "created_at": created,
        },
    ]
    before = deepcopy(rows)
    actual, reference = StubCH(), StubCH()
    actual.query_all_returns.append(rows)
    reference.query_all_returns.append(deepcopy(rows))
    monkeypatch.setattr(owner, "naive_utc_now", lambda: now)
    args = dict(org="clickhouse", namespace="private-agent", entity_texts=["Northwind AI"])
    resolved = await owner.resolve_entities_for_write(actual, **args)
    with monkeypatch.context() as control:
        control.setattr(owner, "choose_canonical_candidate", _frozen_raw_entity_decision)
        control.setattr(owner, "entity_similarity", _frozen_raw_entity_similarity)
        expected = await owner.resolve_entities_for_write(reference, **args)
    assert resolved == expected
    assert actual.insert_calls == reference.insert_calls
    assert actual.query_all_calls == reference.query_all_calls
    assert actual.query_all_calls[0][1] == {"org": "clickhouse", "ns": "private-agent", "lim": 5000}
    canonical = "northwind ai" if self_evidence == 0 else "northwindai"
    assert resolved == {"northwind ai": canonical}
    table, inserted, settings = actual.insert_calls[0]
    assert table == "loom.entity_canonical_map" and settings == committed_insert_settings()
    assert len(inserted) == (3 if self_evidence == 0 else 1)
    assert inserted[0] == {
        "org": "clickhouse",
        "namespace": "private-agent",
        "mention_text": "northwind ai",
        "canonical_text": canonical,
        "confidence": 1.0,
        "strategy": "self" if self_evidence == 0 else "fuzzy",
        "evidence_count": 1,
        "created_at": now,
        "updated_at": now,
        "tombstoned_at": None,
    }
    assert all(set(row) == set(inserted[0]) for row in inserted)
    assert all(
        row["org"] == "clickhouse" and row["namespace"] == "private-agent" for row in inserted
    )
    assert all(row["updated_at"] == now and row["tombstoned_at"] is None for row in inserted)
    if self_evidence == 0:
        assert [row["created_at"] for row in inserted[1:]] == [created, created]
        assert [row["evidence_count"] for row in inserted[1:]] == [0, 3]
        assert all(row["canonical_text"] == canonical for row in inserted[1:])
    assert rows == before


@pytest.mark.parametrize("shape", ["prepared", "tuples", "raw"])
@pytest.mark.parametrize(
    ("mention", "suffixes"),
    [
        ("893", ["83", "89", "892", "93"]),
        ("1234", ["123", "124", "134"]),
    ],
)
def test_ambiguous_candidate_gap_stops_unnecessary_scores(monkeypatch, shape, mention, suffixes):
    from server.memory.app import entity_resolution as owner

    prefix = "gate-95e31a07d4c84b62a90f65de28c713bf-2-"
    mention = prefix + mention
    raw = [prefix + suffix for suffix in suffixes]
    expected = _frozen_raw_entity_decision(mention, raw)
    assert expected == owner.EntityResolutionDecision(mention, mention, 1.0, "self")
    prepared = owner._prepare_candidates(raw)
    candidates = raw if shape == "raw" else list(prepared) if shape == "tuples" else prepared
    score_pair = owner._prepared_similarity
    pairs = []

    def observed_score(left, right, compact_left, compact_right):
        pairs.append((left, right))
        return score_pair(left, right, compact_left, compact_right)

    monkeypatch.setattr(owner, "_prepared_similarity", observed_score)
    assert owner.choose_canonical_candidate(mention, candidates) == expected
    # Two exact scores already make the winner gap impossible, even for a later score of one.
    assert pairs == [(mention, candidate[0]) for candidate in prepared[:2]]


@pytest.mark.parametrize("min_gap", [0.0, -0.1, 0.01, 1.0 - 84 / 85, float("nan")])
def test_ambiguous_gap_bound_keeps_smaller_or_unordered_gap_scoring(monkeypatch, min_gap):
    from server.memory.app import entity_resolution as owner

    prefix = "gate-95e31a07d4c84b62a90f65de28c713bf-2-"
    mention = prefix + "893"
    raw = [prefix + suffix for suffix in ("83", "89", "892", "93")]
    expected = _frozen_raw_entity_decision(mention, raw, min_gap=min_gap, max_entropy=1.0)
    prepared = owner._prepare_candidates(raw)
    score_pair = owner._prepared_similarity
    pairs = []

    def observed_score(left, right, compact_left, compact_right):
        pairs.append((left, right))
        return score_pair(left, right, compact_left, compact_right)

    monkeypatch.setattr(owner, "_prepared_similarity", observed_score)
    assert (
        owner.choose_canonical_candidate(mention, prepared, min_gap=min_gap, max_entropy=1.0)
        == expected
    )
    # Equality still admits a winner; zero, negative and NaN gaps cannot prove rejection.
    assert pairs == [(mention, candidate[0]) for candidate in prepared]


def test_one_high_candidate_still_resolves_fuzzy():
    from server.memory.app import entity_resolution as owner

    prefix = "gate-95e31a07d4c84b62a90f65de28c713bf-2-"
    mention, candidate = prefix + "893", prefix + "89"
    expected = _frozen_raw_entity_decision(mention, [candidate])
    assert expected == owner.EntityResolutionDecision(mention, candidate, 84 / 85, "fuzzy")
    assert owner.choose_canonical_candidate(mention, [candidate]) == expected


@pytest.mark.parametrize("gap", ["default", "equal", "above"])
def test_gap_bound_leaves_room_for_later_perfect_candidate(monkeypatch, gap):
    import math

    from server.memory.app import entity_resolution as owner

    family = "95e31a07d4c84b62a90f65de28c713bf"
    prefix = f"gate-{family}-2-"
    mention = prefix + "893"
    later = f"gate.{family}-2-893"
    raw = [prefix + "813", prefix + "823", later]
    boundary = 1.0 - 42 / 43
    min_gap = 0.02 if gap == "default" else boundary
    if gap == "above":
        min_gap = math.nextafter(boundary, math.inf)
    expected = _frozen_raw_entity_decision(mention, raw, min_gap=min_gap, max_entropy=1.0)
    assert expected == owner.EntityResolutionDecision(
        mention, mention if gap == "above" else later, 1.0, "self" if gap == "above" else "fuzzy"
    )
    prepared = owner._prepare_candidates(raw)
    assert [candidate[0] for candidate in prepared] == raw
    score_pair = owner._prepared_similarity
    pairs = []

    def observed_score(left, right, compact_left, compact_right):
        pairs.append((left, right))
        return score_pair(left, right, compact_left, compact_right)

    monkeypatch.setattr(owner, "_prepared_similarity", observed_score)
    assert (
        owner.choose_canonical_candidate(mention, prepared, min_gap=min_gap, max_entropy=1.0)
        == expected
    )
    if gap != "above":
        assert pairs == [(mention, candidate) for candidate in raw]

"""A chunk or digest whose text cites a fact key in the pool
is ranked just under that fact, whatever its own similarity."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from server.memory.domain.fact_citations import cited_keys, defer_citing_rows


def _hit(key, score, *, source="fact", excerpt=""):
    return SimpleNamespace(
        memory_key=key,
        rank_score=score,
        source=source,
        content_excerpt=excerpt,
        score_components={"rank_score_raw": score},
    )


def test_cited_keys_read_backticks_hashes_and_reader_labels() -> None:
    text = "You live in Ujjain, saved in Loom as `where_i_live` (#fav_place); see [user_home_location] and plain where_i_live"
    assert cited_keys(text) == ["where_i_live", "fav_place", "user_home_location"]
    assert cited_keys("no keys here, just #tag and `x`") == []


def test_chunk_and_summary_citing_the_fact_drop_just_under_it() -> None:
    fact = _hit("where_i_live", 0.414)
    chunk = _hit(
        "chunk:a3f0:0",
        0.641,
        source="chunk",
        excerpt="[assistant] You live in Ujjain, saved in Loom as `where_i_live` on 2026-09-19.",
    )
    digest = _hit(
        "summary:a3f0",
        0.454,
        source="summary",
        excerpt="On 2026-09-19 the user was informed that they live in Ujjain, saved in Loom as `where_i_live`.",
    )
    other_chunk = _hit(
        "chunk:6518:0",
        0.583,
        source="chunk",
        excerpt="[user] and where do i live? [assistant] I'll check.",
    )
    hits = [chunk, other_chunk, digest, fact]
    assert defer_citing_rows(hits) == 2
    assert chunk.rank_score < fact.rank_score and digest.rank_score < fact.rank_score
    assert chunk.score_components["cites_fact"] == "where_i_live"
    assert chunk.score_components["rank_score_before_defer"] == 0.641
    assert other_chunk.rank_score == 0.583, "a row citing nothing in the pool is untouched"
    ranked = sorted(hits, key=lambda h: -h.rank_score)
    assert [h.memory_key for h in ranked][:2] == ["chunk:6518:0", "where_i_live"]


def test_no_facts_or_no_citations_means_no_change() -> None:
    chunk = _hit("chunk:x", 0.9, source="chunk", excerpt="mentions `where_i_live`")
    assert defer_citing_rows([chunk]) == 0 and chunk.rank_score == 0.9
    fact = _hit("k", 0.5)
    low = _hit("summary:y", 0.2, source="summary", excerpt="cites `k` but already below")
    assert defer_citing_rows([fact, low]) == 0 and low.rank_score == 0.2


def test_fusion_applies_the_rule_before_the_sort() -> None:
    from server.memory.app.search.ranking import _apply_global_fusion

    src = inspect.getsource(_apply_global_fusion)
    assert src.index("_defer_citing_rows(all_hits)") < src.index("all_hits.sort(key=hit_sort_key)")


def test_the_assembled_block_keeps_a_citing_digest_under_the_fact_after_calibration() -> None:
    """The block assembler recalibrates digest scores after fusion,
    so the cap is applied again there — the served window, not only the pool."""
    from server.memory.domain.context_block import assemble_context_block

    def _row(key, score, source, excerpt, sid="-"):
        return SimpleNamespace(
            memory_key=key,
            rank_score=score,
            distance=1 - score,
            source=source,
            content_excerpt=excerpt,
            score_components={
                "rank_score_raw": score,
                **({"summary_cosine": score} if source == "summary" else {}),
            },
            namespace="jdoe",
            session_id=sid,
            user_id="-",
            speaker="",
            entities=[],
            memory_type="semantic",
            tags=[],
            valid_at=None,
            created_at=None,
        )

    fact = _row("where_i_live", 0.423, "fact", '"indore" (#where_i_live)')
    citing_digest = _row(
        "summary:a3f0",
        0.58,
        "summary",
        "the user was informed that they live in Ujjain, saved in Loom as `where_i_live`.",
        "a3f0",
    )
    other_digest = _row(
        "summary:zz", 0.30, "summary", "the user saved their favorite place as the mountain.", "zz"
    )
    plain_chunk = _row(
        "chunk:6518:0",
        0.583,
        "chunk",
        "[user] and where do i live? [assistant] I'll check.",
        "6518",
    )
    out = assemble_context_block([plain_chunk, citing_digest, fact, other_digest], top_k=4)
    keys = [h.memory_key for h in out]
    assert keys.index("where_i_live") < keys.index("summary:a3f0"), keys
    assert citing_digest.rank_score < fact.rank_score
    assert citing_digest.score_components["cites_fact"] == "where_i_live"


def test_the_block_names_the_current_row_beside_a_capped_restatement() -> None:
    """The served paragraph of a capped chunk or digest says which
    row is current, so the reader's most-recent rule cannot pick the older
    transcript line over the edited fact."""
    from server.memory.app.ask.reader_render import render_reader_block

    fact = _hit("where_i_live", 0.488, excerpt='"xyz" (#where_i_live)')
    chunk = _hit(
        "chunk:a3f0:0",
        0.60,
        source="chunk",
        excerpt="[assistant @ 10:34] You live in Ujjain, saved in Loom as `where_i_live`.",
    )
    plain = _hit(
        "chunk:6518:0",
        0.55,
        source="chunk",
        excerpt="[user] and where do i live? [assistant] I'll check.",
    )
    assert defer_citing_rows([fact, chunk, plain]) == 1
    block = render_reader_block([plain, fact, chunk], "where does the user live")
    paras = block.split("\n\n")
    restated = [p for p in paras if "[chunk:a3f0:0]" in p][0]
    assert restated.endswith(
        "You live in Ujjain, saved in Loom as `where_i_live`. "
        "(restates [where_i_live] as it stood when this was said; the [where_i_live] row carries the current value)"
    )
    assert "restates" not in [p for p in paras if "[where_i_live]" in p and "chunk" not in p][0]
    assert "restates" not in [p for p in paras if "[chunk:6518:0]" in p][0]
    # The cited row outside the served window: no note points at a row the reader cannot see.
    assert "restates" not in render_reader_block([chunk, plain], "where does the user live")


def test_a_recall_echo_chunk_is_capped_under_the_weakest_fact_even_without_a_key() -> None:
    """A pool for 'where does the user live' where a favourite-place lookup
    chunk leads by rank, cites no fact in the pool, and carries the recall
    words."""
    from server.memory.domain.fact_citations import recall_echo

    fact = _hit("where_i_live", 0.486, excerpt='"xyz" (#where_i_live)')
    fav1 = _hit("user_favorite_place", 0.418, excerpt="User's favorite place is the mountain.")
    lookup = _hit(
        "chunk:65187490:0",
        0.532,
        source="chunk",
        excerpt="[assistant @ 08:53] Your favorite place is the mountain, saved in Loom as `fav_place`. "
        "[user @ 08:53] and where do i live? [assistant @ 08:53] I'll check Loom for your saved home location.",
    )
    citing = _hit(
        "chunk:a3f0:0",
        0.50,
        source="chunk",  # above the fact here; normally it is already capped
        excerpt="[assistant @ 10:34] You live in Ujjain, saved in Loom as `where_i_live`.",
    )
    plain = _hit(
        "chunk:plain:0",
        0.6,
        source="chunk",
        excerpt="[user] please refactor the parser [assistant] done, see src/p.cpp",
    )
    assert (
        recall_echo(lookup)
        and recall_echo(citing)
        and not recall_echo(plain)
        and not recall_echo(fact)
    )
    hits = [lookup, fact, citing, fav1, plain]
    assert defer_citing_rows(hits) == 2
    assert lookup.rank_score < fav1.rank_score, "capped under the WEAKEST fact (it names none)"
    assert (
        lookup.score_components["cites_fact"] == "*"
        and lookup.score_components["recall_echo"] == 1.0
    )
    assert (
        citing.rank_score < fact.rank_score
        and citing.score_components["cites_fact"] == "where_i_live"
    )
    assert plain.rank_score == 0.6, "a transcript chunk that recalls nothing is untouched"
    assert [h.memory_key for h in sorted(hits, key=lambda h: -h.rank_score)][:2] == [
        "chunk:plain:0",
        "where_i_live",
    ]

"""`vector_search` (server/memory/app/retrieval/): the mode dispatch,
`_build_where`'s predicate branches, the rank terms, the fact leg's
two-stage / codes-first / re-read / bare-ranking variants, the key axis.

One entry point, split into cases of at most 64 combinations each; every
statement the legs issue under a combination is rendered through the stub.
"""

from __future__ import annotations

from typing import Any

from server.memory.domain.search_types import SearchQuery
from tests.analyzer.cases._common import (
    AGENT,
    MODEL,
    NS,
    ORG,
    VEC,
    chunk_cand,
    fact_hit,
    rows_by_substring,
)
from tests.analyzer.corpus import Case, RecordingCH

MODULE = "server.memory.app.retrieval.search"
MODES = ("semantic", "hybrid", "bm25_hybrid", "rrf", "graph", "temporal", "lexical", "related")


def _q(**overrides: Any) -> SearchQuery:
    base: dict[str, Any] = {
        "org": ORG,
        "namespace": NS,
        "scope": "private",
        "agent": AGENT,
        "embedding_model": MODEL,
        "query_vec": VEC,
        "top_k": 10,
        "query_text": "deploy window friday",
    }
    base.update(overrides)
    return SearchQuery(**base)


def _stage2_row(session: str, index: int) -> dict[str, Any]:
    from tests.unit.test_chunk_two_stage import _stage2_row as real

    return dict(real(session, index))


# The legs' stub answers: the patch-part probe reads "patch-free", stage 1
# of the fact leg returns two identities so stage 2 renders, the chunk leg's
# candidate read returns two anchors so its one-trip re-read renders.
_ROWS = rows_by_substring(
    {
        "system.parts": [{"patch_parts": 0}],
        "system.columns": [{"n": 1}],
        "value_distance": [fact_hit("k1"), fact_hit("k2", session_id="s2")],
        "concat('chunk:'": [_stage2_row("s1", 3), _stage2_row("s2", 0)],
        "loom.memory_chunks": [chunk_cand("s1", 3), chunk_cand("s2", 0)],
        "loom.memory_embeddings AS e": [fact_hit("k1")],
    }
)


async def _vector_search(ch: RecordingCH, q: SearchQuery) -> None:
    from server.memory.app.retrieval import vector_search

    await vector_search(q, ch=ch)


async def _modes(ch: RecordingCH, c: dict[str, Any]) -> None:
    await _vector_search(
        ch,
        _q(
            search_mode=c["search_mode"],
            scope=c["scope"],
            key_axis=c["key_axis"],
            entity_filter=["nate"] if c["search_mode"] == "graph" else [],
            graph_expanded=["nate", "sam"] if c["search_mode"] == "graph" else [],
            query_anchor_iso="2026-01-01" if c["search_mode"] == "temporal" else "",
            bm25_idfs={"deploy": 1.2, "window": 0.7} if c["search_mode"] == "bm25_hybrid" else {},
        ),
    )


async def _filters(ch: RecordingCH, c: dict[str, Any]) -> None:
    await _vector_search(
        ch,
        _q(
            search_mode="semantic",
            memory_type="preference" if c["memory_type"] else None,
            user_id="alice" if c["user_id"] else None,
            run_id="run-1" if c["run_id"] else None,
            session_ids=["s1", "s2"] if c["session_ids"] else [],
            entity_filter=["nate"] if c["entity_filter"] else [],
            tags=["ops", "deploy"] if c["tags"] else [],
        ),
    )


async def _rank_terms(ch: RecordingCH, c: dict[str, Any]) -> None:
    await _vector_search(
        ch,
        _q(
            # Semantic ranks bare; a boosted mode carries the prior terms.
            search_mode=c.get("search_mode", "semantic"),
            tags=["ops"] if c["tags_soft"] else [],
            tags_soft=c["tags_soft"],
            category="work" if c["category"] else "",
            category_soft=c["category"] == "soft",
            demote_tags=["stale"] if c["demote_tags"] else [],
            conflicted_penalty=0.2 if c["penalties"] else 0.0,
            sensitivity_penalty=0.3 if c["penalties"] else 0.0,
            prior_cap=0.5 if c["penalties"] else 0.0,
            reinforcement_weight=0.2 if c["reinforcement"] else 0.0,
            reinforcement_half_life_use_scale=1.0 if c["reinforcement"] else 0.0,
            preference_attribute="deploy" if c["preference"] else "",
            source_class_boost_eligible=["curated"] if c["preference"] else None,
        ),
    )


async def _fact_leg(ch: RecordingCH, c: dict[str, Any]) -> None:
    await _vector_search(
        ch,
        _q(
            search_mode="semantic",
            as_of=c["as_of"],
            include_top_n_unmatched=3 if c["unmatched"] else 0,
        ),
    )


async def _served_flags(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The two widening flags on the status predicate."""
    await _vector_search(
        ch,
        _q(
            search_mode="semantic",
            scope=c["scope"],
            include_duplicates=c["include_duplicates"],
            include_superseded=c["include_superseded"],
        ),
    )


async def _fact_leg_codes_first(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The thread pin is a setting."""
    await _vector_search(ch, _q(search_mode="semantic", top_k=30))


async def _windows(ch: RecordingCH, c: dict[str, Any]) -> None:
    await _vector_search(
        ch,
        _q(
            search_mode=c["search_mode"],
            query_anchor_iso="2026-01-15" if c["window"] else "",
            anchor_window_days=14,
            triple_matched_keys=["k1", "k2"] if c["triples"] else [],
            execution_planes=["fact", "chunk", "lexical", "graph", "temporal"]
            if c["planes"]
            else [],
            personalization="none" if c["personalization"] else "full",
        ),
    )


async def _recall_credit_closer(ch: RecordingCH, c: dict[str, Any]) -> None:
    # The closer-window read behind a recall credit
    from server.memory.app import recall_credit

    recall_credit._reset_for_tests()
    await recall_credit.consumer_closed_loop(ORG, "clickgap", ch=ch)
    recall_credit._reset_for_tests()


async def _set_chunk_rows(ch: RecordingCH, c: dict[str, Any]) -> None:
    # The row a pooled memory.set chunk was cut from
    from server.memory.app.retrieval.pool import _set_chunk_rows as read
    from server.memory.domain.search_types import SearchHit

    chunk = SearchHit(
        memory_key="chunk:memory.set:k1:0",
        memory_type="episodic",
        content_excerpt="passage",
        distance=0.2,
        source="chunk",
        parent_memory_key="k1",
        identity=("a", "u", "memory.set:k1"),
    )
    await read(ch, _q(), [chunk])


def cases() -> list[Case]:
    from datetime import UTC, datetime

    return [
        Case(
            "recall_credit.closer_window",
            "server.memory.app.recall_credit",
            _recall_credit_closer,
            {},
        ),
        Case(
            "pool.set_chunk_rows",
            "server.memory.app.retrieval.pool",
            _set_chunk_rows,
            {},
            # a served row of the key, so the value read runs too
            rows=rows_by_substring(
                {
                    "max(e.version) AS version": [
                        {
                            "agent": "a",
                            "user_id": "u",
                            "session_id": "s1",
                            "memory_key": "k1",
                            "version": 1,
                            "tags": [],
                        }
                    ]
                }
            ),
        ),
        Case(
            "vector_search.modes",
            MODULE,
            _modes,
            {"search_mode": MODES, "scope": ("private", "shared"), "key_axis": (None, True, False)},
            rows=_ROWS,
        ),
        Case(
            "vector_search.filters",
            MODULE,
            _filters,
            {
                k: (False, True)
                for k in (
                    "memory_type",
                    "user_id",
                    "run_id",
                    "session_ids",
                    "entity_filter",
                    "tags",
                )
            },
            rows=_ROWS,
        ),
        Case(
            "vector_search.served_flags",
            MODULE,
            _served_flags,
            {
                "scope": ("private", "shared"),
                "include_duplicates": (False, True),
                "include_superseded": (False, True),
            },
            rows=_ROWS,
        ),
        Case(
            "vector_search.rank_terms.filters",
            MODULE,
            _rank_terms,
            {
                "tags_soft": (False, True),
                "category": ("", "hard", "soft"),
                "demote_tags": (False, True),
                "penalties": (False, True),
                "reinforcement": (False,),
                "preference": (False,),
            },
            rows=_ROWS,
        ),
        Case(
            "vector_search.rank_terms.boosts",
            MODULE,
            _rank_terms,
            {
                "tags_soft": (False, True),
                "category": ("", "soft"),
                "demote_tags": (False,),
                "penalties": (False, True),
                "reinforcement": (False, True),
                "preference": (False, True),
                "search_mode": ("semantic", "hybrid"),
            },
            rows=_ROWS,
        ),
        Case(
            "vector_search.fact_leg",
            MODULE,
            _fact_leg,
            {
                "unmatched": (False, True),
                "as_of": (None, datetime(2026, 1, 1, tzinfo=UTC)),
            },
            rows=_ROWS,
        ),
        Case(
            "vector_search.fact_leg_codes_first",
            MODULE,
            _fact_leg_codes_first,
            {
                "memory_search_fact_leg_max_threads": (4, 0),
                "memory_search_key_axis": ("off", "on"),
                # The deployment flag behind every vector-ranked read; the
                # corpus renders with it on (corpus.render_all), this case
                # carries the off form too.
                "vector_search_quantized_codes": (True, False),
            },
            rows=_ROWS,
        ),
        Case(
            "vector_search.windows",
            MODULE,
            _windows,
            {
                "search_mode": ("semantic", "temporal", "graph", "rrf"),
                "window": (False, True),
                "triples": (False,),
                "planes": (False,),
                "personalization": (False,),
            },
            rows=_ROWS,
        ),
        Case(
            "vector_search.planes",
            MODULE,
            _windows,
            {
                "search_mode": ("semantic", "temporal", "graph", "rrf"),
                "window": (False,),
                "triples": (False,),
                "planes": (False, True),
                "personalization": (False, True),
            },
            rows=_ROWS,
        ),
    ]

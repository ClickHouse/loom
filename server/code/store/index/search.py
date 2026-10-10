"""The legs of code.search: lexical and vector reads over chunks and file
cards, the shortlist-then-exact rerank, the corpus statistics the lexical
scorer reads, and the outcome signals that feed ranking back.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from server.code.domain.index.grep_plan import GREP_FINAL_SETTINGS
from server.code.domain.search_scope import SearchScope
from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _scope_sql, skip_tests_sql
from server.core.datetimes import naive_utc_now as _now


class SearchMixin(CodeRepoBase):
    """code.search legs, corpus statistics and outcome signals."""

    async def lexical_search_chunks(
        self,
        org: str,
        ns: str,
        model: str,
        tokens: list[str],
        idfs: list[float],
        *,
        path_prefix: str = "",
        pool: int = 200,
        scope: SearchScope | None = None,
    ) -> list[dict[str, Any]]:
        """Lexical leg over app-tokenized lexemes: the idf-weighted sum of
        `countSubstrings` over the deduplicated lexemes. tf counts space-bounded
        token occurrences (0 or 1: the writer stores each token once), so a long
        chunk wins by covering more query subtokens and pays nothing for its
        length. idfs are app-computed from the corpus-stat MVs and bound
        positionally (memory-plane convention).

        `scope` adds the pattern / path predicates to the plain WHERE after
        the identity keys; unset, the statement is unchanged byte for byte.

        The gate is `hasAnyTokens(lexemes, tokens)`, the predicate the `text`
        index on `lexemes` (tokenizer splitByNonAlpha — the
        writer's own token boundaries) answers per granule;
        `multiSearchAny(concat(' ', lexemes, ' '), …)` reads every chunk of
        the namespace (293 MB and 462 ms per call on the ClickHouse
        repository)."""
        if not tokens:
            return []
        where_prefix = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        scope_sql, scope_params = _scope_sql(scope)
        rows = await self._ch.query_all(
            f"""
            SELECT chunk_id, path, scope, kind, start_line, end_line,
                   content, symbol_ids, context_description,
                   arraySum(
                       arrayMap(
                           (tok, idf) -> idf * countSubstrings(
                               concat(' ', lexemes, ' '), concat(' ', tok, ' ')
                           ),
                           {{tokens:Array(String)}}, {{idfs:Array(Float32)}}
                       )
                   ) AS lex_score
            FROM loom.code_chunks FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND tombstoned_at IS NULL
              AND hasAnyTokens(lexemes, {{tokens:Array(String)}})
              {where_prefix}
              {scope_sql}
            ORDER BY lex_score DESC
            LIMIT {{pool:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                "tokens": tokens,
                "idfs": idfs,
                "pool": pool,
                **({"prefix": path_prefix} if path_prefix else {}),
                **scope_params,
            },
        )
        return rows

    async def vector_search_cards(
        self,
        org: str,
        ns: str,
        model: str,
        query_vec: list[float],
        *,
        path_prefix: str = "",
        pool: int = 15,
        skip_tests: bool = False,
    ) -> list[dict[str, Any]]:
        """The file cards nearest the query — one row per file (`scope_path`,
        `summary`, `pagerank`, `distance`) from the weekly
        cards (loom.code_summaries), the same bare-distance shape as the
        chunk leg so the codes-first scan serves it.
        Only rows that carry a summary vote: a structural placeholder row
        (embedded before any card was written) has nothing to say about the
        file. A
        namespace with no cards returns [] and the leg contributes nothing."""
        from server.core.vectors.search_settings import vector_search_settings
        from server.settings import settings

        # The cards' codes shortlist needs x10 where the default is x3 — on
        # 202 real code questions over 49k live cards, rabitq at x3 kept
        # recall@15 0.994 (188/202 identical), x10 1.000.
        # Inert until loom.code_summaries carries the codec.
        cards_settings = vector_search_settings(
            table="loom.code_summaries",
            fetch_multiplier=float(settings.code_search_cards_fetch_multiplier),
        )
        where_prefix = "AND startsWith(scope_path, {prefix:String})" if path_prefix else ""
        if skip_tests:
            # Source files only — two thirds of the ClickHouse
            # cards are tests, and for a behaviour question they are the nearest
            where_prefix += skip_tests_sql("scope_path")
        return await self._ch.query_all(
            f"""
            SELECT scope_path AS path, summary, pagerank,
                   cosineDistance(embedding, {{q:Array(Float32)}}) AS distance
            FROM loom.code_summaries FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND scope_type = 'file'
              AND embedding_dim > 0 AND tombstoned_at IS NULL AND summary != ''
              {where_prefix}
            ORDER BY distance ASC
            LIMIT {{pool:UInt32}}
            {cards_settings}
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                "q": query_vec,
                "pool": pool,
                **({"prefix": path_prefix} if path_prefix else {}),
            },
        )

    async def vector_search_chunks(
        self,
        org: str,
        ns: str,
        model: str,
        query_vec: list[float],
        *,
        path_prefix: str = "",
        pool: int = 200,
        scope: SearchScope | None = None,
    ) -> list[dict[str, Any]]:
        """Brute-force cosine leg in the exact shape CH's vector rewrite
        needs (bare distance, single ASC sort, no GROUP BY).

        Carries `vector_search_settings()` like every memory-plane vector
        read: with the `Quantized('rabitq')` codec on `code_chunks.embedding`
        the scan becomes codes-first plus a full-precision rescore of LIMIT x
        multiplier candidates — 254 -> 120 ms p50 and 2,034 -> 959 MB read at
        recall@200 0.999; without the codec, or on a replica that does not
        know the setting (the client strips it on code 115), a no-op."""
        from server.core.vectors.code_tables import code_vector_table
        from server.core.vectors.search_settings import vector_search_settings

        where_prefix = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        # The pattern / path predicates sit in the plain WHERE (PREWHERE is
        # banned here); the scan then reads
        # `embedding` only for granules holding a match.
        scope_sql, scope_params = _scope_sql(scope)
        # A second model's rows live in the table its label
        # maps to (the coded column is one width per table); the served
        # label resolves to loom.code_chunks.
        table = code_vector_table(model)
        rows = await self._ch.query_all(
            f"""
            SELECT chunk_id, path, scope, kind, start_line, end_line,
                   content, symbol_ids, context_description,
                   cosineDistance(embedding, {{q:Array(Float32)}}) AS distance
            FROM {table} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND embedding_dim > 0
              AND tombstoned_at IS NULL
              {where_prefix}
              {scope_sql}
            ORDER BY distance ASC
            LIMIT {{pool:UInt32}}
            {vector_search_settings(table=table)}
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                "q": query_vec,
                "pool": pool,
                **({"prefix": path_prefix} if path_prefix else {}),
                **scope_params,
            },
        )
        return rows

    async def vector_shortlist_ids(
        self,
        org: str,
        ns: str,
        model: str,
        query_vec: list[float],
        *,
        n: int = 1000,
        path_prefix: str = "",
        scope: SearchScope | None = None,
    ) -> list[dict[str, Any]]:
        """Stage 1 of shortlist-then-exact: the second model's top-`n`
        `(chunk_id, path, distance)` under
        its own label and table — key columns and the distance only, no
        bodies, so the read is the small vector column plus the key."""
        from server.core.vectors.code_tables import code_vector_table
        from server.core.vectors.search_settings import vector_search_settings

        where_prefix = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        scope_sql, scope_params = _scope_sql(scope)
        table = code_vector_table(model)
        return await self._ch.query_all(
            f"""
            SELECT chunk_id, path,
                   cosineDistance(embedding, {{q:Array(Float32)}}) AS distance
            FROM {table} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND embedding_dim > 0
              AND tombstoned_at IS NULL
              {where_prefix}
              {scope_sql}
            ORDER BY distance ASC
            LIMIT {{n:UInt32}}
            {vector_search_settings(table=table)}
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                "q": query_vec,
                "n": n,
                **({"prefix": path_prefix} if path_prefix else {}),
                **scope_params,
            },
        )

    async def exact_rerank_by_ids(
        self,
        org: str,
        ns: str,
        model: str,
        query_vec: list[float],
        keys: list[tuple[str, str]],
        *,
        pool: int = 200,
    ) -> list[dict[str, Any]]:
        """Stage 2 of shortlist-then-exact: exact cosine on the served
        model's vectors for the shortlisted `(path, chunk_id)` keys only. Two
        plain keyed reads beside each other, no FINAL (FINAL on the wide
        table reads 396 MiB for 200 keys where the plain read reads 92 MiB in
        a third of the time): the
        vector read ranks each keyed row's newest embedded version, and a
        key-only read names the keys whose newest version is a tombstone.
        Versions of a key can carry different vectors — a re-describe
        re-embeds `context_text` under an unchanged content hash — so the
        newest version is picked before ranking (`LIMIT 1 BY` the key ordered
        by version); ranking every version would let a stale vector outrank
        the served one until a merge collapsed them.
        The client keeps one row per key, drops the tombstoned ones and the
        rows the two IN lists admit beyond the asked keys, and returns the
        vector leg's unit columns in distance order, `pool` rows at most."""
        if not keys:
            return []
        uniq = sorted({(str(p), str(c)) for p, c in keys})
        params = {
            "org": org,
            "ns": ns,
            "model": model,
            "q": query_vec,
            "paths": sorted({p for p, _ in uniq}),
            "ids": sorted({c for _, c in uniq}),
            "lim": max(int(pool), len(uniq)) * 8,
        }
        ident = """org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String}
              AND path IN {paths:Array(String)} AND chunk_id IN {ids:Array(UUID)}"""
        rows, tombs = await asyncio.gather(
            self._ch.query_all(
                f"""
                SELECT chunk_id, path, scope, kind, start_line, end_line,
                       content, symbol_ids, context_description, distance
                FROM (
                    SELECT chunk_id, path, scope, kind, start_line, end_line,
                           content, symbol_ids, context_description,
                           cosineDistance(embedding, {{q:Array(Float32)}}) AS distance
                    FROM loom.code_chunks
                    WHERE {ident}
                      AND embedding_dim > 0
                    ORDER BY version DESC
                    LIMIT 1 BY path, chunk_id
                )
                ORDER BY distance ASC
                LIMIT {{lim:UInt32}}
                """,
                params,
            ),
            self._ch.query_all(
                f"""
                SELECT path, chunk_id, argMax(tombstoned_at IS NULL, version) AS is_live
                FROM loom.code_chunks
                WHERE {ident}
                GROUP BY path, chunk_id
                HAVING is_live = 1
                """,
                params,
            ),
        )
        # the keys whose newest version is live (the null-safe idiom: a
        # re-inserted key reads live, not its old tombstone)
        live = {(str(r["path"]), str(r["chunk_id"])) for r in tombs} & set(uniq)
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for r in rows:
            key = (str(r["path"]), str(r["chunk_id"]))
            if key in seen or key not in live:
                continue
            seen.add(key)
            out.append(r)
            if len(out) >= pool:
                break
        return out

    async def scope_file_count(
        self,
        org: str,
        ns: str,
        model: str,
        scope: SearchScope,
        *,
        path_prefix: str = "",
    ) -> int:
        """Distinct files the scope's predicates admit under the identity
        keys — the pre-count behind `code_search_pattern_max_files`. Reads
        `content` for every granule under the
        prefix and nothing else; an over-cap pattern is refused on this
        number before either leg scans."""
        where_prefix = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        scope_sql, scope_params = _scope_sql(scope)
        row = await self._ch.query_one(
            f"""
            SELECT uniqExact(path) AS n
            FROM loom.code_chunks FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND tombstoned_at IS NULL
              {where_prefix}
              {scope_sql}
            {GREP_FINAL_SETTINGS}
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                **({"prefix": path_prefix} if path_prefix else {}),
                **scope_params,
            },
        )
        return int((row or {}).get("n") or 0)

    async def code_corpus_stats(
        self, org: str, ns: str, tokens: list[str]
    ) -> tuple[dict[str, int], int, float]:
        """(df per token, N chunks, mean token_count). The mean is BM25's avgdl.

        One read of `code_term_df`, the hourly snapshot of the numbers: the
        tokens' rows and the namespace's `term = ''` row (N and avgdl). A
        namespace the snapshot does not hold yet (no `''` row) is read from
        the corpus-stat MVs themselves, whose per-term states cost a granule
        of object storage per token on a cold cache."""
        if not tokens:
            return {}, 0, 0.0
        snap = await self._ch.query_all(
            """
            SELECT term, df, avgdl
            FROM loom.code_term_df
            WHERE org = {org:String} AND namespace = {ns:String}
              AND term IN {terms:Array(String)}
            """,
            {"org": org, "ns": ns, "terms": ["", *tokens]},
        )
        totals = next((r for r in snap if r["term"] == ""), None)
        if totals is not None:
            return (
                {str(r["term"]): int(r["df"]) for r in snap if r["term"] != ""},
                int(totals["df"]),
                float(totals["avgdl"] or 0.0),
            )
        # Two independent reads of two tables, gathered.
        df_rows, n_row = await asyncio.gather(
            self._ch.query_all(
                """
                SELECT term, toUInt64(uniqExactMerge(df_state)) AS df
                FROM loom.code_term_freq
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND term IN {tokens:Array(String)}
                GROUP BY term
                """,
                {"org": org, "ns": ns, "tokens": tokens},
            ),
            self._ch.query_one(
                """
                SELECT toUInt64(count()) AS n,
                       toFloat64(avgOrDefault(token_count)) AS avgdl
                FROM loom.code_doc_stats FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                """,
                {"org": org, "ns": ns},
            ),
        )
        return (
            {str(r["term"]): int(r["df"]) for r in df_rows},
            int(n_row["n"]) if n_row else 0,
            float(n_row.get("avgdl") or 0.0) if n_row else 0.0,
        )

    async def add_unit_signals(
        self, org: str, ns: str, deltas: list[tuple[str, float, float]]
    ) -> None:
        """(unit_id, positive, negative) increments; SummingMergeTree folds."""
        if not deltas:
            return
        now = _now()
        await self._ch.insert(
            "loom.code_unit_signal",
            [
                {
                    "org": org,
                    "namespace": ns,
                    "unit_id": uid,
                    "positive": pos,
                    "negative": neg,
                    "last_positive_at": now if pos > 0 else datetime(1970, 1, 1),
                }
                for uid, pos, neg in deltas
            ],
        )

    async def unit_signals(
        self, org: str, ns: str, unit_ids: list[str]
    ) -> dict[str, tuple[float, float, Any]]:
        """unit_id -> (positive, negative, last_positive_at) for candidates."""
        if not unit_ids:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT unit_id, sum(positive) AS pos, sum(negative) AS neg,
                   max(last_positive_at) AS last_pos
            FROM loom.code_unit_signal
            WHERE org = {org:String} AND namespace = {ns:String}
              AND unit_id IN {ids:Array(String)}
            GROUP BY unit_id
            """,
            {"org": org, "ns": ns, "ids": unit_ids[:200]},
        )
        return {str(r["unit_id"]): (float(r["pos"]), float(r["neg"]), r["last_pos"]) for r in rows}

    async def trace_by_run_id(self, org: str, ns: str, trace_run_id: str) -> dict[str, Any] | None:
        """The trace row an outcome refers to (bounded to recent partitions —
        outcomes arrive within a session, not weeks later)."""
        return await self._ch.query_one(
            """
            SELECT op, returned_ids, returned_paths, abstain, route, args_json
            FROM loom.code_search_trace
            WHERE org = {org:String} AND namespace = {ns:String}
              AND trace_run_id = {rid:String}
              AND ts > now() - INTERVAL 7 DAY
            ORDER BY ts DESC LIMIT 1
            """,
            {"org": org, "ns": ns, "rid": trace_run_id},
        )

    async def trace_outcomes(self, org: str, ns: str, *, days: int) -> list[dict[str, Any]]:
        """The last `days` of non-abstaining code.search traces joined with the outcomes
        clients reported for them, oldest first."""
        rows: list[dict[str, Any]] = await self._ch.query_all(
            """
            SELECT t.query AS query, t.route AS route, t.ts AS ts,
                   t.returned_ids AS ids, t.returned_paths AS paths,
                   t.returned_via AS via,
                   o.action AS action, o.targets AS targets
            FROM loom.code_search_trace AS t
            INNER JOIN loom.code_search_outcome AS o
                ON t.trace_run_id = o.trace_run_id
               AND t.org = o.org AND t.namespace = o.namespace
            WHERE t.org = {org:String} AND t.namespace = {ns:String}
              AND t.ts > now() - INTERVAL {days:UInt32} DAY
              AND t.op = 'code.search' AND t.abstain = 0
              AND length(t.returned_ids) > 0
            ORDER BY t.ts
            """,
            {"org": org, "ns": ns, "days": days},
        )
        return rows

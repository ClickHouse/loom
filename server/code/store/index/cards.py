"""Reads over loom.code_summaries, the file and directory cards."""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase


class CardsMixin(CodeRepoBase):
    """File and directory cards."""

    async def top_summaries(
        self, org: str, ns: str, model: str, *, module_prefix: str = "", limit: int = 40
    ) -> list[dict[str, Any]]:
        where_prefix = "AND startsWith(scope_path, {prefix:String})" if module_prefix else ""
        return await self._ch.query_all(
            f"""
            SELECT scope_type, scope_path, summary, pagerank, in_degree,
                   out_degree, key_symbols
            FROM loom.code_summaries FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND tombstoned_at IS NULL
              {where_prefix}
            ORDER BY pagerank DESC
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                "limit": limit,
                **({"prefix": module_prefix} if module_prefix else {}),
            },
        )

    async def live_summary_rows(
        self,
        org: str,
        ns: str,
        model: str,
        *,
        with_embedding: bool = True,
    ) -> list[dict[str, Any]]:
        """Every live card row of the namespace under `model`. A caller that
        mirrors the rows forward (the graph-stats pass) needs the vector; the
        card pass re-embeds every card it writes and reads without it —
        6 KB a row, 300 MB for a 50k-file repository, enough to push a pass
        past the read deadline."""
        cols = "*" if with_embedding else "* EXCEPT (embedding)"
        return await self._ch.query_all(
            f"""
            SELECT {cols} FROM loom.code_summaries FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "model": model},
        )

    async def file_card_ranks(
        self, org: str, ns: str, model: str
    ) -> dict[str, tuple[float, int, int]]:
        """`scope_path -> (pagerank, in_degree, out_degree)` of every live file card
        under `model`: what the graph-stats pass compares its fresh ranks with,
        without the cards' text or vector (the full read of a large repository
        can run past the read deadline)."""
        rows = await self._ch.query_all(
            """
            SELECT scope_path, pagerank, in_degree, out_degree FROM loom.code_summaries FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND scope_type = 'file'
              AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "model": model},
        )
        return {
            str(r["scope_path"]): (
                float(r["pagerank"] or 0.0),
                int(r["in_degree"] or 0),
                int(r["out_degree"] or 0),
            )
            for r in rows
        }

    async def live_file_cards(
        self, org: str, ns: str, model: str, paths: list[str]
    ) -> list[dict[str, Any]]:
        """The full live file-card rows of `paths` (vector included): the rows a
        pass that rewrites them mirrors forward."""
        if not paths:
            return []
        return await self._ch.query_all(
            """
            SELECT * FROM loom.code_summaries FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND scope_type = 'file'
              AND scope_path IN {paths:Array(String)} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "model": model, "paths": sorted(set(paths))},
        )

    async def cards_for_paths(
        self,
        org: str,
        ns: str,
        model: str,
        paths: list[str],
    ) -> dict[str, str]:
        """The file cards of `paths` (text only) under the
        served label — what code.ask puts above each excerpt so the
        synthesizer can tell the mechanism's file from its callers."""
        if not paths:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT scope_path, summary FROM loom.code_summaries FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND scope_type = 'file'
              AND scope_path IN {paths:Array(String)} AND summary != ''
              AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "model": model, "paths": sorted(set(paths))},
        )
        return {str(r["scope_path"]): str(r["summary"]) for r in rows}

    async def live_summary_rows_other_models(
        self,
        org: str,
        ns: str,
        model: str,
        *,
        with_embedding: bool = True,
    ) -> list[dict[str, Any]]:
        """Live card rows written under any model but `model` — the dedupe
        input. `with_embedding` as in `live_summary_rows`:
        a carried card is re-embedded and a tombstone never scored."""
        cols = "*" if with_embedding else "* EXCEPT (embedding)"
        return await self._ch.query_all(
            f"""
            SELECT {cols} FROM loom.code_summaries FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model != {{model:String}} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "model": model},
        )

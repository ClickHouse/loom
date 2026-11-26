"""The pending-embed stubs: rows written without a vector that the embed
worker and the recovery scan pick up.
"""

from __future__ import annotations

from typing import Any

from server.code.domain.extract.lang_reference import UNEMBEDDED_BY_DESIGN
from server.code.store.index.base import CodeRepoBase


class EmbedQueueMixin(CodeRepoBase):
    """Stub chunk and card rows awaiting a vector."""

    async def pending_embed_chunks(
        self,
        org: str,
        ns: str,
        model: str,
        max_attempts: int,
        limit: int,
        *,
        include_poisoned: bool = False,
    ) -> list[dict[str, Any]]:
        """Stub rows awaiting a vector. `include_poisoned` also returns rows
        that exhausted their attempts (embed_failed_at set) — the backfill's
        `--include-poisoned` retry."""
        poison_filter = (
            ""
            if include_poisoned
            else "AND embed_attempts < {max_attempts:UInt16} AND embed_failed_at IS NULL"
        )
        # A reference fixture's row is unembedded BY DESIGN: neither a stub to
        # fill nor a poisoned row to retry, under either filter.
        poison_filter += " AND NOT startsWith(embed_last_error, {unembedded:String})"
        params = {
            "org": org,
            "ns": ns,
            "model": model,
            "max_attempts": max_attempts,
            "limit": limit,
            "unembedded": UNEMBEDDED_BY_DESIGN,
        }
        # Two phases, as the memory store's find_embedding_stubs: a FINAL read
        # filters after the merge and so reads every column of the (org, namespace,
        # model) range — the vector included — to find stubs that mostly are
        # not there. Phase 1 has no FINAL and touches only the flag columns;
        # phase 2 is the FINAL read pruned to the candidates' sort keys. Phase
        # 1 judges each key by its NEWEST version (argMax over the flag
        # columns), not by any version: a heal leaves the superseded stub
        # version of every row it rewrote in the table, and a flat
        # `DISTINCT … PREWHERE embedding_dim = 0` window would fill with those
        # stale keys, leaving phase 2 nothing to keep.
        newest_poison = (
            ""
            if include_poisoned
            else "AND argMax(embed_attempts, version) < {max_attempts:UInt16}"
            " AND argMax(embed_failed_at IS NULL, version) = 1"
        )
        candidates = await self._ch.query_all(
            f"""
            SELECT path, chunk_id FROM loom.code_chunks
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}}
            GROUP BY path, chunk_id
            HAVING argMax(embedding_dim, version) = 0
              AND argMax(tombstoned_at IS NULL, version) = 1
              AND NOT startsWith(argMax(embed_last_error, version), {{unembedded:String}})
              {newest_poison}
            LIMIT {{cand_lim:UInt32}}
            """,
            {**params, "cand_lim": int(limit) * 4},
        )
        if not candidates:
            return []
        keys = [(str(r["path"]), str(r["chunk_id"])) for r in candidates]
        # `* EXCEPT (embedding)`: a stub's vector is empty by definition and the
        # caller writes the new one; under FINAL the column would still be read
        # for every row of the candidates' granules.
        return await self._ch.query_all(
            f"""
            SELECT * EXCEPT (embedding) FROM loom.code_chunks FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}}
              AND (path, chunk_id) IN {{keys:Array(Tuple(String, UUID))}}
              AND embedding_dim = 0 AND tombstoned_at IS NULL
              {poison_filter}
            ORDER BY created_at
            LIMIT {{limit:UInt32}}
            """,
            {**params, "keys": keys},
        )

    # The recovery worker's pending scan, for every configured repo in one
    # statement and keyed above a version watermark. The key
    # columns of the two tables it serves (the sort key after
    # `embedding_model`).
    PENDING_EMBED_KEY_COLS: dict[str, tuple[str, ...]] = {
        "code_chunks": ("path", "chunk_id"),
        "code_summaries": ("scope_type", "scope_path"),
    }

    _PENDING_EMBED_KEY_TYPES: dict[str, str] = {
        "code_chunks": "Tuple(String, String, String, String, UUID)",
        "code_summaries": "Tuple(String, String, String, String, String)",
    }

    async def pending_embed_keys(
        self,
        table: str,
        repos: list[tuple[str, str]],
        model: str,
        max_attempts: int,
        limit: int,
        *,
        min_version: int = 0,
    ) -> list[dict[str, Any]]:
        """Phase one of the recovery scan: the sort keys and versions of stub
        rows (`embedding_dim = 0`) across every configured
        `(org, namespace)` in ONE statement, oldest version first, above
        `min_version`. No FINAL and narrow columns: the statement reads the
        flag columns of the repos' `(org, namespace, model)` ranges and
        nothing else. `version` rides along so the caller can keep the
        newest returned version as its watermark — with the ORDER BY, every
        stub version at or below it has been returned once, so the next scan
        skips the stub versions of rows already replaced instead of paying a
        FINAL read per repo per pass that returns nothing."""
        if not repos:
            return []
        key_cols = ", ".join(self.PENDING_EMBED_KEY_COLS[table])
        # The CHEAP candidate scan — flag columns only, no FINAL, no
        # aggregate — and the caller passes its page through `live_stub_keys`
        # (a keyed argMax over the page's keys) before the FINAL row read. A
        # per-key aggregate over every version of every configured namespace
        # would read ~380 MB a scan where this one reads ~27 MB.
        return await self._ch.query_all(
            f"""
            SELECT org, namespace, {key_cols}, version FROM loom.{table}
            WHERE (org, namespace) IN {{repos:Array(Tuple(String, String))}}
              AND embedding_model = {{model:String}}
              AND embedding_dim = 0 AND tombstoned_at IS NULL
              AND embed_attempts < {{max_attempts:UInt16}} AND embed_failed_at IS NULL
              AND NOT startsWith(embed_last_error, {{unembedded:String}})
              AND version > {{min_version:UInt64}}
            ORDER BY version, org, namespace, {key_cols}
            LIMIT {{limit:UInt32}}
            """,
            {
                "repos": [(str(o), str(n)) for o, n in repos],
                "model": model,
                "max_attempts": int(max_attempts),
                "unembedded": UNEMBEDDED_BY_DESIGN,
                "min_version": int(min_version),
                "limit": int(limit),
            },
        )

    async def live_stub_keys(
        self,
        table: str,
        model: str,
        keys: list[dict[str, Any]],
        max_attempts: int,
    ) -> list[dict[str, Any]]:
        """Of a candidate page, the keys whose NEWEST version is still a
        live, embeddable stub — one keyed aggregate over
        the page's sort keys (`IN` on the full key prunes to their granules;
        flag columns only, no FINAL), so a page of superseded stub versions
        costs a small read instead of the ~0.55 s keyed FINAL row read it
        would otherwise trigger (`code_summaries`). Returns the same key rows
        with `version` set to the key's newest."""
        if not keys:
            return []
        key_cols = ", ".join(self.PENDING_EMBED_KEY_COLS[table])
        key_type = self._PENDING_EMBED_KEY_TYPES[table]
        cols = self.PENDING_EMBED_KEY_COLS[table]
        tuples = [
            (str(k["org"]), str(k["namespace"]), model, *(str(k[c]) for c in cols)) for k in keys
        ]
        rows = await self._ch.query_all(
            f"""
            SELECT org, namespace, {key_cols}, max(version) AS newest FROM loom.{table}
            WHERE (org, namespace, embedding_model, {key_cols}) IN {{keys:Array({key_type})}}
            GROUP BY org, namespace, {key_cols}
            HAVING argMax(embedding_dim, version) = 0
              AND argMax(tombstoned_at IS NULL, version) = 1
              AND argMax(embed_attempts, version) < {{max_attempts:UInt16}}
              AND argMax(embed_failed_at IS NULL, version) = 1
              AND NOT startsWith(argMax(embed_last_error, version), {{unembedded:String}})
            """,
            {
                "keys": tuples,
                "max_attempts": int(max_attempts),
                "unembedded": UNEMBEDDED_BY_DESIGN,
            },
        )
        # `newest`, not `version`: aliasing an aggregate to its source column
        # is ILLEGAL_AGGREGATION under the analyzer; callers keep `version`.
        out: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            row["version"] = row.pop("newest")
            out.append(row)
        return out

    async def pending_embed_rows_by_keys(
        self,
        table: str,
        model: str,
        keys: list[dict[str, Any]],
        max_attempts: int,
    ) -> list[dict[str, Any]]:
        """Phase two: the full rows — every column but the vector, which a
        stub has none of and the caller writes — of the keys
        phase one returned, under FINAL by the full sort key so the primary
        index prunes to their granules, the stub state re-checked after the
        merge so a row replaced since the keys were read is not embedded
        twice. Keys may span repos: one statement per round, not per repo."""
        if not keys:
            return []
        key_cols = self.PENDING_EMBED_KEY_COLS[table]
        tuples = [
            (str(k["org"]), str(k["namespace"]), model, *(str(k[c]) for c in key_cols))
            for k in keys
        ]
        return await self._ch.query_all(
            f"""
            SELECT * EXCEPT (embedding) FROM loom.{table} FINAL
            WHERE (org, namespace, embedding_model, {", ".join(key_cols)})
                  IN {{keys:Array({self._PENDING_EMBED_KEY_TYPES[table]})}}
              AND embedding_dim = 0 AND tombstoned_at IS NULL
              AND embed_attempts < {{max_attempts:UInt16}} AND embed_failed_at IS NULL
              AND NOT startsWith(embed_last_error, {{unembedded:String}})
            ORDER BY created_at, org, namespace, {", ".join(key_cols)}
            """,
            {
                "keys": tuples,
                "max_attempts": int(max_attempts),
                "unembedded": UNEMBEDDED_BY_DESIGN,
            },
        )

"""Reads over loom.code_files and loom.code_chunks: live file and chunk rows
for ingest diffs, path lookups, chunk spans and texts, and the chunk pages
the read path assembles.
"""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _batched, _gather_batched, _hex


class FilesMixin(CodeRepoBase):
    """Files and chunks: live rows, path sets, spans, texts."""

    async def live_chunks_for_path(
        self,
        org: str,
        ns: str,
        path: str,
        embedding_model: str,
        *,
        with_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        """Live chunk rows for one path WITHOUT the vector.

        `embedding` is most of the table's bytes; under FINAL every selected
        column of every row in the touched granules is read, so `SELECT *`
        costs about three times what this shape costs per file. Readers that show
        content (code.ask expansion, the
        file-summary worker) never need the vector. A write path that
        mirrors a row into a newer version needs every column — it gets
        them from `chunks_full_by_ids`, for the few rows it rewrites — or
        passes `with_embedding=True` when it mirrors the whole file."""
        cols = "*" if with_embedding else "* EXCEPT (embedding)"
        return await self._ch.query_all(
            f"""
            SELECT {cols} FROM loom.code_chunks FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND path = {{path:String}}
              AND embedding_model = {{model:String}}
              AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "path": path, "model": embedding_model},
        )

    async def live_chunk_index_for_path(
        self, org: str, ns: str, path: str, embedding_model: str
    ) -> list[dict[str, Any]]:
        """(chunk_id, start_line, end_line, version) of one path's live
        chunks — what the content-addressed reconcile in ingest compares
        on. The full rows for the chunks it then rewrites (moved spans,
        tombstones) come from `chunks_full_by_ids`."""
        return await self._ch.query_all(
            """
            SELECT chunk_id, start_line, end_line, kind, version
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String}
              AND embedding_model = {model:String}
              AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "path": path, "model": embedding_model},
        )

    async def live_chunk_index_for_paths(
        self, org: str, ns: str, paths: list[str], embedding_model: str
    ) -> dict[str, list[dict[str, Any]]]:
        """`live_chunk_index_for_path` for a round of paths in one statement
        per 100, routed back per path; every requested path present (an
        empty list for one with no live chunk)."""
        out: dict[str, list[dict[str, Any]]] = {str(p): [] for p in paths}
        for batch in _batched(sorted(out), 100):
            rows = await self._ch.query_all(
                """
                SELECT chunk_id, start_line, end_line, kind, version, path
                FROM loom.code_chunks FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)}
                  AND embedding_model = {model:String}
                  AND tombstoned_at IS NULL
                """,
                {"org": org, "ns": ns, "paths": batch, "model": embedding_model},
            )
            for r in rows:
                out.setdefault(str(r.pop("path")), []).append(r)
        return out

    async def live_paths_named(self, org: str, ns: str, name: str, limit: int = 4000) -> list[str]:
        """Every live file path whose basename is `name` (the `go.mod` files
        of a multi-module repository, read once per namespace
        for the module index), sorted, capped."""
        rows = await self._ch.query_all(
            """
            SELECT path FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND (path = {name:String} OR endsWith(path, {suffix:String}))
              AND tombstoned_at IS NULL
            ORDER BY path LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "name": name, "suffix": "/" + name, "limit": int(limit)},
        )
        return [str(r["path"]) for r in rows]

    async def live_paths_under(self, org: str, ns: str, prefix: str, limit: int = 64) -> list[str]:
        """The live file paths under one directory prefix (a Go package whose
        files carry no conventional name), sorted, capped."""
        rows = await self._ch.query_all(
            """
            SELECT path FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND startsWith(path, {prefix:String}) AND tombstoned_at IS NULL
            ORDER BY path LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "prefix": prefix, "limit": int(limit)},
        )
        return [str(r["path"]) for r in rows]

    async def live_paths_for_lang(self, org: str, ns: str, lang: str) -> list[str]:
        """The live file paths of one language (the SQL files a folder vote
        may decide)."""
        rows = await self._ch.query_all(
            """
            SELECT path FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND lang = {lang:String} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "lang": lang},
        )
        return [str(r["path"]) for r in rows]

    async def chunks_full_by_ids(
        self, org: str, ns: str, path: str, embedding_model: str, chunk_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Every column (vector included) of the named live chunks, by the
        full sort key so the primary index prunes to their granules. For the
        rows a write path mirrors into a newer version."""
        out: list[dict[str, Any]] = []
        for batch in _batched(sorted(set(chunk_ids)), 500):
            out.extend(
                await self._ch.query_all(
                    """
                SELECT * FROM loom.code_chunks FINAL
                WHERE (org, namespace, embedding_model, path, chunk_id)
                      IN {keys:Array(Tuple(String, String, String, String, String))}
                  AND tombstoned_at IS NULL
                """,
                    {"keys": [(org, ns, embedding_model, path, cid) for cid in batch]},
                )
            )
        return out

    async def chunks_by_keys(
        self, org: str, ns: str, embedding_model: str, keys: list[tuple[str, str]]
    ) -> list[dict[str, Any]]:
        """The unit columns of the named live (path, chunk_id) pairs, by the
        full sort key so the primary index prunes to their granules. The
        body read of a continuation page: the pool row names the ids and
        paths, this returns what the page serves. The column list is the
        legs' own — no vector and no `lexemes` (`* EXCEPT (embedding)` reads
        56 ms median for five keys, this list 31 ms). Plain WHERE — no
        PREWHERE in the code plane."""
        out: list[dict[str, Any]] = []
        uniq = sorted({(str(p), str(c)) for p, c in keys})

        async def read(batch: list[Any]) -> list[dict[str, Any]]:
            return await self._ch.query_all(
                """
                SELECT chunk_id, path, scope, kind, start_line, end_line,
                       content, symbol_ids, context_description
                FROM loom.code_chunks FINAL
                WHERE (org, namespace, embedding_model, path, chunk_id)
                      IN {keys:Array(Tuple(String, String, String, String, String))}
                  AND tombstoned_at IS NULL
                """,
                {"keys": [(org, ns, embedding_model, p, c) for p, c in batch]},
            )

        for rows in await _gather_batched(uniq, 200, read):
            out.extend(rows)
        return out

    async def chunk_spans_for_path(
        self, org: str, ns: str, path: str
    ) -> list[tuple[str, int, int]]:
        """(kind, start_line, end_line) for one path's live chunks, any
        embedding model. Powers cross-tier validation in the scip apply:
        the treesitter chunker knows function-body extents that scip-clang
        0.4.0 does not emit (no enclosing_range)."""
        rows = await self._ch.query_all(
            """
            SELECT DISTINCT kind, start_line, end_line
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "path": path},
        )
        return [(str(r["kind"]), int(r["start_line"]), int(r["end_line"])) for r in rows]

    async def chunk_bodies_with_symbol(
        self, org: str, ns: str, path: str, symbol_id: str, *, limit: int = 3
    ) -> list[str]:
        """The contents of the live chunks of `path` that carry `symbol_id`, in line
        order, at most `limit`."""
        rows = await self._ch.query_all(
            """
            SELECT content FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND has(symbol_ids, {sid:String})
              AND tombstoned_at IS NULL
            ORDER BY start_line LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "path": path, "sid": symbol_id, "limit": limit},
        )
        return [str(r["content"]) for r in rows]

    async def innermost_chunk_at(self, org: str, ns: str, path: str, line: int) -> str | None:
        """The content of the tightest live chunk of `path` covering `line`; None when
        no chunk covers it."""
        rows = await self._ch.query_all(
            """
            SELECT content FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND tombstoned_at IS NULL
              AND start_line <= {line:UInt32} AND end_line >= {line:UInt32}
            ORDER BY end_line - start_line ASC LIMIT 1
            """,
            {"org": org, "ns": ns, "path": path, "line": line},
        )
        return str(rows[0]["content"]) if rows else None

    async def chunk_line_span(
        self, org: str, ns: str, path: str, start: int, end: int
    ) -> tuple[int, int] | None:
        """(lowest start_line, highest end_line) of the live chunks of `path` that
        overlap `start..end`; None when none does."""
        row = await self._ch.query_one(
            """
            SELECT min(start_line) AS lo, max(end_line) AS hi
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND path = {path:String}
              AND tombstoned_at IS NULL
              AND start_line <= {end:UInt32} AND end_line >= {start:UInt32}
            """,
            {"org": org, "ns": ns, "path": path, "start": start, "end": end},
        )
        if not row or row.get("lo") is None:
            return None
        return int(row["lo"]), int(row["hi"])

    async def live_files_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        return await self._ch.query_all(
            """
            SELECT * FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "path": path},
        )

    async def file_identities(self, org: str, ns: str) -> list[tuple[str, str, str]]:
        """(path, indexed_commit, blob_sha) for every live indexed path — the
        reconcile's input. `indexed_commit` is the commit the file was last
        fully parsed at: the freshness marker, since unmoved chunks keep an
        older commit forever. `blob_sha` is '' for an older
        row; the caller judges those by the diff walk."""
        rows = await self._ch.query_all(
            """
            SELECT path, indexed_commit AS c, blob_sha AS b FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns},
        )
        out: list[tuple[str, str, str]] = []
        for r in rows:
            out.append((str(r["path"]), _hex(r.get("c")), _hex(r.get("b"))))
        return out

    async def file_rows_for_paths(
        self, org: str, ns: str, paths: list[str]
    ) -> dict[str, dict[str, Any]]:
        """path -> {lang, loc, indexed_commit} for the paths the index holds
        a live file row for (a cited path exists, and a
        cited line beyond `loc` is dead by an exact count). Point reads on
        the (org, namespace, path) key, 200 paths per statement."""
        out: dict[str, dict[str, Any]] = {}
        wanted = list(dict.fromkeys(p for p in paths if p))
        for i in range(0, len(wanted), 200):
            rows = await self._ch.query_all(
                """
                SELECT path, lang, loc, indexed_commit FROM loom.code_files FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
                """,
                {"org": org, "ns": ns, "paths": wanted[i : i + 200]},
            )
            for r in rows:
                out[str(r["path"])] = {
                    "lang": str(r.get("lang") or ""),
                    "loc": int(r.get("loc") or 0),
                    "indexed_commit": _hex(r.get("indexed_commit")),
                }
        return out

    async def paths_by_suffix(
        self, org: str, ns: str, suffixes: list[str], *, limit: int = 1000
    ) -> dict[str, list[str]]:
        """suffix -> live indexed paths ending in `/<suffix>` (a draft cites
        `Parsers/ASTSetQuery.cpp` or `configs/keeper.xml`
        relative to a directory; the file table says which files those
        are). One read over the namespace's file rows (one row per file,
        ~30k on the largest index), no bodies; 100 suffixes per call."""
        wanted = list(dict.fromkeys(s.strip("/") for s in suffixes if s.strip("/")))[:100]
        if not wanted:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT path FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND arrayExists(s -> endsWith(path, s), {suffixes:Array(String)})
            ORDER BY path
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "suffixes": ["/" + s for s in wanted], "limit": limit},
        )
        out: dict[str, list[str]] = {}
        for r in rows:
            path = str(r["path"])
            for s in wanted:
                if path.endswith("/" + s):
                    out.setdefault(s, []).append(path)
        return out

    async def chunk_spans_for_paths(
        self, org: str, ns: str, paths: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """path -> [{kind, scope, start_line, end_line}] of the live chunks
        of each path, any embedding model, no bodies (the
        units a cited line may sit in when no symbol span holds it). Keyed
        by (org, namespace, embedding_model, path) — the IN over paths
        selects by the sort key's path component; 50 paths per statement."""
        out: dict[str, list[dict[str, Any]]] = {}
        wanted = list(dict.fromkeys(p for p in paths if p))
        for i in range(0, len(wanted), 50):
            rows = await self._ch.query_all(
                """
                SELECT DISTINCT path, kind, scope, start_line, end_line
                FROM loom.code_chunks FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
                ORDER BY path, start_line
                LIMIT 20000
                """,
                {"org": org, "ns": ns, "paths": wanted[i : i + 50]},
            )
            for r in rows:
                out.setdefault(str(r["path"]), []).append(
                    {
                        "kind": str(r.get("kind") or ""),
                        "scope": str(r.get("scope") or ""),
                        "start_line": int(r.get("start_line") or 0),
                        "end_line": int(r.get("end_line") or 0),
                    }
                )
        return out

    async def class_skeleton_chunk(
        self, org: str, ns: str, path: str, scope: str
    ) -> dict[str, Any] | None:
        """The first `class_skeleton` piece of a class/struct in `path`: the
        class text with method bodies elided (chunker.py), which is what a
        reader of a large container wants before any body. One piece, never
        the joined pieces — the outline carries the rest as line spans."""
        rows = await self._ch.query_all(
            """
            SELECT content, start_line, end_line, chunk_id
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND kind = 'class_skeleton'
              AND scope = {scope:String} AND tombstoned_at IS NULL
            ORDER BY start_line ASC LIMIT 1
            """,
            {"org": org, "ns": ns, "path": path, "scope": scope},
        )
        return rows[0] if rows else None

    async def live_text_for_path(self, org: str, ns: str, path: str) -> str:
        """One path's text as its live chunks hold it, in line order, any
        embedding label (a package.json is read this way: a config file's
        line windows cover the file end to end). '' when the
        path is not indexed."""
        rows = await self._ch.query_all(
            """
            SELECT DISTINCT start_line, content FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND tombstoned_at IS NULL
            ORDER BY start_line
            """,
            {"org": org, "ns": ns, "path": path},
        )
        # Windows overlap by a few lines of context, so a
        # concatenation repeats them (a package.json would read back as
        # invalid JSON); place each chunk's lines at their absolute numbers
        by_line: dict[int, str] = {}
        for r in rows:
            start = int(r["start_line"] or 1)
            for i, line in enumerate(str(r["content"]).split("\n")):
                if line or i == 0:
                    by_line.setdefault(start + i, line)
        if not by_line:
            return ""
        last = max(by_line)
        return "\n".join(by_line.get(n, "") for n in range(1, last + 1)) + "\n"

    async def paths_under_prefix(
        self, org: str, ns: str, model: str, prefix: str, *, limit: int = 9
    ) -> list[str]:
        """Live chunk paths equal to or under `prefix`, key-only read over
        the (org, namespace, embedding_model, path) range (a caller may name
        `src/Access/QuotaCache` where the index holds
        QuotaCache.cpp and QuotaCache.h). Capped: a caller that gets `limit`
        rows back asked for a directory, not a file."""
        rows = await self._ch.query_all(
            """
            SELECT DISTINCT path FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND startsWith(path, {prefix:String})
              AND tombstoned_at IS NULL
            ORDER BY path
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "model": model, "prefix": prefix, "limit": limit},
        )
        return [str(r["path"]) for r in rows]

    async def track_path_chunks(
        self,
        org: str,
        ns: str,
        model: str,
        paths: list[str],
        tokens: list[str],
        idfs: list[float],
        query_vec: list[float] | None,
        *,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """track_path diagnostic: every live chunk of a few named paths
        scored with the SAME lexical expression the lexical leg
        uses (tokens + positional idfs) and the same cosine the vector leg
        uses. The predicate walks the ORDER BY prefix (org, namespace,
        embedding_model, path), so only those files' granules are read;
        plain WHERE like every read in this plane. `distance` is -1 for a
        chunk with no vector or when no query vector was given (arrayResize
        pads the empty vector so cosineDistance never throws on it; the `if`
        masks the NaN). On 26.8 with 3072-dim bf16 vectors: one indexed file
        8.4 MB / ~20 ms with the vector, 1.1 MB / 12 ms
        without — the vector column's granule is the bytes."""
        if not paths:
            return []
        vec_expr = (
            "if(embedding_dim > 0, cosineDistance(arrayResize(embedding, "
            "length({q:Array(Float32)})), {q:Array(Float32)}), -1.0)"
            if query_vec
            else "toFloat64(-1.0)"
        )
        return await self._ch.query_all(
            f"""
            SELECT chunk_id, path, scope, kind, start_line, end_line, embedding_dim,
                   arraySum(
                       arrayMap(
                           (tok, idf) -> idf * countSubstrings(
                               concat(' ', lexemes, ' '), concat(' ', tok, ' ')
                           ),
                           {{tokens:Array(String)}}, {{idfs:Array(Float32)}}
                       )
                   ) AS lex_score,
                   {vec_expr} AS distance
            FROM loom.code_chunks FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND path IN {{paths:Array(String)}}
              AND tombstoned_at IS NULL
            ORDER BY path, start_line
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                "paths": paths,
                "tokens": tokens,
                "idfs": idfs,
                "limit": limit,
                **({"q": query_vec} if query_vec else {}),
            },
        )

    async def best_chunks_for_paths(
        self,
        org: str,
        ns: str,
        model: str,
        query_vec: list[float],
        paths: list[str],
        *,
        per_path: int = 1,
    ) -> list[dict[str, Any]]:
        """For each of `paths` (the nearest source files by
        card), the `per_path` chunks closest to the query by exact cosine —
        the vector leg's unit columns plus `distance`, nearest first. A
        keyed read of a few files' vectors; no FINAL-free trick needed at
        this size."""
        if not paths:
            return []
        return await self._ch.query_all(
            """
            SELECT chunk_id, path, scope, kind, start_line, end_line,
                   content, symbol_ids, context_description,
                   cosineDistance(embedding, {q:Array(Float32)}) AS distance
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND path IN {paths:Array(String)}
              AND embedding_dim > 0 AND tombstoned_at IS NULL
            ORDER BY distance ASC
            LIMIT {per_path:UInt32} BY path
            LIMIT {lim:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                "q": query_vec,
                "paths": sorted(set(paths)),
                "per_path": int(per_path),
                "lim": int(per_path) * len(set(paths)),
            },
        )

    async def chunks_for_paths(
        self,
        org: str,
        ns: str,
        model: str,
        paths: list[str],
        *,
        per_path: int = 2,
    ) -> list[dict[str, Any]]:
        """Up to `per_path` live chunk rows per path in the shape the fusion
        consumes (unit_from_chunk), skeleton-level units first — the file
        header / class skeleton is the unit a file-level vote stands for.
        No vector column."""
        if not paths:
            return []
        return await self._ch.query_all(
            """
            SELECT chunk_id, path, scope, kind, start_line, end_line,
                   content, symbol_ids, context_description
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND tombstoned_at IS NULL
              AND path IN {paths:Array(String)}
            ORDER BY path, kind = 'class_skeleton' DESC, start_line
            LIMIT {per_path:UInt32} BY path
            """,
            {
                "org": org,
                "ns": ns,
                "model": model,
                "paths": paths[:50],
                "per_path": max(1, int(per_path)),
            },
        )

    async def list_files(
        self,
        org: str,
        ns: str,
        *,
        glob_re: str = "",
        stem: str = "",
        path_prefix: str = "",
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Live indexed files by name (code.files) — a keyed
        read over loom.code_files (one row per file), no chunk bodies.
        `glob_re` is the anchored RE2 of an rg-style glob; `stem` matches
        the basename without extension exactly or as its leading prefix
        (`05138_with_fill` → 05138_with_fill_step_below_precision.sql),
        exact first. The second value is the count before `limit`."""
        if not (glob_re or stem or path_prefix):
            return [], 0
        stem_expr = "splitByChar('.', arrayElement(splitByChar('/', path), -1))[1]"
        conds = []
        if path_prefix:
            conds.append("AND startsWith(path, {prefix:String})")
        if glob_re:
            conds.append("AND match(path, {glob_re:String})")
        if stem:
            conds.append(f"AND startsWith({stem_expr}, {{stem:String}})")
        order = f"({stem_expr} != {{stem:String}}), path" if stem else "path"
        rows = await self._ch.query_all(
            f"""
            SELECT path, lang, size_bytes, loc, indexed_commit,
                   count() OVER () AS total
            FROM loom.code_files FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND tombstoned_at IS NULL
              {" ".join(conds)}
            ORDER BY {order}
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "prefix": path_prefix,
                "glob_re": glob_re,
                "stem": stem,
                "limit": limit,
            },
        )
        files: list[dict[str, Any]] = []
        for r in rows:
            files.append(
                {
                    "path": str(r["path"]),
                    "lang": str(r.get("lang") or ""),
                    "size_bytes": int(r.get("size_bytes") or 0),
                    "loc": int(r.get("loc") or 0),
                    "indexed_commit": _hex(r.get("indexed_commit")),
                }
            )
        total = int(rows[0].get("total") or 0) if rows else 0
        return files, total

    async def chunks_of_path(
        self, org: str, ns: str, path: str, *, limit: int = 400
    ) -> list[dict[str, Any]]:
        """Every live chunk of one file in line order — for the few files a
        read has to parse end to end (SettingsChangesHistory.cpp: a change
        entry's release is the nearest `addSettingsChanges("X.Y"` above it,
        often chunks away). Keyed point read on the sort key;
        the caller caches the parse."""
        if not path:
            return []
        return await self._ch.query_all(
            """
            SELECT start_line, end_line, content, context_description
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND tombstoned_at IS NULL
            ORDER BY start_line
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "path": path, "limit": max(1, min(int(limit), 2000))},
        )

    async def chunks_for_patch(
        self,
        org: str,
        ns: str,
        paths: list[str],
        symbols: list[str],
        needles: list[str],
        *,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """The live chunks of `paths` a hunk-level presence test needs: the
        enclosing functions the fix's hunks name (scope equals or ends in
        `::<symbol>`) and any chunk carrying one of the patch's lines
        (`multiSearchAny` over the
        added and removed lines' longest runs). One statement per branch on
        the path range of the sort key; bodies of the matching chunks only,
        line order, no vector."""
        paths = [p for p in paths if p][:40]
        symbols = [s for s in symbols if s][:32]
        needles = [n for n in needles if n][:128]
        if not paths or not (symbols or needles):
            return []
        return await self._ch.query_all(
            """
            SELECT path, scope, kind, start_line, end_line, content
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
              AND (arrayExists(n -> scope = n OR endsWith(scope, concat('::', n)),
                               {symbols:Array(String)})
                   OR (length({needles:Array(String)}) > 0
                       AND multiSearchAny(content, {needles:Array(String)})))
            ORDER BY path, start_line
            LIMIT {limit:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "paths": paths,
                "symbols": symbols,
                "needles": needles,
                "limit": max(1, min(int(limit), 2000)),
            },
        )

    async def paths_by_stem(
        self, org: str, ns: str, stems: list[str], *, limit: int = 40
    ) -> list[dict[str, Any]]:
        """Live source paths whose basename stem is one of `stems`
        (`MergeTreeData` → src/…/MergeTreeData.cpp, .h). A read over
        loom.code_files (one row per file, ~10k per namespace), no chunk
        bodies."""
        wanted = sorted({str(s).strip() for s in stems if str(s).strip()})[:20]
        if not wanted:
            return []
        return await self._ch.query_all(
            """
            SELECT path,
                   splitByChar('.', arrayElement(splitByChar('/', path), -1))[1] AS stem
            FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND splitByChar('.', arrayElement(splitByChar('/', path), -1))[1]
                  IN {stems:Array(String)}
            ORDER BY path
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "stems": wanted, "limit": limit},
        )

    async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
        """Which of `paths` the index holds live — a membership check on the
        file table's sort key (the `.reference` sibling next
        to a test, for code.files and code.test_home; whether a seed path is in the
        index at all, for code.impact and code.blame). At most 500 paths."""
        wanted = sorted({str(p) for p in paths if p})[:500]
        if not wanted:
            return set()
        rows = await self._ch.query_all(
            """
            SELECT path FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "paths": wanted},
        )
        return {str(r["path"]) for r in rows}

    async def live_path_set(self, org: str, ns: str) -> set[str]:
        """Every live path of the namespace: the oracle the
        import ladders resolve against, read once per ingesting process and
        TTL instead of one capped `IN` probe per file. One key-range read on
        the file table; a namespace holds thousands of paths, not millions."""
        rows = await self._ch.query_all(
            """
            SELECT path FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns},
        )
        return {str(r["path"]) for r in rows}

    async def max_stateless_test_number(self, org: str, ns: str) -> int:
        """The highest NNNNN prefix among tests/queries/0_stateless files the
        index holds — the next free number is one above it ("a stateless
        test number already taken" is a rejection class)."""
        row = await self._ch.query_one(
            """
            SELECT max(toUInt32OrZero(extract(path, '0_stateless/(\\d{5})_'))) AS n
            FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND startsWith(path, 'tests/queries/0_stateless/')
            """,
            {"org": org, "ns": ns},
        )
        return int((row or {}).get("n") or 0)

    async def stateless_paths_for_numbers(
        self, org: str, ns: str, numbers: list[str], *, limit: int = 400
    ) -> list[str]:
        """Live stateless test paths carrying one of `numbers` (five-digit
        prefixes) — the stems a new test's number is shared with, and any
        file of the same name. A read over code_files, no
        chunk bodies."""
        nums = sorted({str(n) for n in numbers if str(n).isdigit()})[:20]
        if not nums:
            return []
        rows = await self._ch.query_all(
            """
            SELECT path FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND startsWith(path, 'tests/queries/0_stateless/')
              AND extract(path, '0_stateless/(\\d{5})_') IN {nums:Array(String)}
            ORDER BY path
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "nums": nums, "limit": max(1, min(int(limit), 2000))},
        )
        return [str(r["path"]) for r in rows]

    async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
        """path -> content_hash for every live indexed file."""
        rows = await self._ch.query_all(
            """
            SELECT path, content_hash FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns},
        )
        out: dict[str, str] = {}
        for r in rows:
            out[str(r["path"])] = _hex(r["content_hash"])
        return out

    async def chunk_units_for_paths(
        self, org: str, ns: str, model: str, paths: list[str], *, per_statement: int = 50
    ) -> list[dict[str, Any]]:
        """Every live chunk of `paths` as a span — chunk_id, path, kind,
        start_line, end_line — no content, no vector: the units a risk row
        is computed for. Keyed on the table's (org, namespace,
        embedding_model, path) prefix; `per_statement` paths a statement (50;
        the weekly builder passes a whole round)."""
        rows: list[dict[str, Any]] = []
        for batch in _batched(list(dict.fromkeys(paths)), max(1, int(per_statement))):
            rows.extend(
                await self._ch.query_all(
                    """
                    SELECT chunk_id, path, kind, start_line, end_line
                    FROM loom.code_chunks FINAL
                    WHERE org = {org:String} AND namespace = {ns:String}
                      AND embedding_model = {model:String} AND tombstoned_at IS NULL
                      AND path IN {paths:Array(String)}
                    ORDER BY path, start_line, chunk_id
                    """,
                    {"org": org, "ns": ns, "model": model, "paths": batch},
                )
            )
        return rows

    async def embeddings_for_texts(
        self, org: str, ns: str, *, embedding_model: str, path: str, content_hashes: list[str]
    ) -> list[dict[str, Any]]:
        """(`context_text`, `embedding`) of the live, embedded chunks of `path` under
        `embedding_model` whose content hash is one of `content_hashes`."""
        rows: list[dict[str, Any]] = await self._ch.query_all(
            """
            SELECT context_text, embedding FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND path = {path:String}
              AND content_hash IN {hashes:Array(String)}
              AND embedding_dim > 0 AND tombstoned_at IS NULL
            """,
            {
                "org": org,
                "ns": ns,
                "model": embedding_model,
                "path": path,
                "hashes": sorted(set(content_hashes)),
            },
        )
        return rows

    async def chunk_page_for_copy(
        self,
        org: str,
        ns: str,
        embedding_model: str,
        columns: tuple[str, ...],
        *,
        path_prefix: str,
        after_path: str,
        limit: int,
        only_path: str = "",
    ) -> list[dict[str, Any]]:
        """`columns` of the live chunks under `embedding_model` after `after_path`, in
        (path, chunk_id) order, at most `limit`; `only_path` reads that one path whole
        instead. A plain WHERE on the key prefix."""
        bound: dict[str, Any]
        if only_path:
            where, bound, limit_sql = "AND path = {only:String}", {"only": only_path}, ""
        else:
            where, limit_sql = "AND path > {after:String}", "LIMIT {limit:UInt32}"
            bound = {"after": after_path, "limit": limit}
        rows: list[dict[str, Any]] = await self._ch.query_all(
            f"""
            SELECT {", ".join(columns)}
            FROM loom.code_chunks FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND tombstoned_at IS NULL
              {where}
              {"AND startsWith(path, {prefix:String})" if path_prefix else ""}
            ORDER BY path, chunk_id
            {limit_sql}
            """,
            {
                "org": org,
                "ns": ns,
                "model": embedding_model,
                **bound,
                **({"prefix": path_prefix} if path_prefix else {}),
            },
        )
        return rows

    async def vector_ids_for_paths(
        self, org: str, ns: str, embedding_model: str, paths: list[str]
    ) -> set[tuple[str, str]]:
        """(path, chunk_id) of the live rows under `embedding_model` for `paths`, read
        from the model's vector table."""
        from server.core.vectors.code_tables import code_vector_table

        if not paths:
            return set()
        rows = await self._ch.query_all(
            f"""
            SELECT path, chunk_id FROM {code_vector_table(embedding_model)} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND embedding_model = {{model:String}} AND tombstoned_at IS NULL
              AND path IN {{paths:Array(String)}}
            """,
            {"org": org, "ns": ns, "model": embedding_model, "paths": sorted(set(paths))},
        )
        return {(str(r["path"]), str(r["chunk_id"])) for r in rows}

"""Reads over loom.code_symbols and its name / id projections: symbol
lookup by name, id, signature or path, outlines, and the defining chunk
hash of a symbol.
"""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _batched, _hex, like_contains
from server.code.store.index.symbol_rank import (
    SYMBOL_GENERATED_PATH_RE,
    SYMBOL_NOISE_PATH_RE,
    _prefer_scip,
    collapse_outline_rows,
    demote_generated_paths,
    demote_member_rows,
    demote_noise_paths,
    return_type_from_signature,
)
from server.core.ch_params import chunks


class SymbolsMixin(CodeRepoBase):
    """Symbol definitions, both tiers."""

    async def return_types_for_qualified(
        self, org: str, ns: str, qualified_names: list[str]
    ) -> dict[str, str]:
        """qualified_name -> the type the function returns: the
        `return_type` column when the parser wrote one, else the return
        annotation carried in `signature` (`(o: Opts): ClickHouseClient`,
        `Promise<T>` → T). One keyed read on the sort prefix
        `(org, namespace, tier, qualified_name)`; newest version decides,
        tombstoned rows are out."""
        if not qualified_names:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT qualified_name,
                   argMax(return_type, version) AS rt,
                   argMax(signature, version) AS sig,
                   argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_symbols
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tier = 'treesitter'
              AND qualified_name IN {qns:Array(String)}
            GROUP BY qualified_name
            HAVING is_live = 1
            """,
            {"org": org, "ns": ns, "qns": sorted(set(qualified_names))},
        )
        out: dict[str, str] = {}
        for r in rows:
            rt = str(r["rt"] or "") or return_type_from_signature(str(r["sig"] or ""))
            if rt:
                out[str(r["qualified_name"])] = rt
        return out

    async def live_symbols_for_path(
        self, org: str, ns: str, path: str, tier: str
    ) -> list[dict[str, Any]]:
        """The reconcile columns of one path's live symbols: key, span, kind,
        signature — what ingest compares a parse against.

        `path` is not in the sort key (org, namespace, tier, qualified_name,
        symbol_id). Under FINAL the path skip index selects its granules and
        ClickHouse then re-expands the selection to every granule that
        shares their key range (PrimaryKeyExpand), so a `SELECT * FINAL`
        reads 229k rows / 72 MB per file. Two reads instead: key
        discovery WITHOUT FINAL, where the skip index prunes for real (41k
        rows / 4 MB), then the authoritative FINAL read restricted to those
        keys, which the primary index serves from their granules (12.9 MB
        with every column; the pruning, not the column list, is the saving
        here). A key whose newest version is tombstoned or lives at
        another path drops out in the second read. Full rows: the ingest
        mirrors them into tombstones and moved versions."""
        cand = await self._ch.query_all(
            """
            SELECT DISTINCT qualified_name, symbol_id
            FROM loom.code_symbols
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tier = {tier:String} AND path = {path:String}
            """,
            {"org": org, "ns": ns, "path": path, "tier": tier},
        )
        keys = [(str(r["qualified_name"]), str(r["symbol_id"])) for r in cand]
        out: list[dict[str, Any]] = []
        for batch in _batched(keys, 1000):
            out.extend(
                await self._ch.query_all(
                    """
                SELECT * FROM loom.code_symbols FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tier = {tier:String}
                  AND (qualified_name, symbol_id) IN {keys:Array(Tuple(String, String))}
                  AND path = {path:String} AND tombstoned_at IS NULL
                """,
                    {"org": org, "ns": ns, "path": path, "tier": tier, "keys": batch},
                )
            )
        return out

    async def symbol_names_for_paths(
        self,
        org: str,
        ns: str,
        paths: list[str],
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """The names and kinds of the live symbols defined in `paths` — the
        repository's own vocabulary for the files the nearest cards point
        at. One read of two small columns, keyed by path; no bodies, no
        signatures."""
        if not paths:
            return []
        # No FINAL — on SharedReplacingMergeTree a FINAL read keyed by a
        # non-key column (path; the key leads with tier and qualified_name)
        # scans the namespace's rows: 975k rows and 184 MB per call on the
        # ClickHouse repository against 9.5 MB for this shape, which picks
        # the latest version per symbol row itself with the null-safe
        # liveness idiom `argMax(tombstoned_at IS NULL, version)`.
        return await self._ch.query_all(
            """
            SELECT name, kind, path
            FROM (
                SELECT tier, qualified_name, symbol_id, name, kind, path,
                       argMax(tombstoned_at IS NULL, version) AS is_live
                FROM loom.code_symbols
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)} AND name != ''
                GROUP BY tier, qualified_name, symbol_id, name, kind, path
            )
            WHERE is_live = 1
            LIMIT {lim:UInt32}
            """,
            {"org": org, "ns": ns, "paths": sorted(set(paths)), "lim": int(limit)},
        )

    async def live_symbols_for_paths(
        self, org: str, ns: str, paths: list[str], tier: str
    ) -> dict[str, list[dict[str, Any]]]:
        """`live_symbols_for_path` for a round of paths: the same two reads —
        key discovery without FINAL over the path skip index,
        then the authoritative FINAL read of those keys — with the round's
        paths in both predicates, routed back by the row's live path. A key
        whose newest version lives at a path outside the round drops out, as
        it does per path; every requested path is present."""
        out: dict[str, list[dict[str, Any]]] = {str(p): [] for p in paths}
        for batch in _batched(sorted(out), 100):
            cand = await self._ch.query_all(
                """
                SELECT DISTINCT qualified_name, symbol_id
                FROM loom.code_symbols
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tier = {tier:String} AND path IN {paths:Array(String)}
                """,
                {"org": org, "ns": ns, "paths": batch, "tier": tier},
            )
            keys = [(str(r["qualified_name"]), str(r["symbol_id"])) for r in cand]
            for kb in _batched(keys, 1000):
                rows = await self._ch.query_all(
                    """
                    SELECT * FROM loom.code_symbols FINAL
                    WHERE org = {org:String} AND namespace = {ns:String}
                      AND tier = {tier:String}
                      AND (qualified_name, symbol_id) IN {keys:Array(Tuple(String, String))}
                      AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
                    """,
                    {"org": org, "ns": ns, "paths": batch, "tier": tier, "keys": kb},
                )
                for r in rows:
                    out.setdefault(str(r["path"]), []).append(r)
        return out

    async def name_candidates(
        self, org: str, ns: str, names: list[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        """bare name -> [(symbol_id, qualified_name, kind)] across both
        tiers, for edge resolution. Only for the requested names, so
        per-merge ingest never loads the whole symbol table. `kind` rides
        along so pytest fixture resolution can restrict candidates to
        kind='fixture' — a test parameter must never resolve to an
        arbitrary same-named function."""
        if not names:
            return {}
        # One statement over `code_symbol_names`. The key (name, tier,
        # qualified_name, symbol_id) mirrors every version the main table ever
        # wrote for it, so the newest version decides: kind by argMax, liveness
        # by `argMax(tombstoned_at IS NULL, version)` (a NULL-skipping argMax
        # over tombstoned_at would read a tombstoned-then-re-inserted key as
        # dead). No FINAL: the ReplacingMergeTree collapse is an optimization
        # here, never what the answer depends on. The two tiers are folded
        # app-side.
        rows = await self._ch.query_all(
            """
            SELECT name, tier, qualified_name, symbol_id,
                   argMax(kind, version) AS kind,
                   argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_symbol_names
            WHERE org = {org:String} AND namespace = {ns:String}
              AND name IN {names:Array(String)}
            GROUP BY name, tier, qualified_name, symbol_id
            HAVING is_live = 1
            """,
            {"org": org, "ns": ns, "names": names},
        )
        table: dict[str, list[tuple[str, str, str]]] = {}
        seen: set[tuple[str, str, str, str]] = set()
        for r in rows:
            cand = (str(r["symbol_id"]), str(r["qualified_name"]), str(r["kind"]))
            if (str(r["name"]), *cand) in seen:
                continue
            seen.add((str(r["name"]), *cand))
            table.setdefault(str(r["name"]), []).append(cand)
        for cands in table.values():
            cands.sort()
        return table

    async def symbols_by_name(
        self, org: str, ns: str, name: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Exact/suffix symbol lookup, both tiers. `name` may be bare
        (`insert`), qualified (`DB::IColumn::insert`), or partially
        qualified (`IColumn::insert`). The bare-name equality predicate is
        index-served (ngrambf on name); qualification filters app-visible
        rows. Callers prefer tier='scip' per (qualified_name, symbol_id)."""
        # Python surface form: agents write Cls.method / module.fn with
        # dots, the index stores :: — normalize before matching. Module
        # scopes contain dots INSIDE one component
        # (tests.integration.x.test::f), so this is a query-side
        # convenience only and never applied to stored names.
        written = name
        if "." in name and "::" not in name:
            name = name.replace(".", "::")
        bare = name.split("::")[-1]
        # The bare-equality disjunct only applies to BARE queries: a
        # qualified lookup (IColumn::insert) must not also match unscoped
        # symbols that happen to share the method name (found on the real
        # ClickHouse corpus: example binaries' free insert() functions).
        bare_clause = "OR qualified_name = {bare:String}" if "::" not in name else ""
        # A SQL object is stored schema-qualified with a DOT inside its last
        # component (`mod::test.xml_dictionary_table`), so a bare
        # `xml_dictionary_table` matches on `.bare` and the written
        # `test.xml_dictionary_table` on `::test.xml_dictionary_table` — the
        # dotted form the caller typed, before the Python normalisation above.
        sql_clause = "OR endsWith(qualified_name, {dot_suffix:String})" if "::" not in name else ""
        # Source before test/example/fuzzer paths, then compiler tier, then
        # types over functions, then span. A Markdown section whose heading
        # is a bare symbol name is not a definition of it (a doc section
        # titled `Ballots` must not outrank the TLA operator
        # Recovery::Ballots), so sections sort with suites and statements,
        # after every definition. Test paths sort last because the compiler
        # tier records a class as a one-line span: on the ClickHouse index a
        # bare `Field` would otherwise return the 48-line Python helper
        # tests/integration/helpers/dictionary.py::Field above DB::Field on
        # size. The regex is bound as a parameter, never interpolated
        # (ClickHouse string literals eat backslashes).
        # Key discovery from the name-keyed side table (code_symbol_names,
        # a point read by (org, namespace, name)), then the
        # ranked FINAL read of the main table by full sort key.
        cand = await self._ch.query_all(
            f"""
            SELECT DISTINCT tier, qualified_name, symbol_id
            FROM loom.code_symbol_names
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND name IN {{names:Array(String)}}
              AND (qualified_name = {{full:String}}
                   OR endsWith(qualified_name, {{suffix:String}})
                   OR endsWith(qualified_name, {{sql_suffix:String}})
                   {bare_clause} {sql_clause})
            """,
            # `names`: the bare name after the dot normalisation AND the
            # written last component — a symbol whose OWN name carries dots
            # (a Helm define `chart.fullname`; a dbt model
            # `schema.model`) is stored under that dotted name, and the
            # normalisation above would otherwise look it up as `fullname`
            {
                "org": org,
                "ns": ns,
                "names": sorted({bare, written.split("::")[-1]}),
                "bare": bare,
                "full": name,
                "suffix": "::" + name,
                "sql_suffix": "::" + written,
                "dot_suffix": "." + bare,
            },
        )
        keys = [(str(r["tier"]), str(r["qualified_name"]), str(r["symbol_id"])) for r in cand]
        if not keys:
            return []
        rows = await self._ch.query_all(
            """
            SELECT * FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND (tier, qualified_name, symbol_id)
                  IN {keys:Array(Tuple(String, String, String))}
              AND tombstoned_at IS NULL
            ORDER BY match(path, {noise:String}) ASC,
                     match(path, {generated:String}) ASC,
                     kind IN ('suite', 'statement', 'section') ASC,
                     kind IN ('field', 'method') ASC,
                     tier DESC,
                     kind IN ('class', 'struct', 'enum') DESC,
                     end_line - start_line DESC, qualified_name
            LIMIT {limit:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "keys": keys[:4000],
                "limit": limit,
                "noise": SYMBOL_NOISE_PATH_RE,
                "generated": SYMBOL_GENERATED_PATH_RE,
            },
        )
        if "::" not in name:  # a bare name answers its top-level declaration first
            rows = demote_member_rows(rows)
        return _prefer_scip(demote_generated_paths(demote_noise_paths(rows)))

    async def functions_for_paths(
        self,
        org: str,
        ns: str,
        paths: list[str],
        *,
        limit: int = 400,
        kinds: tuple[str, ...] = ("function", "method", "fixture"),
        with_extent_only: bool = False,
        per_statement: int = 20,
    ) -> list[dict[str, Any]]:
        """Live function/method symbols defined in the given paths, both
        tiers (scip preferred). Powers code.ask mechanism expansion: match
        question terms against the symbol table of the retrieved files to
        find the implementing function whose chunk retrieval ranked below
        the context cutoff. `per_statement` paths share one statement (the
        read is a skip-index scan that costs the same whatever the IN list
        holds; the weekly builder passes a whole round).
        The ORDER BY closes with (path, start_line, symbol_id): two
        single-line compiler rows tie on (tier, extent), and the row order
        decides which symbol's callees fill a chunk's risk hop first, so
        without the tie-break the same file read alone and read in a batch
        could give different callee lists."""
        if not paths:
            return []
        # Batched by path so a diff touching many files drops none of them and
        # one huge file cannot starve the rest of a shared LIMIT (Settings.cpp
        # alone has hundreds of methods).
        rows: list[dict[str, Any]] = []
        step = max(1, int(per_statement))
        for i in range(0, len(paths[:200]), step):
            batch = paths[i : i + step]
            rows.extend(
                await self._ch.query_all(
                    """
                SELECT symbol_id, qualified_name, name, signature, path,
                       start_line, end_line, tier, kind
                FROM loom.code_symbols FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)}
                  AND kind IN {kinds:Array(String)}
                  AND tombstoned_at IS NULL
                """
                    + ("  AND end_line > start_line\n" if with_extent_only else "")
                    + """
                ORDER BY tier DESC, end_line - start_line DESC, path, start_line, symbol_id
                LIMIT {limit:UInt32}
                """,
                    {
                        "org": org,
                        "ns": ns,
                        "paths": batch,
                        "kinds": list(kinds),
                        "limit": max(limit, 300 * len(batch)),
                    },
                )
            )
        return _prefer_scip(rows)

    async def outline_for_path(
        self,
        org: str,
        ns: str,
        path: str,
        *,
        kinds: tuple[str, ...] = ("function", "method", "class", "struct", "fixture"),
        limit: int = 3000,
    ) -> list[dict[str, Any]]:
        """Every live declaration of the requested kinds in ONE file, in
        source order — the read behind code.outline ("what is in this
        file?"). One statement: `code_symbols` is keyed (org, namespace,
        tier, qualified_name, symbol_id), so the path predicate is served by
        `idx_code_symbols_path` (bloom_filter, GRANULARITY 4) exactly as
        `functions_for_paths` pays for it; plain WHERE, as everywhere in the
        code plane.

        Two-tier collapse, per declaration (qualified_name, start_line): the
        compiler tier records a one-line span for classes and for functions
        it saw only declared, the parser tier carries the body extent — an
        outline is line spans, so the row WITH an extent survives and the
        entry's `tier` says 'scip' when the compiler confirmed the
        declaration at that line. Class/struct rows without an extent
        (forward declarations, aliases) are dropped after the collapse.
        Tombstones are excluded; the caller budgets rows whole."""
        if not path:
            return []
        rows = await self._ch.query_all(
            """
            SELECT symbol_id, qualified_name, name, signature, path,
                   start_line, end_line, tier, kind
            FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String}
              AND kind IN {kinds:Array(String)}
              AND tombstoned_at IS NULL
            ORDER BY start_line ASC, end_line - start_line DESC, tier DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "path": path, "kinds": list(kinds), "limit": limit},
        )
        return collapse_outline_rows(rows)

    async def scip_covered_paths(self, org: str, ns: str, paths: list[str]) -> set[str]:
        """Paths the compiler tier has at least one live symbol in — the
        per-file coverage fact behind `epistemic: exact`. Read from the live
        symbol table, not from code_scip_parts: the parts are upload blobs
        keyed by batch with no path column and a seven-day TTL, so they
        cannot say which files the tier covers now. `tier` leads the sort key
        after the identity and the path
        bloom filter prunes the rest; plain WHERE."""
        if not paths:
            return set()
        rows = await self._ch.query_all(
            """
            SELECT DISTINCT path
            FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tier = 'scip' AND path IN {paths:Array(String)}
              AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "paths": paths[:200]},
        )
        return {str(r["path"]) for r in rows}

    async def scip_stale_paths(self, org: str, ns: str, paths: list[str]) -> set[str]:
        """Paths a compiler-tier edge may not be read from as current: the fast tier
        wrote the file's row after the compiler tier last ran (the `.py` watermark,
        `scip_commit_py`, for a Python file when there is one, `scip_commit`
        otherwise), deleted the file, or holds no row for it. The fast tier writes a
        file row when the file's bytes change, so a row newer than the watermark is
        content the compiler tier has not seen; a deleted or unknown file is a tree
        the compiler tier's rows no longer describe."""
        if not paths:
            return set()
        asked = list(dict.fromkeys(paths))[:200]
        rows = await self._ch.query_all(
            """
            WITH
                (SELECT maxIf(updated_at, state_key = 'scip_commit')
                 FROM loom.code_index_state FINAL
                 WHERE org = {org:String} AND namespace = {ns:String}) AS t_scip,
                (SELECT maxIf(updated_at, state_key = 'scip_commit_py')
                 FROM loom.code_index_state FINAL
                 WHERE org = {org:String} AND namespace = {ns:String}) AS t_py
            SELECT path
            FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
              AND created_at <= if(endsWith(path, '.py') AND t_py > toDateTime(0), t_py, t_scip)
            """,
            {"org": org, "ns": ns, "paths": asked},
        )
        # the query names the FRESH paths; every other asked path is stale
        return set(asked) - {str(r["path"]) for r in rows}

    async def live_scip_paths(self, org: str, ns: str) -> set[str]:
        """Every path holding a live compiler-tier symbol row:
        one namespace-wide distinct read, taken once per full compiler run so
        the documents the new index no longer has can be retired."""
        rows = await self._ch.query_all(
            """
            SELECT DISTINCT path
            FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tier = 'scip' AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns},
        )
        return {str(r["path"]) for r in rows}

    async def types_for_paths(
        self, org: str, ns: str, paths: list[str], *, limit: int = 400
    ) -> list[dict[str, Any]]:
        """Class / struct / enum rows WITH a body extent in the given paths —
        the fallback scope for a line that sits in no function (a member
        declaration in a header). Single-line rows (forward declarations,
        compiler-only sightings) are excluded at the query: they cannot
        enclose anything but themselves."""
        return await self.functions_for_paths(
            org,
            ns,
            paths,
            limit=limit,
            # The lane languages' containers enclose lines too
            kinds=(
                "class",
                "struct",
                "enum",
                "interface",
                "type",
                "const",
                "suite",
                "module",
                "trait",
                "impl",
                "model",
                "macro",
            ),
            with_extent_only=True,
        )

    async def chunk_hashes_for_symbols(
        self,
        org: str,
        ns: str,
        symbol_ids: list[str],
        *,
        rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        """symbol_id -> content_hash of its DEFINING chunk. This is what a
        code-referencing memory anchors to: equal hash = the
        definition text is byte-identical to what the memory was written
        against; different = the body changed even if name and file did not.

        Two passes. (1) The lowest-start live chunk TAGGED with the id — but
        chunk symbol_ids come from the tree-sitter ingest, and the compiler
        tier hashes the same definition to a different id (parameter text vs
        scip overload hash), so a scip row matches nothing here. (2) For ids
        still unresolved, the tightest live chunk at the row's path that
        CONTAINS its start_line — the same fallback `symbol_body` uses.
        Pass `rows` (symbol rows with symbol_id/path/start_line) to enable
        pass 2. Ids resolved by neither are absent (unknown, not stale)."""
        ids = [s for s in symbol_ids if s][:200]
        if not ids:
            return {}
        out: dict[str, str] = {}
        tagged = await self._ch.query_all(
            """
            SELECT sid, argMin(content_hash, start_line) AS h
            FROM (
                SELECT arrayJoin(symbol_ids) AS sid, content_hash, start_line
                FROM loom.code_chunks FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tombstoned_at IS NULL
                  AND hasAny(symbol_ids, {ids:Array(String)})
            )
            WHERE sid IN {ids:Array(String)}
            GROUP BY sid
            """,
            {"org": org, "ns": ns, "ids": ids},
        )
        for r in tagged:
            if r.get("h"):
                out[str(r["sid"])] = _hex(r["h"])
        pending = [
            r
            for r in (rows or [])
            if str(r.get("symbol_id") or "") in ids
            and str(r["symbol_id"]) not in out
            and r.get("path")
            and int(r.get("start_line") or 0) > 0
        ]
        pending = pending[:20]
        if not pending:
            return out
        # One read of the pending rows' files, then the tightest chunk that
        # contains each row's line (ties: the earlier start), in Python.
        chunks = await self._ch.query_all(
            """
            SELECT path, start_line, end_line, content_hash FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
              AND start_line <= {max_line:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "paths": sorted({str(r["path"]) for r in pending}),
                "max_line": max(int(r.get("start_line") or 0) for r in pending),
            },
        )
        by_path: dict[str, list[dict[str, Any]]] = {}
        for c in chunks:
            if c.get("content_hash"):
                by_path.setdefault(str(c["path"]), []).append(c)
        for r in pending:
            line = int(r.get("start_line") or 0)
            around = [
                c
                for c in by_path.get(str(r["path"]), [])
                if int(c["start_line"]) <= line <= int(c["end_line"])
            ]
            if around:
                best = min(
                    around,
                    key=lambda c: (int(c["end_line"]) - int(c["start_line"]), int(c["start_line"])),
                )
                out[str(r["symbol_id"])] = _hex(best["content_hash"])
        return out

    async def symbol_ids_for_names(
        self, org: str, ns: str, names: list[str]
    ) -> dict[str, list[str]]:
        """qualified_name -> live symbol ids across BOTH tiers. The one join
        key the chunk table (scope), the parser tier and the compiler tier
        share — ids differ per tier for the same definition."""
        want = [n for n in names if n][:100]
        if not want:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT qualified_name, symbol_id
            FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND qualified_name IN {names:Array(String)} AND tombstoned_at IS NULL
            LIMIT 2000
            """,
            {"org": org, "ns": ns, "names": want},
        )
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(str(r["qualified_name"]), []).append(str(r["symbol_id"]))
        return out

    async def symbols_fuzzy(
        self, org: str, ns: str, needle: str, *, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Symbols whose name contains `needle`, case-insensitively.

        The `lower(name) LIKE` gate is what the n-gram index
        on `lower(name)` answers per granule — the
        `positionCaseInsensitive` predicate alone is a namespace-wide scan
        under FINAL (855k rows, 119 MB per call on the ClickHouse repository)
        because the index on `name` is case-sensitive and
        `position` is not an indexed function. The position predicate stays
        for its exact semantics; the gate never excludes a row it accepts."""
        rows = await self._ch.query_all(
            """
            SELECT * FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND lower(name) LIKE {like:String}
              AND positionCaseInsensitive(name, {needle:String}) > 0
              AND tombstoned_at IS NULL
            ORDER BY tier DESC, length(name), qualified_name
            LIMIT {limit:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "needle": needle,
                "like": like_contains(needle.lower()),
                "limit": limit,
            },
        )
        return _prefer_scip(rows)

    async def members_of(
        self, org: str, ns: str, qualified_names: list[str], limit: int = 64
    ) -> list[dict[str, Any]]:
        """Direct members of container symbols: rows whose
        qualified name extends a seed's by exactly one `::` segment. The key
        starts `(org, namespace, tier, qualified_name)`, so each seed is a
        range read per tier, never a namespace scan."""
        if not qualified_names:
            return []
        rows = await self._ch.query_all(
            """
            SELECT symbol_id, qualified_name, name, kind, path, tier, start_line
            FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
              AND arrayExists(
                    q -> startsWith(qualified_name, concat(q, '::'))
                         AND position(substring(qualified_name, length(q) + 3), '::') = 0,
                    {qns:Array(String)})
            ORDER BY qualified_name
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "qns": qualified_names[:8], "limit": int(limit)},
        )
        return _prefer_scip(rows)

    async def parser_definitions(
        self,
        org: str,
        ns: str,
        *,
        names: list[str] | None = None,
        qualified_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Live tree-sitter definitions with a bare name in `names` or a qualified name in
        `qualified_names`. Tree-sitter rows only: their qualified name is the identity a
        memory's code link keys on (the compiler tier names some differently)."""
        if not names and not qualified_names:
            return []
        # a long list is read a slice at a time, names and qualified names apart
        found = []
        for part in chunks(sorted(set(names or ()))):
            found += await self._parser_definitions(org, ns, part, [])
        for part in chunks(sorted(set(qualified_names or ()))):
            found += await self._parser_definitions(org, ns, [], part)
        return list({(r["qualified_name"], r["symbol_id"], r["path"]): r for r in found}.values())

    async def _parser_definitions(
        self, org: str, ns: str, names: list[str], qualified_names: list[str]
    ) -> list[dict[str, Any]]:
        return await self._ch.query_all(
            """
            SELECT qualified_name, symbol_id, name, kind, path, start_line, end_line
            FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tier = 'treesitter'
              AND (qualified_name IN {qns:Array(String)}
                   OR (qualified_name, symbol_id) IN (
                       SELECT qualified_name, symbol_id FROM loom.code_symbol_names
                       WHERE org = {org:String} AND namespace = {ns:String}
                         AND tier = 'treesitter' AND name IN {names:Array(String)}))
              AND tombstoned_at IS NULL
            """,
            {
                "org": org,
                "ns": ns,
                "names": names,
                "qns": qualified_names,
            },
        )

    async def symbols_by_ids(
        self, org: str, ns: str, symbol_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Rows by symbol id, both tiers. The id is the last component of the
        sort key, so a direct read scans the namespace (498k rows / 207 MB
        per call). Key discovery goes through the id-keyed side table
        `code_symbol_ids` (the same pattern as `symbols_by_name`, fed by a
        materialized view on every `code_symbols` insert) and the FINAL read
        then takes the main table by full key."""
        if not symbol_ids:
            return []
        ids = symbol_ids[:500]
        keys = await self._ch.query_all(
            """
            SELECT DISTINCT tier, qualified_name, symbol_id
            FROM loom.code_symbol_ids
            WHERE org = {org:String} AND namespace = {ns:String}
              AND symbol_id IN {ids:Array(String)}
            """,
            {"org": org, "ns": ns, "ids": ids},
        )
        if not keys:
            return []
        rows = await self._ch.query_all(
            """
            SELECT * FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND (tier, qualified_name, symbol_id)
                  IN {keys:Array(Tuple(String, String, String))}
              AND tombstoned_at IS NULL
            """,
            {
                "org": org,
                "ns": ns,
                "keys": [
                    (str(k["tier"]), str(k["qualified_name"]), str(k["symbol_id"])) for k in keys
                ],
            },
        )
        return _prefer_scip(rows)

    async def symbol_spans_by_ids(
        self, org: str, ns: str, symbol_ids: list[str]
    ) -> list[dict[str, Any]]:
        """`symbols_by_ids` for a caller that needs only where each symbol
        is (code.grep's unit spans): the columns
        `_prefer_scip` reads and nothing else, and the side-table key
        discovery folded into the main read as an IN subquery on the sort
        key — one round trip where `symbols_by_ids` makes two. Same rows,
        same collapse."""
        if not symbol_ids:
            return []
        cols = "tier, qualified_name, symbol_id, name, path, start_line, end_line"
        params = {"org": org, "ns": ns, "ids": symbol_ids[:500]}
        rows = await self._ch.query_all(
            f"""
            SELECT {cols} FROM loom.code_symbols FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND (tier, qualified_name, symbol_id) IN (
                  SELECT tier, qualified_name, symbol_id
                  FROM loom.code_symbol_ids
                  WHERE org = {{org:String}} AND namespace = {{ns:String}}
                    AND symbol_id IN {{ids:Array(String)}}
              )
              AND tombstoned_at IS NULL
            """,
            params,
        )
        return _prefer_scip(rows)

    async def symbol_paths_for_ids(
        self, org: str, ns: str, symbol_ids: list[str]
    ) -> dict[str, str]:
        """{symbol_id: one live defining path} — dialect narrowing needs to
        know WHICH file defines each candidate table; the
        set-valued `symbol_paths_by_ids` cannot say. The first path in sort
        order wins for an id two files share (a header and its source)."""
        out: dict[str, str] = {}
        for batch in _batched(list(dict.fromkeys(s for s in symbol_ids if s)), 500):
            rows = await self._ch.query_all(
                """
                SELECT symbol_id, min(path) AS path FROM loom.code_symbols FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND symbol_id IN {ids:Array(String)} AND tombstoned_at IS NULL
                GROUP BY symbol_id
                """,
                {"org": org, "ns": ns, "ids": batch},
            )
            out.update({str(r["symbol_id"]): str(r["path"]) for r in rows if r.get("path")})
        return out

    async def symbol_paths_by_ids(self, org: str, ns: str, symbol_ids: list[str]) -> set[str]:
        """Every live path defining any of `symbol_ids` — the files a
        commit's calls reach. One column where
        `symbols_by_ids` reads them all: `symbol_id` is the sort key's last
        column, so the IN is a scan of the namespace's rows, and the bytes
        are the columns read (signature, doc_comment, decl_paths ... on
        `SELECT *`). A symbol id is (qualified name, signature), not path: a
        header's declaration and its source's definition share one, and both
        files' units carry the id — DISTINCT path names both, where the
        two-tier collapse kept one row per id. 500 ids a statement."""
        out: set[str] = set()
        for batch in _batched(list(dict.fromkeys(s for s in symbol_ids if s)), 500):
            rows = await self._ch.query_all(
                """
                SELECT DISTINCT path FROM loom.code_symbols FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND symbol_id IN {ids:Array(String)} AND tombstoned_at IS NULL
                """,
                {"org": org, "ns": ns, "ids": batch},
            )
            out |= {str(r["path"]) for r in rows if r.get("path")}
        return out

    async def methods_named(
        self, org: str, ns: str, method: str, *, limit: int = 300
    ) -> list[dict[str, Any]]:
        """Every `*::method` definition, one row per qualified name, carrying
        a human signature (tree-sitter's parameter list; scip stores a hash)
        and the scip symbol_id when that tier has the symbol (scip edges bind
        to it). Powers virtual-dispatch resolution for override seeds."""
        rows = await self._ch.query_all(
            """
            SELECT qualified_name,
                   argMaxIf(symbol_id, version, tier = 'scip') AS scip_id,
                   argMaxIf(symbol_id, version, tier = 'treesitter') AS ts_id,
                   argMaxIf(signature, version, tier = 'treesitter') AS signature,
                   any(path) AS path
            FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND name = {name:String} AND kind IN ('function', 'method')
              AND tombstoned_at IS NULL AND position(qualified_name, '::') > 0
            GROUP BY qualified_name
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "name": method, "limit": limit},
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            sid = str(r.get("scip_id") or "") or str(r.get("ts_id") or "")
            if not sid:
                continue
            out.append(
                {
                    "symbol_id": sid,
                    "qualified_name": str(r["qualified_name"]),
                    "signature": str(r.get("signature") or ""),
                    "path": str(r.get("path") or ""),
                    "kind": "function",
                    "tier": "scip" if r.get("scip_id") else "treesitter",
                }
            )
        return out

    async def symbols_by_signatures(
        self,
        org: str,
        ns: str,
        signatures: list[str],
    ) -> list[dict[str, Any]]:
        """Live symbols whose signature is one of `signatures` (an Angular
        selector is `selector:<name>` on its component class).
        Signature is not in the sort key: one namespace-range read, bounded
        by the small IN list; a namespace holds tens of thousands of rows."""
        if not signatures:
            return []
        return await self._ch.query_all(
            """
            SELECT symbol_id, qualified_name, signature, path FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND signature IN {sigs:Array(String)} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "sigs": sorted(set(signatures))[:200]},
        )

    async def symbol_names_present(
        self, org: str, ns: str, names: list[str]
    ) -> dict[str, list[str]]:
        """Which of `names` (bare or `Cls::method`) this namespace defines —
        name → the qualified names that match, from the name-keyed side
        table (a point read by (org, namespace, name)) with suffix matching:
        a qualified name equals or is a suffix of
        the stored qualified name, a bare name matches any definition of it.
        One read per namespace for every name; no bodies."""
        wanted = [str(n).strip() for n in names if str(n).strip()][:64]
        if not wanted:
            return {}
        bares = sorted({n.split("::")[-1] for n in wanted})
        rows = await self._ch.query_all(
            """
            SELECT DISTINCT name, qualified_name
            FROM loom.code_symbol_names FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND name IN {bares:Array(String)} AND tombstoned_at IS NULL
            LIMIT 4000
            """,
            {"org": org, "ns": ns, "bares": bares},
        )
        by_bare: dict[str, set[str]] = {}
        for r in rows:
            bare_name = str(r.get("name") or "")
            by_bare.setdefault(bare_name, set()).add(str(r.get("qualified_name") or ""))
        out: dict[str, list[str]] = {}
        for n in wanted:
            bare = n.split("::")[-1]
            quals = by_bare.get(bare, set())
            if "::" in n:
                hits = sorted(q for q in quals if q == n or q.endswith("::" + n))
            else:
                hits = sorted(quals)
            if hits:
                out[n] = hits[:8]
        return out

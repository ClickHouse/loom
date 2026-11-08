"""A round's per-file resolution reads, served from round-wide statements.

`ingest_file` reads, per file, the path's live edges (`live_edges_for_path`) and the
name table its call sites resolve against (`name_candidates`). Those two dominate an
ingest pass's reads — one statement per file each, over a walk that already moves in
batches of 200 files.

`RoundReads` wraps the repository for one round of files: `prefetch` parses the round's
sources (the parse `ingest_file` would do; it is handed over so no file is parsed
twice), reads every path's live edges, symbols, declined sites and caller guards in one
statement each (two for the symbols) and the candidates of every call name in the round
in another, and then answers the per-file reads from memory. The declined-site table
leads with `callee_name`, so a per-path read scans the namespace's slice per file; the
round pays that once. Anything not prefetched delegates to the repository: a name the
round did not collect (one a header supplied) is read on the miss, a path outside the
round is read per file, and every other
attribute is the repository's own. A `code_symbols` insert drops the names it carries
from the cache, so a later file in the round re-reads a name this round redefined
(headers before sources); a `code_edges` insert drops its paths. Prefetch failures fail
open to the per-file reads.

Rows are the same either way: the round-wide edge read groups by the same
full key with `path` in it, and the name read groups per key with no LIMIT,
so a name's candidates restricted from the union equal its own read.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

logger = logging.getLogger("loom.code_index.round_reads")

# The order the caller hands `ingest_file` (headers first) is kept; the round is a
# contiguous slice of it.
TIER = "treesitter"


def round_size() -> int:
    """`code_ingest_round_files`, 0 when the batched reads are off."""
    from server.settings import settings

    return max(0, int(settings.code_ingest_round_files))


def buffer_row_cap() -> int:
    """`code_ingest_round_write_rows`: a buffer this large is written early,
    so a round of large files never holds all its vectors at once."""
    from server.settings import settings

    return max(1, int(settings.code_ingest_round_write_rows))


def rounds(items: list[Any], size: int) -> list[list[Any]]:
    """Contiguous slices of `size` (the whole list as one round when size is
    0), so a cursor that advances by completed rounds stays exact."""
    if size <= 0 or not items:
        return [list(items)] if items else []
    return [items[i : i + size] for i in range(0, len(items), size)]


class RoundReads:
    """The repository, with one round's per-file reads answered from memory. A round
    opens it with `async with`, which writes the buffered rows when the round ends."""

    def __init__(self, repo: Any, org: str, ns: str, *, tier: str = TIER) -> None:
        self._repo = repo
        self._org, self._ns, self._tier = org, ns, tier
        self.parsed: dict[str, Any] = {}
        self._edges: dict[str, list[dict[str, Any]]] = {}
        self._symbols: dict[str, list[dict[str, Any]]] = {}
        self._misses: dict[str, list[dict[str, Any]]] = {}
        self._guards: dict[str, list[dict[str, Any]]] = {}
        self._facets: dict[str, list[dict[str, Any]]] = {}
        self._names: dict[str, list[tuple[str, str, str]]] = {}
        self._known: set[str] = set()
        # For the record and the tests: per-file reads answered here / sent on.
        self.hits = 0
        self.misses = 0
        self.statements = 0
        # The round's model answers, keyed by the exact call (round_models), and the
        # per-path reads the prefetch already made.
        self.describe_hints: dict[str, str] = {}
        self.embed_hints: dict[str, list[float]] = {}
        self._chunk_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._cards: dict[str, str] = {}
        self._cards_for: tuple[str, frozenset[str]] | None = None
        # The round's writes, one INSERT per table when a read could see them or the
        # round ends (`flush`) — not one per file.
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._pending_paths: set[str] = set()
        self._pending_rows = 0
        self.flushes = 0
        # A flush in progress still counts as buffered: its paths are not yet
        # durable, so a concurrent reader (the dependents' tasks) waits on the
        # lock rather than read around it.
        self._flush_lock = asyncio.Lock()
        self._inflight_paths: set[str] = set()
        # Return types by qualified name, for the round; a `code_symbols` insert drops
        # the names it carries (None = no type).
        self._return_types: dict[str, str | None] = {}
        # A header's chunks and a path's liveness, per round — otherwise every dependent
        # that includes one header reads it again. A code_chunks / code_files insert for
        # the path drops it.
        self._chunks_of: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._present: dict[str, bool] = {}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._repo, name)
        if not self._unsettled() or not inspect.iscoroutinefunction(attr):
            return attr

        async def _read_after_flush(*args: Any, **kwargs: Any) -> Any:
            if self._may_see_pending(attr, args, kwargs):
                await self.flush()
            return await attr(*args, **kwargs)

        return _read_after_flush

    # -- the round's buffered writes ---------------------------------------------------

    def _may_see_pending(self, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        """Whether a repository read could return a row this round buffered.
        Only a read of THIS namespace scoped to paths none of which the buffer
        holds is known not to; anything else (a name lookup, a namespace-wide
        read, an unknown shape) flushes first, so it sees what it saw before."""
        try:
            bound = inspect.signature(fn).bind(*args, **kwargs).arguments
        except (TypeError, ValueError):
            return True
        ns = bound.get("ns", bound.get("namespace"))
        if ns is not None and ns != self._ns:
            return False
        if "path" in bound:
            return self._holds(str(bound["path"]))
        if "paths" in bound and isinstance(bound["paths"], (list, tuple, set, frozenset)):
            return any(self._holds(str(p)) for p in bound["paths"])
        return True

    def _holds(self, path: str) -> bool:
        """Whether a row of `path` is buffered or being written."""
        return path in self._pending_paths or path in self._inflight_paths

    def _unsettled(self) -> bool:
        return bool(self._pending or self._inflight_paths)

    async def __aenter__(self) -> RoundReads:
        return self

    async def __aexit__(self, *exc: object) -> None:
        """A round's owner writes what the round buffered, whether it finished or raised."""
        await self.flush()

    async def flush(self) -> None:
        """Write the buffered rows, one INSERT per table in first-write order;
        returns once every earlier flush is durable too."""
        async with self._flush_lock:
            pending, self._pending = self._pending, {}
            self._inflight_paths, self._pending_paths = self._pending_paths, set()
            self._pending_rows = 0
            try:
                for table, rows in pending.items():
                    await self._repo.insert_rows(table, rows)
            finally:
                self._inflight_paths = set()
            if pending:
                self.flushes += 1

    # -- what the round's model prefetch read ------------------------------------------

    async def repo_read(self, method: str, *args: Any) -> Any:
        return await getattr(self._repo, method)(*args)

    def cache_chunk_index(self, path: str, model: str, rows: list[dict[str, Any]]) -> None:
        self._chunk_index[(path, model)] = list(rows)

    async def live_chunk_index_for_path(
        self, org: str, ns: str, path: str, embedding_model: str
    ) -> list[dict[str, Any]]:
        key = (path, embedding_model)
        if (org, ns) == (self._org, self._ns) and key in self._chunk_index:
            self.hits += 1
            return self._chunk_index.pop(key)
        if self._holds(path):
            await self.flush()
        rows: list[dict[str, Any]] = await self._repo.live_chunk_index_for_path(
            org, ns, path, embedding_model
        )
        return rows

    async def prefetch_cards(self, org: str, ns: str, model: str, paths: list[str]) -> None:
        """The round's file cards in one statement (only the `questions`
        prompt reads them); a failure leaves the per-file read."""
        from server.code.app.index.describe import describe_uses_cards

        if not paths or (org, ns) != (self._org, self._ns) or not describe_uses_cards():
            return
        try:
            self._cards = dict(await self._repo.cards_for_paths(org, ns, model, paths))
            self._cards_for = (model, frozenset(paths))
            self.statements += 1
        except Exception as exc:  # noqa: BLE001 — a missed prefetch: each file reads its own card
            logger.debug("round reads: card prefetch skipped (%s)", exc)

    async def cards_for_paths(
        self, org: str, ns: str, model: str, paths: list[str]
    ) -> dict[str, str]:
        covered = self._cards_for
        if (
            covered is not None
            and (org, ns) == (self._org, self._ns)
            and covered[0] == model
            and set(paths) <= covered[1]
        ):
            self.hits += 1
            return {p: self._cards[p] for p in paths if p in self._cards}
        out: dict[str, str] = await self.__getattr__("cards_for_paths")(org, ns, model, paths)
        return out

    # -- the round-wide reads --------------------------------------------------

    async def prefetch(
        self, sources: dict[str, bytes], denylist: Any = None, *, embedding_model: str = ""
    ) -> None:
        """Parse the round's files (skipping what `ingest_file` would refuse
        before parsing) and run the round-wide reads: edges, symbols, call
        misses and guards, facets, and with `embedding_model` the chunk index
        each file's content-addressed reconcile compares on. Never raises."""
        from server.code.app.index.ingest_parse import parse_for_round
        from server.code.domain.extract.edges_treesitter import collect_call_names

        if not sources:
            return
        names: set[str] = set()
        for path, source in sources.items():
            # one parse can take `code_parse_timeout_seconds`: the loop runs between files
            await asyncio.sleep(0)
            parsed = parse_for_round(path, source, denylist)
            if parsed is None:
                continue
            self.parsed[path] = parsed
            pf = parsed[0]
            if pf is not None:
                try:
                    names |= collect_call_names(pf, None)
                except Exception:  # noqa: BLE001 — the file's own ingest reports it
                    continue
        paths = list(sources)
        self._edges = await self._round_read("live_edges_for_paths", paths, self._tier)
        self._symbols = await self._round_read("live_symbols_for_paths", paths, self._tier)
        self._misses = await self._round_read("live_call_misses_for_paths", paths)
        self._guards = await self._round_read("live_call_guards_for_paths", paths)
        self._facets = await self._round_read("live_facets_for_paths", paths)
        if embedding_model:
            index = await self._round_read("live_chunk_index_for_paths", paths, embedding_model)
            for path, rows in index.items():
                self._chunk_index.setdefault((path, embedding_model), rows)
        if not names:
            return
        try:
            self._names = await self._repo.name_candidates(self._org, self._ns, sorted(names))
            self._known = set(names)
            self.statements += 1
        except Exception as exc:  # noqa: BLE001 — a missed prefetch: each file reads its own names
            logger.debug("round reads: name prefetch skipped (%s)", exc)
            self._names, self._known = {}, set()

    async def _round_read(self, method: str, paths: list[str], *args: Any) -> dict[str, Any]:
        """One round-wide read by name; a repository without it, or a failing
        statement, fails open to the per-file reads (an empty cache)."""
        try:
            out: dict[str, Any] = await getattr(self._repo, method)(
                self._org, self._ns, paths, *args
            )
            self.statements += max(1, (len(paths) + 99) // 100)
            return out
        except Exception as exc:  # noqa: BLE001 — fail open to the per-file read
            logger.debug("round reads: %s skipped (%s)", method, exc)
            return {}

    # -- the per-file reads `ingest_file` makes ----------------------------------

    async def _per_path(
        self,
        cache: dict[str, list[dict[str, Any]]],
        method: str,
        org: str,
        ns: str,
        path: str,
        *args: Any,
    ) -> list[dict[str, Any]]:
        if (org, ns) == (self._org, self._ns) and path in cache:
            self.hits += 1
            return cache.pop(path)
        self.misses += 1
        if (org, ns) == (self._org, self._ns) and self._holds(path):
            await self.flush()
        rows: list[dict[str, Any]] = await getattr(self._repo, method)(org, ns, path, *args)
        return rows

    async def live_edges_for_path(
        self, org: str, ns: str, path: str, tier: str
    ) -> list[dict[str, Any]]:
        cache = self._edges if tier == self._tier else {}
        return await self._per_path(cache, "live_edges_for_path", org, ns, path, tier)

    async def live_symbols_for_path(
        self, org: str, ns: str, path: str, tier: str
    ) -> list[dict[str, Any]]:
        cache = self._symbols if tier == self._tier else {}
        return await self._per_path(cache, "live_symbols_for_path", org, ns, path, tier)

    async def live_call_misses_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        return await self._per_path(self._misses, "live_call_misses_for_path", org, ns, path)

    async def live_facets_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        return await self._per_path(self._facets, "live_facets_for_path", org, ns, path)

    async def live_call_guards_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        return await self._per_path(self._guards, "live_call_guards_for_path", org, ns, path)

    async def name_candidates(
        self, org: str, ns: str, names: list[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        if (org, ns) != (self._org, self._ns):
            other: dict[str, list[tuple[str, str, str]]] = await self._repo.name_candidates(
                org, ns, names
            )
            return other
        missing = sorted({n for n in names if n not in self._known})
        if missing:
            self.misses += 1
            await self.flush()  # a name read sees every symbol the round wrote
            fetched = await self._repo.name_candidates(org, ns, missing)
            self._names.update(fetched)
            self._known.update(missing)
        else:
            self.hits += 1
        return {n: list(self._names[n]) for n in names if n in self._names}

    async def return_types_for_qualified(
        self, org: str, ns: str, qualified_names: list[str]
    ) -> dict[str, str]:
        """One read per name per round, not one per call site. A name a buffered symbol
        row carries was dropped at its insert and is read after a flush."""
        if (org, ns) != (self._org, self._ns):
            other: dict[str, str] = await self._repo.return_types_for_qualified(
                org, ns, qualified_names
            )
            return other
        missing = sorted({q for q in qualified_names if q not in self._return_types})
        if missing:
            self.misses += 1
            if self._pending.get("code_symbols") or self._inflight_paths:
                await self.flush()
            got = await self._repo.return_types_for_qualified(org, ns, missing)
            for q in missing:
                self._return_types[q] = got.get(q)
        else:
            self.hits += 1
        return {q: t for q in qualified_names if (t := self._return_types.get(q))}

    async def chunks_of_path(
        self, org: str, ns: str, path: str, *, limit: int = 400
    ) -> list[dict[str, Any]]:
        if (org, ns) != (self._org, self._ns):
            other: list[dict[str, Any]] = await self._repo.chunks_of_path(
                org, ns, path, limit=limit
            )
            return other
        key = (path, int(limit))
        if key in self._chunks_of:
            self.hits += 1
            return [dict(r) for r in self._chunks_of[key]]
        self.misses += 1
        if self._holds(path):
            await self.flush()
        rows = await self._repo.chunks_of_path(org, ns, path, limit=limit)
        self._chunks_of[key] = [dict(r) for r in rows]
        return list(rows)

    async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
        if (org, ns) != (self._org, self._ns):
            other: set[str] = await self._repo.live_paths_present(org, ns, paths)
            return other
        wanted = sorted({str(p) for p in paths if p})[:500]
        missing = [p for p in wanted if p not in self._present]
        if missing:
            self.misses += 1
            if any(self._holds(p) for p in missing):
                await self.flush()
            got = set(await self._repo.live_paths_present(org, ns, missing))
            for p in missing:
                self._present[p] = p in got
        else:
            self.hits += 1
        return {p for p in wanted if self._present.get(p)}

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        cache = {
            "code_symbols": self._symbols,
            "code_edges": self._edges,
            "code_call_sites_unresolved": self._misses,
            "code_caller_guards": self._guards,
        }.get(table)
        if cache is not None:
            for r in rows:
                cache.pop(str(r.get("path") or ""), None)
        if table == "code_symbols":
            for r in rows:
                name = str(r.get("name") or "")
                self._known.discard(name)
                self._names.pop(name, None)
                self._return_types.pop(str(r.get("qualified_name") or ""), None)
        if table == "code_chunks":
            touched = {str(r.get("path") or "") for r in rows}
            for key in [k for k in self._chunks_of if k[0] in touched]:
                del self._chunks_of[key]
        elif table == "code_files":
            for r in rows:
                self._present.pop(str(r.get("path") or ""), None)
        if not rows:
            return
        self._pending.setdefault(table, []).extend(rows)
        self._pending_paths.update(str(r.get("path") or "") for r in rows)
        self._pending_rows += len(rows)
        if self._pending_rows >= buffer_row_cap():
            await self.flush()

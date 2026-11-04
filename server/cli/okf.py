"""`loom export-okf` / `loom import-okf` — a namespace as a git-native OKF bundle.

  loom export-okf --namespace NS --out ./knowledge      # rows → Markdown files
  loom import-okf ./knowledge --namespace NS [--verify]  # Markdown files → rows

Both talk to a running Loom over HTTP with the caller's JWT, so they work
against any deployment and go through the same authz, conflict detection
and audit as every other write. The mapping lives in
`server/memory/domain/okf_bundle.py` (pure, unit-tested); this module is the
transport and the file system.

Export is read-only: `memory.list` pages every live row of the namespace
(all agents), `memory.bulk_get` fetches full values in batches, and the
planned files are written under `--out` (existing concept files are
overwritten; nothing else in the directory is touched). Commit the result
to a repo the team already reviews — memory changes become a diff.

Typed edges travel: export pages the namespace's live
`operator_edges` through `memory.export_evidence kind=edges` and writes them
as `relates_to` frontmatter; import re-creates them with `memory.link`
(`--no-links` skips either side). Provenance travels: the
`loom:` block carries `source_class`, `sensitivity`, `superseded_by` and
`valid_to`; `sensitivity: high` rows stay home unless `--include-high`.

Import is `memory.set` per concept (identity from the token or flags), subject
to conflict detection. A concept whose live row already holds everything the
set would write is skipped, as is an edge that is already live: re-sending
them would count as reinforcement and write version and audit rows, so a
scheduled sync stays a no-op until the source changes. `--prune` deletes the
rows an earlier import wrote whose key has left the bundle. With
`--verify`, concepts whose frontmatter carries a `human:` verification are
re-attested through `memory.verify` AS THE IMPORTING CALLER — the server
decides the actor from the token; the bundle's actor is recorded in the
memory's tags for provenance, never forged.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.cli._client import (
    default_base_url,
    error_line,
    jwt_claims,
    refused_for_agent,
    resolve_token,
)
from server.core.datetimes import naive_utc_now as _now
from server.core.datetimes import to_naive_utc
from server.core.hashing import sha256_value
from server.memory.domain import okf_bundle as okf
from server.memory.domain import okf_evidence as okf_ev
from server.memory.domain import okf_lint
from server.memory.domain.edge_relations import canonical_edge
from server.memory.domain.graph_quality import canonical_entities
from server.memory.domain.provenance import class_from_trace
from server.memory.domain.sensitivity import ATTRIBUTE_TAG_PREFIX, SENSITIVITY_TAG_PREFIX, is_high
from server.memory.domain.trust import actor_for

_PAGE = 1000
_BULK = 500
# Listings of a namespace that keeps changing before the export gives up calling it complete.
_LIST_ATTEMPTS = 3  # not a setting: a CLI's consistency retry while a namespace changes
# Every concept row the import writes carries this source_trace prefix; --prune deletes
# nothing else.
_TRACE_PREFIX = "okf:"
# Tags memory.set adds itself (sensitivity tier, attribute labels); a live row carrying one
# the bundle lacks is not a change, since the next write adds it back.
_SERVER_TAGS = (SENSITIVITY_TAG_PREFIX, ATTRIBUTE_TAG_PREFIX)
_LIVE_FIELDS = [
    "session_id",
    "memory_type",
    "confidence",
    "source_trace",
    "valid_at",
    "tags",
    "verification",
    "expires_at",
    # prune deletes only the version this listing saw with the import's trace
    "version",
]
# What memory.list does not return, or blanks for a high row (its hash and entities are
# served by key only); read with memory.bulk_get.
_BULK_FIELDS = ["valid_to", "source_class", "content_sha256", "entities"]


def _resolve_token(args: argparse.Namespace) -> str:
    token, source = resolve_token(args.token)
    if refused_for_agent(source):
        sys.exit(2)
    if not token:
        print(
            "No token: pass --token, set $LOOM_API_KEY, or write one to ~/.loom/token",
            file=sys.stderr,
        )
        sys.exit(2)
    return token


def _resolve_org(args: argparse.Namespace, token: str) -> str:
    org = args.org or str(jwt_claims(token).get("org") or "")
    if not org:
        print("Could not infer org from the token; pass --org.", file=sys.stderr)
        sys.exit(2)
    return org


class _Client:
    def __init__(self, base_url: str, token: str) -> None:
        import httpx

        self._c = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=60.0,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        """The op's JSON object. Every failure is a RuntimeError — no answer, a
        non-200 (named by its error envelope, never the raw body, which can
        quote the concept's content back), a body that is not JSON — so an
        import counts it against the one concept, link or batch and goes on."""
        import httpx

        try:
            r = await self._c.post(f"/v1/{op}", json=body)
        except httpx.HTTPError as e:
            raise RuntimeError(f"{op} → no answer ({type(e).__name__}: {e})") from e
        if r.status_code != 200:
            raise RuntimeError(f"{op} → {error_line(r.status_code, r.reason_phrase, r.content)}")
        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError(f"{op} → HTTP 200, not a JSON answer") from e
        return data if isinstance(data, dict) else {}

    async def close(self) -> None:
        await self._c.aclose()

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# ------------------------------------------------------------------
# export
# ------------------------------------------------------------------


async def _fetch_namespace(
    client: _Client,
    org: str,
    namespace: str,
    *,
    include_tombstoned: bool,
    include_high: bool = False,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[str], str, bool]:
    """Every live row of `namespace` (all agents, users and sessions), full
    values, as memory_to_concept dicts; the keys left out because bulk_get did not
    answer them; why the rows are not known to be every row ('' when they are); and
    whether the listing stopped at `limit`, which caps the rows for smoke runs."""
    # Offset pages shift when the namespace changes mid-listing, which can skip a live row;
    # a bundle that says complete without it would let --prune delete its copy. An export
    # that stopped at --limit, or keeps deleted rows, is never complete, so it is listed once.
    for _ in range(_LIST_ATTEMPTS):
        entries, capped = await _list_namespace(
            client, org, namespace, include_tombstoned=include_tombstoned, limit=limit
        )
        if capped or include_tombstoned:
            why = ""
            break
        why = await _not_listed(client, org, namespace, entries)
        if why != _SHIFTED:
            break
    rows, unreadable = await _with_full_values(
        client,
        org,
        namespace,
        entries,
        include_tombstoned=include_tombstoned,
        include_high=include_high,
    )
    if unreadable and not why:
        # Its absence says nothing about the rows around it.
        why = "a listed row was deleted before its full read"
    return rows, unreadable, why, capped


_SHIFTED = "the source changed while it was read (a row was written while it was listed)"


async def _list_namespace(
    client: _Client,
    org: str,
    namespace: str,
    *,
    include_tombstoned: bool,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """The `memory.list` entries of every row, and whether the listing stopped at `limit`."""
    query: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        # "*" drops the predicate on each identity column (memory.list
        # otherwise scopes to one writer identity, default '-').
        "agent": "*",
        "user_id": "*",
        "session_id": "*",
        "sort_by": "recency",
        "include_tombstoned": include_tombstoned,
        # memory.list hides superseded and duplicate rows by default; the
        # bundle renders them as `status: deprecated`, and a bundle without
        # them would let --prune delete their copies.
        "include_superseded": True,
        "include_duplicates": True,
    }
    # A row a shifted page served twice counts once, as the pages arrive: a repeat must not
    # count toward --limit.
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    offset = 0
    while True:
        page_size = _PAGE if limit is None else max(1, min(_PAGE, limit - len(seen)))
        page = await client.post("memory.list", {**query, "limit": page_size, "offset": offset})
        rows = page.get("entries") or []
        for r in rows:
            seen.setdefault(_entry_id(r), r)
        if not page.get("truncated") or not rows:
            return list(seen.values()), False
        # Stopped at the limit only when the server said more rows remain: a namespace of
        # exactly `limit` rows was read whole.
        if limit is not None and len(seen) >= limit:
            return list(seen.values())[:limit], True
        offset += len(rows)


async def _not_listed(
    client: _Client, org: str, namespace: str, entries: list[dict[str, Any]]
) -> str:
    """Why `entries` may miss a live row, or '' when every row memory.keys reads (one
    statement, so the namespace at one moment) is among them. A row written after that
    moment is a later change, which the next export carries."""
    try:
        got = await client.post("memory.keys", {"org": org, "namespace": namespace})
    except RuntimeError as e:
        return f"the source did not answer memory.keys ({e})"
    keys = got.get("keys") or []
    if got.get("truncated"):
        return f"the source holds more live rows than one memory.keys answer ({len(keys)})"
    listed = {_entry_id(r) for r in entries}
    for k in keys:
        if (
            k.get("agent"),
            k.get("user_id"),
            k.get("session_id"),
            k.get("memory_key"),
        ) not in listed:
            return _SHIFTED
    return ""


def _entry_id(r: dict[str, Any]) -> tuple[Any, ...]:
    return (r.get("agent"), r.get("user_id"), r.get("session_id"), r.get("memory_key"))


async def _with_full_values(
    client: _Client,
    org: str,
    namespace: str,
    entries: list[dict[str, Any]],
    *,
    include_tombstoned: bool,
    include_high: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The entries with their full values, and the keys left out because `memory.bulk_get`
    did not answer them: deleted since the listing, or, with `include_tombstoned`, a deleted
    row whose list preview is a cut value. A deleted row the list carries in full keeps it."""
    # Full values (list previews are capped) grouped by the writer identity,
    # since bulk_get resolves keys within one identity tuple.
    by_ident: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in entries:
        ident = (r.get("agent") or "*", r.get("user_id") or "-", r.get("session_id") or "-")
        by_ident.setdefault(ident, []).append(r)

    out: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for (agent, user_id, session_id), rows in by_ident.items():
        # A row the list already calls high stays home, so its value is never read.
        held = {
            r["memory_key"]
            for r in rows
            if not include_high and is_high(r.get("sensitivity"), r.get("tags"))
        }
        keys = [r["memory_key"] for r in rows if r["memory_key"] not in held]
        full: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), _BULK):
            got = await client.post(
                "memory.bulk_get",
                {
                    "org": org,
                    "namespace": namespace,
                    "agent": agent or "*",
                    "user_id": user_id,
                    "session_id": session_id,
                    "memory_keys": keys[i : i + _BULK],
                },
            )
            full.update(got.get("results") or {})
        for r in rows:
            g = full.get(r["memory_key"])
            if g is not None and _answered_by_parent(r["memory_key"], g, r.get("version")):
                # A key shaped like a chunk id that bulk_get answered with its parent's row
                # (no row held the key when it read): that is not this row's value.
                g = None
            if g is None and r["memory_key"] not in held and _past_retention(r):
                # bulk_get never serves a high row past its retention window; the list still
                # does until the expiry sweep. It has left the source, as the bundle says.
                continue
            kept_deleted = include_tombstoned and r.get("tombstoned_at")
            if (
                g is None
                and r["memory_key"] not in held
                and not (kept_deleted and not r.get("value_truncated"))
            ):
                # Deleted since the listing, or a deleted row the list does not carry in full
                # (a cut preview, or none for a high row).
                unreadable.append(r["memory_key"])
                continue
            merged = {**r, **{k: v for k, v in (g or {}).items() if v not in (None, "", [], {})}}
            if g is not None:
                # bulk_get's answer, kept even when empty, unless the list says high: during
                # a rolling upgrade the two reads can reach builds that disagree.
                high = is_high(r.get("sensitivity"), r.get("tags"))
                merged["sensitivity"] = "high" if high else g.get("sensitivity")
            merged.setdefault("namespace", namespace)
            merged["agent"] = agent if agent != "*" else (r.get("agent") or "")
            out.append(merged)
    return out, unreadable


def _past_retention(r: dict[str, Any]) -> bool:
    """A high row whose `expires_at` has passed: memory.bulk_get no longer serves it."""
    expires = _when(r.get("expires_at"))
    if not isinstance(expires, datetime) or not is_high(r.get("sensitivity"), r.get("tags")):
        return False
    return expires <= _now()


def _answered_by_parent(
    memory_key: str, answer: dict[str, Any], listed_version: int | None
) -> bool:
    """Whether bulk_get answered `memory_key` with another row: a chunk-id-shaped key
    (`chunk:memory.set:<parent>:<n>`) that no row holds reads its parent. This build marks
    that answer with `resolved_from`, and a duplicate row's own answer with `duplicate_of`,
    which an older build's bulk_get never filled. An older build marks a parent's answer only
    by `canonical_key` (a duplicate parent's own canonical), so a chunk-shaped key answered
    with a canonical key and another version than the listed row is taken as another row's
    too."""
    if answer.get("resolved_from"):
        return True
    if answer.get("duplicate_of"):
        return False  # the row's own answer: it is a duplicate
    rest = memory_key.removeprefix("chunk:memory.set:")
    parent, _, index = rest.rpartition(":")
    if rest == memory_key or not index.isdigit():
        return False
    version = answer.get("version")
    other_version = listed_version is not None and version not in (None, listed_version)
    canonical = answer.get("canonical_key")
    return canonical == parent or (bool(canonical) and other_version)


async def _fetch_evidence(
    client: _Client, org: str, namespace: str, *, include_high: bool, page: int = 500
) -> tuple[list[okf_ev.ChunkRecord], list[okf_ev.DigestRecord], int]:
    """Every live raw chunk (with its memory_chunk_sources links) and session
    digest of the namespace, through memory.export_evidence, and how many the
    server held back as 'high'."""
    chunks: list[okf_ev.ChunkRecord] = []
    held = 0
    cursor = ""
    while True:
        resp = await client.post(
            "memory.export_evidence",
            {
                "org": org,
                "namespace": namespace,
                "kind": "chunks",
                "limit": page,
                "cursor": cursor,
                "include_high": include_high,
            },
        )
        held += int(resp.get("held_high") or 0)
        for c in resp.get("chunks") or []:
            chunks.append(
                okf_ev.ChunkRecord(
                    session_id=str(c.get("session_id") or ""),
                    chunk_index=int(c.get("chunk_index") or 0),
                    content=str(c.get("content") or ""),
                    content_sha256=str(c.get("content_sha256") or ""),
                    agent=str(c.get("agent") or ""),
                    parent_memory_key=str(c.get("parent_memory_key") or ""),
                    memory_keys=[str(k) for k in (c.get("memory_keys") or [])],
                    line_start=int(c.get("line_start") or 0),
                    line_end=int(c.get("line_end") or 0),
                    created_at=str(c.get("created_at") or ""),
                    source_url=str(c.get("source_url") or ""),
                )
            )
        cursor = str(resp.get("next_cursor") or "")
        if not resp.get("has_more") or not cursor:
            break
    resp = await client.post(
        "memory.export_evidence",
        {
            "org": org,
            "namespace": namespace,
            "kind": "digests",
            "limit": 2000,
            "include_high": include_high,
        },
    )
    held += int(resp.get("held_high") or 0)
    digests = [
        okf_ev.DigestRecord(
            session_id=str(d.get("session_id") or ""),
            summary_text=str(d.get("summary_text") or ""),
            agent=str(d.get("agent") or "-"),
            user_id=str(d.get("user_id") or "-"),
            key_facts=[str(k) for k in (d.get("key_facts") or [])],
            derived_from=[str(k) for k in (d.get("derived_from") or [])],
            valid_at=str(d.get("valid_at") or ""),
            created_at=str(d.get("created_at") or ""),
        )
        for d in (resp.get("digests") or [])
    ]
    return chunks, digests, held


async def _fetch_edges(
    client: _Client, org: str, namespace: str, *, page: int = 2000
) -> list[dict[str, Any]]:
    """Every live typed edge of the namespace, paged by the
    table key through memory.export_evidence kind=edges."""
    edges: list[dict[str, Any]] = []
    cursor = ""
    while True:
        resp = await client.post(
            "memory.export_evidence",
            {
                "org": org,
                "namespace": namespace,
                "kind": "edges",
                "limit": page,
                "cursor": cursor,
            },
        )
        edges.extend(dict(e) for e in (resp.get("edges") or []))
        cursor = str(resp.get("next_cursor") or "")
        if not resp.get("has_more") or not cursor:
            return edges


def _server_build(server_url: str) -> str:
    """The exporting server's `build_commit` from /v1/health, '' when unreachable;
    the generation pointer records which build rendered."""
    try:
        import httpx

        r = httpx.get(f"{server_url.rstrip('/')}/v1/health", timeout=5.0)
        data = r.json() if r.status_code == 200 else {}
        return str(data.get("build_commit") or "") if isinstance(data, dict) else ""
    except Exception:  # noqa: BLE001 — the pointer is informational
        return ""


def _plan_export(
    entries: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    chunks: list[okf_ev.ChunkRecord],
    digests: list[okf_ev.DigestRecord],
    *,
    namespace: str,
    title: str | None,
    include_high: bool,
    server_url: str = "",
    complete: bool,
) -> tuple[dict[str, str], list[str], int]:
    """The bundle's files, its orphan concepts, and how many `high` rows stayed
    home. Digests link to the concepts they compressed."""
    kept = [e for e in entries if okf.is_exportable(e, include_high=include_high)]
    files = okf.plan_bundle(
        kept,
        namespace=namespace,
        exported_at=datetime.now(UTC),
        title=title,
        edges=edges,
        build_commit=_server_build(server_url),
        complete=complete,
    )
    orphans = okf.bundle_orphans(files)  # concepts only: evidence files are not concepts
    if chunks or digests:
        paths = okf.concept_paths_by_key(okf.plan_concepts(kept, edges))
        files.update(okf_ev.plan_files(chunks, digests, paths))
        files["log.md"] = files.get("log.md", "") + (
            f"* **Evidence**: {len(chunks)} raw chunks (`evidence/`) and {len(digests)} "
            f"session digests (`digests/`) travel with the concepts; the importing server "
            f"re-embeds them.\n"
        )
    return files, orphans, len(entries) - len(kept)


def _write_bundle(out_dir: Path, files: dict[str, str]) -> tuple[int, int]:
    created = updated = 0
    for rel, text in files.items():
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            if p.read_text(encoding="utf-8") == text:
                continue
            updated += 1
        else:
            created += 1
        p.write_text(text, encoding="utf-8")
    return created, updated


def _cmd_export_okf(args: argparse.Namespace) -> int:
    token = _resolve_token(args)
    org = _resolve_org(args, token)
    out_dir = Path(args.out)

    async def _go() -> int:
        chunks: list[okf_ev.ChunkRecord] = []
        digests: list[okf_ev.DigestRecord] = []
        edges: list[dict[str, Any]] = []
        held_evidence = 0
        async with _Client(args.server_url, token) as client:
            entries, unreadable, why, capped = await _fetch_namespace(
                client,
                org,
                args.namespace,
                include_tombstoned=bool(args.include_tombstoned),
                include_high=bool(args.include_high),
                limit=args.limit,
            )
            if not args.no_evidence:
                chunks, digests, held_evidence = await _fetch_evidence(
                    client, org, args.namespace, include_high=bool(args.include_high)
                )
            if not args.no_links:
                edges = await _fetch_edges(client, org, args.namespace)
        if not args.include_high:
            # The rows this export held home, which the server's rule may not cover (a level
            # it does not know, or a server that holds nothing back).
            held = {e["memory_key"] for e in entries if not okf.is_exportable(e)}
            held.update(unreadable)  # left out, so their evidence is too
            chunks, digests, held_here = okf_ev.without_high(chunks, digests, held)
            held_evidence += held_here
        files, orphans, skipped_high = _plan_export(
            entries,
            edges,
            chunks,
            digests,
            namespace=args.namespace,
            title=args.title,
            include_high=bool(args.include_high),
            server_url=args.server_url,
            # A bundle that keeps deleted rows says nothing about which keys left the source.
            complete=not why and not capped and not args.include_tombstoned,
        )
        n_concepts = len(entries) - skipped_high
        evidence_note = (
            f", {len(chunks)} raw chunks, {len(digests)} session digests"
            if not args.no_evidence
            else ""
        )
        edge_note = "" if args.no_links else f", {len(edges)} typed edges read"
        if args.dry_run:
            print(
                f"would write {len(files)} files ({n_concepts} concepts{evidence_note}"
                f"{edge_note}) to {out_dir}"
            )
            for rel in sorted(files):
                print(f"  {rel}")
        else:
            created, updated = _write_bundle(out_dir, files)
            print(
                f"wrote {n_concepts} concepts{evidence_note}{edge_note} to {out_dir} "
                f"({created} new files, {updated} changed, "
                f"{len(files) - created - updated} unchanged)"
            )
        if skipped_high:
            print(
                f"  {skipped_high} row(s) with sensitivity=high, or no known sensitivity, "
                f"stayed in the database (--include-high exports them)"
            )
        if held_evidence:
            print(
                f"  {held_evidence} raw chunk(s) or session digest(s) stayed in the database: "
                "linked to a row kept home, or holding a high shape "
                "(--include-high exports them)"
            )
        if unreadable:
            print(
                f"  {len(unreadable)} row(s) left out: bulk_get did not answer them (deleted "
                "since the list, a high row past its retention window, or a deleted row whose "
                "full value the list does not carry): "
                + ", ".join(unreadable[:5])
                + (" …" if len(unreadable) > 5 else "")
            )
        if args.include_tombstoned:
            print(
                "  not marked complete: --include-tombstoned keeps deleted rows, so it never prunes"
            )
        elif capped:
            print(f"  not marked complete: it stopped at --limit {args.limit}")
        elif why:
            print(
                f"  not marked complete: {why}; an import with --prune writes it and skips "
                "the prune"
            )
        if orphans:
            print(
                f"  {len(orphans)} concept(s) carry no edge and share no entity with any "
                f"other (`okf validate --strict` will list them as orphans): "
                + ", ".join(orphans[:5])
                + (" …" if len(orphans) > 5 else "")
            )
        print("  validate with: okf validate", out_dir, "--drift")
        return 0

    return asyncio.run(_go())


# ------------------------------------------------------------------
# import
# ------------------------------------------------------------------


def _walk_bundle(files: dict[str, str]) -> list[okf.Concept]:
    """The concepts among a bundle's files, as `okf_lint.read_bundle` read them."""
    concepts: list[okf.Concept] = []
    for rel in sorted(files):
        parts = rel.split("/")
        if any(part.startswith(".") or part == "node_modules" for part in parts):
            continue
        if parts[-1] in okf.RESERVED_FILES:
            continue
        if okf_ev.is_evidence_path(rel):
            continue  # raw chunks and digests: `_walk_evidence`, not concepts
        c = okf.parse_concept(rel, files[rel])
        if not c.type:
            print(f"  skip {rel}: no `type` in frontmatter (not an OKF concept)", file=sys.stderr)
            continue
        concepts.append(c)
    return concepts


def _walk_evidence(root: Path) -> tuple[list[okf_ev.ChunkRecord], list[okf_ev.DigestRecord]]:
    files: dict[str, str] = {}
    for d in okf_ev.EVIDENCE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            if p.name in okf.RESERVED_FILES:
                continue
            files[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8")
    return okf_ev.parse_files(files)


def _session_batches(chunks: list[okf_ev.ChunkRecord]) -> list[list[okf_ev.ChunkRecord]]:
    """The chunks in calls of at most `okf_ev.IMPORT_MAX_ITEMS`, each session's chunks
    whole and in `chunk_index` order. The server finds a key cut at a chunk boundary
    only when both chunks arrive in one call, so only a session longer than the cap is
    split."""
    sessions: dict[str, list[okf_ev.ChunkRecord]] = {}
    for c in chunks:
        sessions.setdefault(c.session_id, []).append(c)
    batches: list[list[okf_ev.ChunkRecord]] = []
    current: list[okf_ev.ChunkRecord] = []
    for session in sessions.values():
        session.sort(key=lambda c: c.chunk_index)
        if current and len(current) + len(session) > okf_ev.IMPORT_MAX_ITEMS:
            batches.append(current)
            current = []
        while len(session) > okf_ev.IMPORT_MAX_ITEMS:
            batches.append(session[: okf_ev.IMPORT_MAX_ITEMS])
            session = session[okf_ev.IMPORT_MAX_ITEMS :]
        current += session
    if current:
        batches.append(current)
    return batches


async def _import_evidence(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    chunks: list[okf_ev.ChunkRecord],
    digests: list[okf_ev.DigestRecord],
) -> dict[str, int]:
    """memory.import_evidence in batches; the server re-embeds."""
    totals = {
        "chunks_written": 0,
        "links_written": 0,
        "digests_written": 0,
        "edited": 0,
        "skipped": 0,
        "failed": 0,
    }

    def _chunk(c: okf_ev.ChunkRecord) -> dict[str, Any]:
        return {
            "session_id": c.session_id,
            "chunk_index": c.chunk_index,
            "content": c.content,
            "content_sha256": c.content_sha256,
            "agent": c.agent,
            "parent_memory_key": c.parent_memory_key,
            "memory_keys": c.memory_keys,
            "line_start": c.line_start,
            "line_end": c.line_end,
            "created_at": c.created_at,
            "source_url": c.source_url,
        }

    def _digest(d: okf_ev.DigestRecord) -> dict[str, Any]:
        return {
            "session_id": d.session_id,
            "summary_text": d.summary_text,
            "agent": d.agent,
            "user_id": d.user_id,
            "key_facts": d.key_facts,
            "derived_from": d.derived_from,
            "valid_at": d.valid_at,
            "created_at": d.created_at,
        }

    payload_base = {
        "org": org,
        "namespace": args.namespace,
        "agent": args.agent,
        "user_id": args.user_id,
        "source_trace": "okf-import",
    }
    batches: list[dict[str, Any]] = []
    for group in _session_batches(chunks):
        batches.append({**payload_base, "chunks": [_chunk(c) for c in group]})
    for i in range(0, len(digests), okf_ev.IMPORT_MAX_ITEMS):
        digest_group = digests[i : i + okf_ev.IMPORT_MAX_ITEMS]
        batches.append({**payload_base, "digests": [_digest(d) for d in digest_group]})
    for body in batches:
        try:
            res = await client.post("memory.import_evidence", body)
        except RuntimeError as e:
            totals["failed"] += len(body.get("chunks") or []) + len(body.get("digests") or [])
            print(f"  ✗ import_evidence batch: {e}", file=sys.stderr)
            continue
        for k in ("chunks_written", "links_written", "digests_written", "skipped"):
            totals[k] += int(res.get(k) or 0)
        totals["edited"] += len(res.get("edited") or [])
    return totals


def _lint_before_import(root: Path, args: argparse.Namespace) -> int | None:
    """A bundle is linted before it is written. A critical
    finding refuses the import (exit 2) unless `--force-import`; warnings are
    printed and the import proceeds. `--no-lint` skips the gate."""
    if getattr(args, "no_lint", False):
        return None
    rep = okf_lint.lint_bundle(root)
    if rep.criticals or rep.warnings:
        print(rep.render(), file=sys.stderr)
    if rep.criticals and not getattr(args, "force_import", False):
        print(
            f"refusing to import: {len(rep.criticals)} critical lint finding(s); "
            "fix the bundle or pass --force-import",
            file=sys.stderr,
        )
        return 2
    return None


def _concept_payload(c: okf.Concept, org: str, args: argparse.Namespace) -> dict[str, Any]:
    """The memory.set body for one concept, identity included."""
    payload = okf.concept_to_memory(c, target_org=org, target_namespace=args.namespace)
    bundle_actor = okf.human_verified_actor(c)
    if bundle_actor:
        payload["tags"] = [*payload["tags"], f"okf:verified_by:{bundle_actor}"]
    session_id = args.session_id
    if args.keep_sessions:
        session_id = okf.concept_session_id(c) or args.session_id
    payload.update(
        {
            "org": org,
            "namespace": args.namespace,
            "agent": args.agent,
            "user_id": args.user_id,
            "session_id": session_id,
            # The path inside the bundle, not where the bundle sits on disk: a run that
            # exports to a new directory must not look like a change to every row.
            "source_trace": f"{_TRACE_PREFIX}{c.path}",
        }
    )
    return payload


async def _live_rows(
    client: _Client, org: str, args: argparse.Namespace
) -> dict[tuple[str, str], dict[str, Any]]:
    """The live rows under the import's identity in the target namespace, by
    (session_id, memory_key), with every field memory.set writes that a read returns."""
    ident = {"org": org, "namespace": args.namespace, "agent": args.agent, "user_id": args.user_id}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    now = _now()
    offset = 0
    while True:
        page = await client.post(
            "memory.list",
            {
                **ident,
                "session_id": "*" if args.keep_sessions else args.session_id,
                "limit": _PAGE,
                "offset": offset,
                "sort_by": "recency",
                # memory.set overwrites these rows too, so they count as live.
                "include_duplicates": True,
                "include_superseded": True,
                "fields": _LIVE_FIELDS,
            },
        )
        entries = page.get("entries") or []
        for r in entries:
            expires = _when(r.get("expires_at"))
            if isinstance(expires, datetime) and expires <= now:
                continue  # memory.get and search already hide it, so write it again
            rows[(str(r.get("session_id") or "-"), str(r["memory_key"]))] = r
        if not page.get("truncated") or not entries:
            break
        offset += len(entries)

    by_session: dict[str, list[str]] = {}
    for sid, key in rows:
        by_session.setdefault(sid, []).append(key)
    for sid, keys in by_session.items():
        for i in range(0, len(keys), _BULK):
            got = await client.post(
                "memory.bulk_get",
                {
                    **ident,
                    "session_id": sid,
                    "memory_keys": keys[i : i + _BULK],
                    "fields": _BULK_FIELDS,
                },
            )
            for key, g in (got.get("results") or {}).items():
                if g.get("resolved_from"):
                    continue  # another row answered (the listed one is gone): not its fields
                rows[(sid, key)].update({f: g.get(f) for f in _BULK_FIELDS})
    return rows


def _when(v: Any) -> datetime | str | None:
    """A bundle or wire timestamp as naive UTC whole seconds (the column's precision)."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return str(v)  # never equal to a parsed time, so the server sees and judges it
    return to_naive_utc(d).replace(microsecond=0)


def _one_per_row(
    concepts: list[okf.Concept], org: str, args: argparse.Namespace
) -> list[tuple[okf.Concept, dict[str, Any]]]:
    """The concepts with their payloads, one per target row. An export holds a key once
    per writer, but the import writes every concept under its own identity: of the
    concepts that land on one row, the most recently updated is written, so a run does
    not alternate between them."""
    newest: dict[tuple[str, str], tuple[okf.Concept, dict[str, Any]]] = {}
    for c in concepts:
        p = _concept_payload(c, org, args)
        k = (p["session_id"], p["memory_key"])
        if k not in newest or _updated_at(c) > _updated_at(newest[k][0]):
            newest[k] = (c, p)
    if len(newest) < len(concepts):
        print(
            f"  ! {len(concepts) - len(newest)} concept(s) share a key with a newer one "
            f"in the bundle; only the newest is written",
            file=sys.stderr,
        )
    return list(newest.values())


def _updated_at(c: okf.Concept) -> str:
    return str(c.loom.get("updated_at") or "")


def _unchanged(payload: dict[str, Any], row: dict[str, Any]) -> bool:
    """True when the live row already holds everything memory.set would write.
    `content_sha256` covers the value only, so the metadata is compared too."""
    if payload.get("source_resource_id"):
        # memory.list does not return source_resource_id, and naming it in bulk_get's
        # `fields` is a 422 from a server that predates the field.
        return False
    return (
        row.get("content_sha256") == sha256_value(payload["value"])
        and row.get("memory_type") == payload["memory_type"]
        and row.get("source_trace") == payload["source_trace"]
        and _comparable_tags(row.get("tags") or [], payload["tags"]) == set(payload["tags"])
        and math.isclose(float(row.get("confidence") or 0), payload["confidence"], abs_tol=1e-6)
        # A bundle without entities leaves them to the target's extractor.
        and (
            "entities" not in payload
            or (row.get("entities") or []) == canonical_entities(payload["entities"])
        )
        and _when(row.get("valid_at")) == _when(payload.get("valid_at"))
        and _when(row.get("valid_to")) == _when(payload.get("valid_to"))
        and _stored_class_matches(row, payload)
    )


def _stored_class_matches(row: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Whether the live row carries the class this write would store: the declared one, or,
    without one, what the server derives from the `okf:` trace (`imported`). A source row
    that stops declaring `derived` must not leave its copy derived."""
    expected = payload.get("source_class") or class_from_trace(
        str(payload.get("source_trace") or "")
    )
    return not expected or row.get("source_class") == expected


def _comparable_tags(row_tags: list[str], payload_tags: list[str]) -> set[str]:
    """The row's tags without those memory.set added that the payload does not carry."""
    return {t for t in row_tags if t in payload_tags or not t.startswith(_SERVER_TAGS)}


async def _import_concepts(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    planned: list[tuple[okf.Concept, dict[str, Any]]],
    live: dict[tuple[str, str], dict[str, Any]],
) -> Counter[str]:
    """memory.set per changed or new concept (conflict detection, blacklist and audit
    apply); `--verify` re-attests human-verified concepts as the caller."""
    n: Counter[str] = Counter()
    for c, payload in planned:
        bundle_actor = okf.human_verified_actor(c)
        row = live.get((payload["session_id"], payload["memory_key"]))
        if row is not None and _unchanged(payload, row):
            n["unchanged"] += 1
            # A skipped row is re-attested when no attestation binds its current content.
            unattested = row.get("verification") in ("none", "stale")
            if args.verify and bundle_actor and not args.dry_run and unattested:
                n[await _verify(client, org, args, payload, c)] += 1
            continue
        if args.dry_run:
            note = (
                f" verify-as-caller (bundle: {bundle_actor})"
                if bundle_actor and args.verify
                else ""
            )
            cls = f" source_class={payload['source_class']}" if payload.get("source_class") else ""
            print(
                f"  {c.path} → {payload['memory_type']}:{payload['memory_key']} "
                f"tags={payload['tags']}{cls}{note}"
            )
            n["written"] += 1
            continue
        try:
            res = await client.post("memory.set", payload)
        except RuntimeError as e:
            n["failed"] += 1
            print(f"  ✗ {c.path}: {e}", file=sys.stderr)
            continue
        verdict = res.get("conflict_verdict")
        if verdict == "merge":
            # Folded into another key's row, so nothing lands at this key and the next run
            # sends it again.
            n["merged"] += 1
            continue
        n["written"] += 1
        # The judge runs after the write unless asked to wait: its conflicts arrive as pending.
        if verdict in ("conflict", "pending"):
            n[verdict] += 1
        if args.verify and bundle_actor:
            n[await _verify(client, org, args, payload, c)] += 1
    return n


async def _verify(
    client: _Client, org: str, args: argparse.Namespace, payload: dict[str, Any], c: okf.Concept
) -> str:
    """memory.verify as the caller: the summary count it adds to."""
    try:
        v = await client.post(
            "memory.verify",
            {
                "org": org,
                "namespace": args.namespace,
                "agent": args.agent,
                "user_id": args.user_id,
                "session_id": payload["session_id"],
                "memory_key": payload["memory_key"],
            },
        )
    except RuntimeError as e:
        print(f"  ✗ {c.path}: verify failed: {e}", file=sys.stderr)
        return "verify_failed"
    return "verified" if v.get("found") else "verify_missed"


async def _import_links(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    links: list[dict[str, str]],
    unresolved: list[dict[str, str]],
    live_edges: set[tuple[str, str, str]],
) -> Counter[str]:
    """Re-create the bundle's `relates_to` edges that are not live yet with memory.link; the
    author is whoever the token names. Entries that name no concept in the bundle (a
    `loom://` pointer, a file that did not travel) are reported, never written."""
    for u in unresolved[:10]:
        print(
            f"  ! {u['path']}: relates_to {u['kind']} → {u['target']} is not in the bundle; "
            f"not linked",
            file=sys.stderr,
        )
    if len(unresolved) > 10:
        print(f"  ! … {len(unresolved) - 10} more unresolved relates_to entries", file=sys.stderr)
    n: Counter[str] = Counter(links_unresolved=len(unresolved))
    # One key exported for several writers carries its edges once per concept.
    wanted = {_edge_key(e): e for e in links}
    new = [e for k, e in wanted.items() if k not in live_edges]
    n["links_unchanged"] = len(wanted) - len(new)
    if args.dry_run:
        n["links"] = len(new)
        return n
    for e in new:
        try:
            await client.post(
                "memory.link",
                {
                    "org": org,
                    "namespace": args.namespace,
                    "from_key": e["from_key"],
                    "to_key": e["to_key"],
                    "relation": e["relation"],
                    "explanation": f"okf bundle: {e['path']}",
                },
            )
            n["links"] += 1
        except RuntimeError as err:
            n["links_failed"] += 1
            print(f"  ✗ link {e['from_key']} {e['relation']} {e['to_key']}: {err}", file=sys.stderr)
    return n


def _edge_key(e: dict[str, str]) -> tuple[str, str, str]:
    """(from_key, to_key, relation) as memory.link stores the edge."""
    edge = canonical_edge(e["from_key"], e["to_key"], e["relation"])
    return edge.from_key, edge.to_key, edge.relation


def _prune_refusal(files: dict[str, str], concepts: list[okf.Concept]) -> str:
    """Why this bundle must not be imported with --prune at all, or ''. An empty bundle or
    one whose files no longer hash to `loom.corpus_digest` is not what the export wrote."""
    if not concepts:
        return "the bundle has no concepts"
    declared = str(okf_lint.generation_pointer(files).get("corpus_digest") or "")
    if not declared:
        return "index.md carries no loom.corpus_digest, so this is not a complete export"
    if declared != okf_lint.bundle_digest(files):
        return (
            "the concept files do not match index.md's loom.corpus_digest: a partial or "
            "edited export, or one written into a directory that held an older bundle"
        )
    return ""


def _marked_complete(files: dict[str, str]) -> bool:
    """Whether the export read every row: only then does the bundle say which keys have
    left the source."""
    return okf_lint.generation_pointer(files).get("complete") is True


_PRUNE_SKIPPED = (
    "not pruning this run: index.md does not mark the export complete (it stopped at "
    "--limit, ran with --include-tombstoned, could not tell it read every row, or an "
    "older release wrote it); "
    "the writes go ahead"
)


# What prune reads of a candidate before it deletes it.
_PRUNE_FIELDS = ["agent", "actor", "version", "lock_mode"]


async def _prune(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    live: dict[tuple[str, str], dict[str, Any]],
    keep: set[tuple[str, str]],
    actor: str,
) -> Counter[str]:
    """Delete each row an earlier import wrote whose key has left the bundle. Only rows
    under the import's identity that this token wrote and nobody locked, still at the
    version the listing saw with the import's source_trace, qualify. memory.delete_version
    removes exactly that version: a row rewritten since is kept, and an older server,
    which does not know the op, deletes nothing."""
    gone = sorted(
        k
        for k, r in live.items()
        if k not in keep and str(r.get("source_trace") or "").startswith(_TRACE_PREFIX)
    )
    n: Counter[str] = Counter()
    for session_id, key in gone:
        who = {
            "org": org,
            "namespace": args.namespace,
            "agent": args.agent,
            "user_id": args.user_id,
            "session_id": session_id,
        }
        try:
            # memory.bulk_get reads the row stored under the key itself, a key shaped like a
            # search chunk's id too (memory.get reads those from the chunk table).
            answer = await client.post(
                "memory.bulk_get", {**who, "memory_keys": [key], "fields": _PRUNE_FIELDS}
            )
            got = (answer.get("results") or {}).get(key) or {}
            # Gone, or answered by a chunk's parent fact rather than a row under the key.
            if not got or got.get("resolved_from") or got.get("agent") != args.agent:
                continue
            listed = live[(session_id, key)].get("version")
            # Another writer's row, a copy someone locked to keep it, or one rewritten since
            # the listing that saw its import trace.
            if (
                got.get("actor") != actor
                or got.get("lock_mode")
                or got.get("version") is None
                or str(got["version"]) != str(listed)
            ):
                n["prune_kept"] += 1
                continue
            if args.dry_run:
                where = f" (session {session_id})" if session_id != "-" else ""
                print(f"  would delete {key}{where}")
                n["pruned"] += 1
                continue
            res = await client.post(
                "memory.delete_version", {**who, "memory_key": key, "version": str(listed)}
            )
        except RuntimeError as e:
            n["prune_failed"] += 1
            print(f"  ✗ prune {key}: {e}", file=sys.stderr)
            continue
        if res.get("tombstoned"):
            n["pruned"] += 1
        else:
            # Rewritten, or gone, since prune's read: nothing of ours was deleted.
            n["prune_kept"] += 1
    return n


def _print_import_summary(
    n: Counter[str],
    args: argparse.Namespace,
    org: str,
    rows: int,
    chunks: int,
    digests: int,
    *,
    prune_skipped: bool,
) -> None:
    would = " would be" if args.dry_run else ""
    if args.dry_run:
        head = (
            f"dry run: {n['written']} of {rows} rows ({n['unchanged']} unchanged), "
            f"{chunks} raw chunks and {digests} session digests would be "
            f"written to {org}/{args.namespace}"
        )
    else:
        head = (
            f"{rows} rows from the bundle into {org}/{args.namespace}: "
            f"{n['written']} written, {n['unchanged']} unchanged, {n['failed']} failed"
        )
        if n["merged"]:
            head += f", {n['merged']} merged into another key"
        head += (
            f" ({n['conflict']} flagged conflicted, {n['pending']} awaiting a conflict verdict, "
            f"{n['verified']} re-attested as caller"
        )
        if n["verify_missed"]:
            head += f", {n['verify_missed']} not found to re-attest"
        if n["verify_failed"]:
            head += f", {n['verify_failed']} failed to re-attest"
        head += ")"
    parts = [head]
    if not args.no_links:
        notes = [f"{n['links_unchanged']} unchanged"]
        if n["links_unresolved"]:
            notes.append(f"{n['links_unresolved']} unresolved")
        if n["links_failed"]:
            notes.append(f"{n['links_failed']} failed")
        parts.append(f"{n['links']} typed edges{would} linked ({', '.join(notes)})")
    if prune_skipped:
        parts.append("prune skipped: the export is not complete")
    elif args.prune:
        notes = []
        if n["prune_kept"]:
            notes.append(
                f"{n['prune_kept']} kept: written by another token under this --agent, "
                "locked, or changed during the run"
            )
        if n["prune_failed"]:
            notes.append(f"{n['prune_failed']} failed")
        parts.append(f"{n['pruned']}{would} pruned" + (f" ({', '.join(notes)})" if notes else ""))
    print(", ".join(parts))


def _cmd_import_okf(args: argparse.Namespace) -> int:
    if args.prune and not args.agent:
        # Prune deletes every import row of its agent that this bundle lacks, so each
        # bundle imported into a namespace needs an agent of its own.
        print("refusing to prune: --prune needs an explicit --agent", file=sys.stderr)
        return 2
    args.agent = args.agent or "okf-import"
    root = Path(args.path)
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 2
    refused = _lint_before_import(root, args)
    if refused is not None:
        return refused

    # The keys prune keeps and the digest and marker it checks come from one read of the
    # bundle, even when the directory changes during the run.
    files = okf_lint.read_bundle(root)
    concepts = _walk_bundle(files)
    if args.prune:
        reason = _prune_refusal(files, concepts)
        if reason:
            print(f"refusing to prune: {reason}", file=sys.stderr)
            return 2
    prune_skipped = bool(args.prune) and not _marked_complete(files)
    if prune_skipped:
        print(_PRUNE_SKIPPED, file=sys.stderr)
    token = _resolve_token(args)
    org = _resolve_org(args, token)
    ev_chunks, ev_digests = ([], []) if args.no_evidence else _walk_evidence(root)
    if not concepts and not ev_chunks and not ev_digests:
        print("no concepts found")
        return 0

    async def _go() -> int:
        planned = _one_per_row(concepts, org, args)
        links, unresolved = okf.resolve_relates_to(concepts)
        n: Counter[str] = Counter()
        async with _Client(args.server_url, token) as client:
            live_edges: set[tuple[str, str, str]] = set()
            try:
                live = await _live_rows(client, org, args)
                if links and not args.no_links:
                    edges = await _fetch_edges(client, org, args.namespace)
                    live_edges = {_edge_key(e) for e in edges}
            except RuntimeError as e:
                print(f"cannot read {org}/{args.namespace}: {e}", file=sys.stderr)
                return 1
            if args.prune and not prune_skipped:
                # Before the writes: a key renamed in the source would otherwise merge into
                # its stale row, which prune then deletes.
                keep = {(p["session_id"], p["memory_key"]) for _, p in planned}
                claims = jwt_claims(token)
                # The writer the server stamped on our rows; the agent name is the client's.
                actor = actor_for(
                    str(claims.get("sub") or ""), is_service_account=claims.get("sa") is True
                )
                n.update(await _prune(client, org, args, live, keep, actor))
            n.update(await _import_concepts(client, org, args, planned, live))
            if not args.no_links:
                n.update(await _import_links(client, org, args, links, unresolved, live_edges))
            if (ev_chunks or ev_digests) and not args.dry_run:
                ev = await _import_evidence(client, org, args, ev_chunks, ev_digests)
                print(
                    f"  evidence: {ev['chunks_written']}/{len(ev_chunks)} raw chunks "
                    f"({ev['links_written']} links), {ev['digests_written']}/{len(ev_digests)} "
                    f"session digests re-embedded by the server"
                    + (f", {ev['edited']} chunk(s) edited since export" if ev["edited"] else "")
                    + (f", {ev['skipped']} skipped" if ev["skipped"] else "")
                    + (f", {ev['failed']} FAILED" if ev["failed"] else "")
                )
                n["evidence_failed"] = ev["failed"]
        _print_import_summary(
            n,
            args,
            org,
            len(planned),
            len(ev_chunks),
            len(ev_digests),
            prune_skipped=prune_skipped,
        )
        failures = ("failed", "links_failed", "prune_failed", "evidence_failed", "verify_failed")
        return 1 if any(n[k] for k in failures) else 0

    return asyncio.run(_go())


# ------------------------------------------------------------------
# lint
# ------------------------------------------------------------------


def _cmd_okf_lint(args: argparse.Namespace) -> int:
    """The compile gate for a bundle directory: no server, no ClickHouse.
    Exit 2 on a critical finding, 1 on warnings (2 under --strict), 0 clean."""
    rep = okf_lint.lint_bundle(Path(args.path), strict=bool(args.strict))
    print(okf_lint.report_json(rep) if args.json else rep.render())
    return rep.exit_code


# ------------------------------------------------------------------
# registration
# ------------------------------------------------------------------


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--server-url", default=default_base_url())
    p.add_argument("--token", default=None, help="JWT (default: $LOOM_API_KEY, ~/.loom/token)")
    p.add_argument("--org", default=None, help="Org (default: the token's `org` claim)")
    p.add_argument("--namespace", required=True)


def register_okf_cli(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_exp = sub.add_parser(
        "export-okf",
        help="Render a namespace as an OKF v0.2 bundle (Markdown + frontmatter) — "
        "the git-reviewable projection of Loom memory.",
    )
    _common(p_exp)
    p_exp.add_argument("--out", required=True, help="Bundle directory (created if missing)")
    p_exp.add_argument("--title", default=None, help="Root index title (default: namespace)")
    p_exp.add_argument(
        "--include-tombstoned",
        action="store_true",
        help="Also export soft-deleted rows (status: deprecated); one the list does not "
        "carry in full (a value past the preview, or a high row under --include-high) is "
        "named and left out.",
    )
    p_exp.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Export at most N most-recent rows (smoke runs; default: all)",
    )
    p_exp.add_argument("--dry-run", action="store_true")
    p_exp.add_argument(
        "--no-evidence",
        action="store_true",
        help="Concepts only; skip the raw chunks and session digests "
        "(evidence/, digests/) the bundle carries by default.",
    )
    p_exp.add_argument(
        "--no-links",
        action="store_true",
        help="Skip the namespace's typed edges (operator_edges → relates_to); "
        "shared-entity links are still written.",
    )
    p_exp.add_argument(
        "--include-high",
        action="store_true",
        help="Also export rows with sensitivity=high, the raw chunks and digests linked to "
        "them, and raw text that holds a high shape (a credential, card number, SSN or "
        "stated PIN); the server audits each released item. bulk_get does not serve a high "
        "row past its retention window, so it is named and left out. Off by default: a "
        "mirror repository is a wider audience than the namespace's grants.",
    )
    p_exp.set_defaults(func=_cmd_export_okf)

    p_imp = sub.add_parser(
        "import-okf",
        help="Write the concepts of an OKF v0.2 bundle into a namespace via memory.set.",
    )
    p_imp.add_argument("path", help="Bundle directory (contains index.md)")
    _common(p_imp)
    p_imp.add_argument(
        "--agent",
        default=None,
        help="Writer agent of the imported rows (default: okf-import). Required with "
        "--prune; use one agent per bundle imported into a namespace.",
    )
    p_imp.add_argument("--user-id", default="-")
    p_imp.add_argument("--session-id", default="-")
    p_imp.add_argument(
        "--keep-sessions",
        action="store_true",
        help="Write each concept under the session recorded in its "
        "`loom.session_id` so a key that lived under "
        "several sessions stays several rows; --session-id is the fallback "
        "for rows that had none.",
    )
    p_imp.add_argument(
        "--verify",
        action="store_true",
        help="Re-attest concepts carrying a human: verification, as the caller",
    )
    p_imp.add_argument("--dry-run", action="store_true")
    p_imp.add_argument(
        "--no-evidence", action="store_true", help="Skip evidence/ and digests/ (concepts only)."
    )
    p_imp.add_argument(
        "--no-lint",
        action="store_true",
        help="Skip the lint gate that runs before the import.",
    )
    p_imp.add_argument(
        "--force-import",
        action="store_true",
        help="Import even when the lint gate reports critical findings.",
    )
    p_imp.add_argument(
        "--no-links",
        action="store_true",
        help="Do not re-create the bundle's relates_to edges with memory.link.",
    )
    p_imp.add_argument(
        "--prune",
        action="store_true",
        help="Delete the rows an earlier import wrote (this --agent and token, an okf: "
        "source_trace) whose key is no longer in the bundle. Refuses an empty bundle or "
        "one that does not match its loom.corpus_digest; a bundle its export did not mark "
        "complete is written but not pruned. With --dry-run, lists the rows it would "
        "delete.",
    )
    p_imp.set_defaults(func=_cmd_import_okf)

    p_lint = sub.add_parser(
        "okf-lint",
        help="Lint an OKF bundle directory offline — the compile gate a mirror "
        "repository's CI runs. Exit 2 critical, 1 warnings, 0 clean.",
    )
    p_lint.add_argument("path", help="Bundle directory (contains index.md)")
    p_lint.add_argument("--json", action="store_true", help="Machine-readable report")
    p_lint.add_argument(
        "--strict",
        action="store_true",
        help="Warnings fail too (exit 2); orphans stay informational.",
    )
    p_lint.set_defaults(func=_cmd_okf_lint)

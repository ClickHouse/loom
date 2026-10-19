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

Import is `memory.set` per concept (identity from the token or flags), so
it is idempotent by content hash and subject to conflict detection. With
`--verify`, concepts whose frontmatter carries a `human:` verification are
re-attested through `memory.verify` AS THE IMPORTING CALLER — the server
decides the actor from the token; the bundle's actor is recorded in the
memory's tags for provenance, never forged.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
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
from server.memory.domain import okf_bundle as okf
from server.memory.domain import okf_evidence as okf_ev
from server.memory.domain.sensitivity import is_high

_PAGE = 1000
_BULK = 500


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
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every live row of `namespace` (all agents, users and sessions), full
    values, as memory_to_concept dicts, and the keys bulk_get did not answer whose
    full value the list does not carry. `limit` caps the rows for smoke runs."""
    entries: list[dict[str, Any]] = []
    offset = 0
    while True:
        page_size = _PAGE if limit is None else max(1, min(_PAGE, limit - len(entries)))
        page = await client.post(
            "memory.list",
            {
                "org": org,
                "namespace": namespace,
                # "*" drops the predicate on each identity column (memory.list
                # otherwise scopes to one writer identity, default '-').
                "agent": "*",
                "user_id": "*",
                "session_id": "*",
                "limit": page_size,
                "offset": offset,
                "sort_by": "recency",
                "include_tombstoned": include_tombstoned,
                # memory.list hides superseded rows by default;
                # the bundle renders them as `status: deprecated`.
                "include_superseded": True,
            },
        )
        rows = page.get("entries") or []
        entries.extend(rows)
        if not page.get("truncated") or not rows or (limit is not None and len(entries) >= limit):
            break
        offset += len(rows)
    if limit is not None:
        entries = entries[:limit]

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
            if g is None and r.get("value_truncated") and r["memory_key"] not in held:
                # Not answered by bulk_get (a tombstoned row, or one deleted since the
                # list), and the list does not carry its full value (a cut preview, or
                # none for a high row).
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
        client = _Client(args.server_url, token)
        chunks: list[okf_ev.ChunkRecord] = []
        digests: list[okf_ev.DigestRecord] = []
        edges: list[dict[str, Any]] = []
        held_evidence = 0
        try:
            entries, unreadable = await _fetch_namespace(
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
        finally:
            await client.close()
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
                "linked to a row kept home, or holding a credential shape "
                "(--include-high exports them)"
            )
        if unreadable:
            print(
                f"  {len(unreadable)} row(s) left out: bulk_get did not answer them (tombstoned, "
                "or deleted since the list) and the list does not carry their full value: "
                + ", ".join(unreadable[:5])
                + (" …" if len(unreadable) > 5 else "")
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


def _walk_bundle(root: Path) -> list[okf.Concept]:
    concepts: list[okf.Concept] = []
    for p in sorted(root.rglob("*.md")):
        parts = p.relative_to(root).parts
        if any(part.startswith(".") or part == "node_modules" for part in parts):
            continue
        if p.name in okf.RESERVED_FILES:
            continue
        rel = p.relative_to(root).as_posix()
        if okf_ev.is_evidence_path(rel):
            continue  # raw chunks and digests: `_walk_evidence`, not concepts
        c = okf.parse_concept(rel, p.read_text(encoding="utf-8"))
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


async def _import_evidence(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    chunks: list[okf_ev.ChunkRecord],
    digests: list[okf_ev.DigestRecord],
    *,
    batch: int = 64,
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
        "source_trace": f"okf-import:{Path(args.path).name}",
    }
    batches: list[dict[str, Any]] = []
    for i in range(0, len(chunks), batch):
        batches.append({**payload_base, "chunks": [_chunk(c) for c in chunks[i : i + batch]]})
    for i in range(0, len(digests), batch):
        batches.append({**payload_base, "digests": [_digest(d) for d in digests[i : i + batch]]})
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
    from server.memory.domain import okf_lint

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


async def _import_concepts(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    concepts: list[okf.Concept],
    root: Path,
) -> dict[str, int]:
    """memory.set per concept (conflict detection, blacklist and audit apply);
    `--verify` re-attests human-verified concepts as the caller."""
    n = {"written": 0, "conflicted": 0, "verified": 0, "failed": 0}
    for c in concepts:
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
                "source_trace": payload.get("source_trace") or f"okf:{root.name}/{c.path}",
            }
        )
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
            continue
        try:
            res = await client.post("memory.set", payload)
        except RuntimeError as e:
            n["failed"] += 1
            print(f"  ✗ {c.path}: {e}", file=sys.stderr)
            continue
        n["written"] += 1
        if res.get("conflict") or res.get("status") == "conflicted":
            n["conflicted"] += 1
        if args.verify and bundle_actor:
            try:
                v = await client.post(
                    "memory.verify",
                    {
                        "org": org,
                        "namespace": args.namespace,
                        "agent": args.agent,
                        "user_id": args.user_id,
                        "session_id": args.session_id,
                        "memory_key": payload["memory_key"],
                    },
                )
                n["verified"] += int(bool(v.get("found")))
            except RuntimeError as e:
                print(f"  ! {c.path}: verify failed: {e}", file=sys.stderr)
    return n


async def _import_links(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    concepts: list[okf.Concept],
    root: Path,
) -> dict[str, int]:
    """Re-create the bundle's `relates_to` edges with memory.link; the author is whoever the
    token names. Entries that name no concept in the bundle (a `loom://` pointer, a file
    that did not travel) are reported, never written."""
    links, unresolved = okf.resolve_relates_to(concepts)
    for u in unresolved[:10]:
        print(
            f"  ! {u['path']}: relates_to {u['kind']} → {u['target']} is not in the bundle; "
            f"not linked",
            file=sys.stderr,
        )
    if len(unresolved) > 10:
        print(f"  ! … {len(unresolved) - 10} more unresolved relates_to entries", file=sys.stderr)
    n = {"links": 0, "links_failed": 0, "links_unresolved": len(unresolved)}
    if args.dry_run:
        n["links"] = len(links)
        return n
    for e in links:
        try:
            await client.post(
                "memory.link",
                {
                    "org": org,
                    "namespace": args.namespace,
                    "from_key": e["from_key"],
                    "to_key": e["to_key"],
                    "relation": e["relation"],
                    "explanation": f"okf bundle {root.name}: {e['path']}",
                },
            )
            n["links"] += 1
        except RuntimeError as err:
            n["links_failed"] += 1
            print(f"  ✗ link {e['from_key']} {e['relation']} {e['to_key']}: {err}", file=sys.stderr)
    return n


def _cmd_import_okf(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 2
    refused = _lint_before_import(root, args)
    if refused is not None:
        return refused
    token = _resolve_token(args)
    org = _resolve_org(args, token)
    concepts = _walk_bundle(root)
    ev_chunks, ev_digests = ([], []) if args.no_evidence else _walk_evidence(root)
    if not concepts and not ev_chunks and not ev_digests:
        print("no concepts found")
        return 0

    async def _go() -> int:
        client = _Client(args.server_url, token)
        n = {
            "written": 0,
            "conflicted": 0,
            "verified": 0,
            "failed": 0,
            "links": 0,
            "links_failed": 0,
            "links_unresolved": 0,
        }
        try:
            n.update(await _import_concepts(client, org, args, concepts, root))
            if not args.no_links:
                n.update(await _import_links(client, org, args, concepts, root))
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
                n["failed"] += ev["failed"]
        finally:
            await client.close()
        link_note = (
            ""
            if args.no_links
            else f", {n['links']} typed edges{' would be' if args.dry_run else ''} linked"
            + (f" ({n['links_unresolved']} unresolved)" if n["links_unresolved"] else "")
            + (f" ({n['links_failed']} link failures)" if n["links_failed"] else "")
        )
        if args.dry_run:
            print(
                f"dry run: {len(concepts)} concepts, {len(ev_chunks)} raw chunks and "
                f"{len(ev_digests)} session digests would be written to {org}/{args.namespace}"
                f"{link_note}"
            )
        else:
            print(
                f"imported {n['written']}/{len(concepts)} concepts into {org}/{args.namespace} "
                f"({n['conflicted']} flagged conflicted, {n['verified']} re-attested as caller, "
                f"{n['failed']} failed){link_note}"
            )
        return 1 if n["failed"] or n["links_failed"] else 0

    return asyncio.run(_go())


# ------------------------------------------------------------------
# lint
# ------------------------------------------------------------------


def _cmd_okf_lint(args: argparse.Namespace) -> int:
    """The compile gate for a bundle directory: no server, no ClickHouse.
    Exit 2 on a critical finding, 1 on warnings (2 under --strict), 0 clean."""
    from server.memory.domain import okf_lint

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
        "them, and raw text that reads as a credential; the server audits each released "
        "item. Off by default: a mirror repository is a wider audience than the "
        "namespace's grants.",
    )
    p_exp.set_defaults(func=_cmd_export_okf)

    p_imp = sub.add_parser(
        "import-okf",
        help="Write the concepts of an OKF v0.2 bundle into a namespace via memory.set.",
    )
    p_imp.add_argument("path", help="Bundle directory (contains index.md)")
    _common(p_imp)
    p_imp.add_argument("--agent", default="okf-import")
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

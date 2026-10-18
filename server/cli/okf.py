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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from server.cli._client import default_base_url, error_line, jwt_claims, resolve_token
from server.core.hashing import sha256_value
from server.memory.domain import okf_bundle as okf
from server.memory.domain import okf_evidence as okf_ev
from server.memory.domain.edge_relations import canonical_link_relation
from server.memory.domain.graph_quality import canonical_entities
from server.memory.domain.procedural_contract import RETYPE_TAG
from server.memory.domain.sensitivity import ATTRIBUTE_TAG_PREFIX, SENSITIVITY_TAG_PREFIX
from server.memory.domain.trust import actor_for

_PAGE = 1000
_BULK = 500
# Every concept row the import writes carries this source_trace prefix; --prune deletes
# nothing else.
_TRACE_PREFIX = "okf:"
# Tags memory.set adds itself (sensitivity tier, attribute labels, a procedural retype); a
# live row carrying one the bundle lacks is not a change, since the next write adds it back.
_SERVER_TAGS = (SENSITIVITY_TAG_PREFIX, ATTRIBUTE_TAG_PREFIX, RETYPE_TAG)


def _resolve_token(args: argparse.Namespace) -> str:
    token, _ = resolve_token(args.token)
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

        self.token = token
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
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every live row of `namespace` (all agents, users and sessions), full
    values, as memory_to_concept dicts. `limit` caps the rows for smoke runs."""
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
    for (agent, user_id, session_id), rows in by_ident.items():
        keys = [r["memory_key"] for r in rows]
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
            g = full.get(r["memory_key"]) or {}
            merged = {**r, **{k: v for k, v in g.items() if v not in (None, "", [], {})}}
            merged.setdefault("namespace", namespace)
            merged["agent"] = agent if agent != "*" else (r.get("agent") or "")
            out.append(merged)
    return out


async def _fetch_evidence(
    client: _Client, org: str, namespace: str, *, page: int = 500
) -> tuple[list[okf_ev.ChunkRecord], list[okf_ev.DigestRecord]]:
    """Every live raw chunk (with its memory_chunk_sources links) and session
    digest of the namespace, through memory.export_evidence."""
    chunks: list[okf_ev.ChunkRecord] = []
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
            },
        )
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
        },
    )
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
    return chunks, digests


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
    limit: int | None = None,
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
        limit=limit,
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
        try:
            entries = await _fetch_namespace(
                client,
                org,
                args.namespace,
                include_tombstoned=bool(args.include_tombstoned),
                limit=args.limit,
            )
            if not args.no_evidence:
                chunks, digests = await _fetch_evidence(client, org, args.namespace)
            if not args.no_links:
                edges = await _fetch_edges(client, org, args.namespace)
        finally:
            await client.close()
        files, orphans, skipped_high = _plan_export(
            entries,
            edges,
            chunks,
            digests,
            namespace=args.namespace,
            title=args.title,
            include_high=bool(args.include_high),
            server_url=args.server_url,
            limit=args.limit,
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
                f"  {skipped_high} row(s) with sensitivity=high stayed in the database "
                f"(--include-high exports them)"
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


def _concept_payload(
    c: okf.Concept, org: str, args: argparse.Namespace, root: Path
) -> dict[str, Any]:
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
            "source_trace": f"{_TRACE_PREFIX}{root.resolve().name}/{c.path}",
        }
    )
    return payload


_LIVE_FIELDS = [
    "session_id",
    "memory_type",
    "confidence",
    "source_trace",
    "valid_at",
    "tags",
    "entities",
    "content_sha256",
    "verification",
]


async def _live_rows(
    client: _Client, org: str, args: argparse.Namespace
) -> dict[tuple[str, str], dict[str, Any]]:
    """The live rows under the import's identity in the target namespace, by
    (session_id, memory_key), with every field memory.set writes that a read returns."""
    ident = {"org": org, "namespace": args.namespace, "agent": args.agent, "user_id": args.user_id}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
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
            rows[(str(r.get("session_id") or "-"), str(r["memory_key"]))] = r
        if not page.get("truncated") or not entries:
            break
        offset += len(entries)

    # valid_to and source_class come only from memory.get.
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
                    "fields": ["valid_to", "source_class"],
                },
            )
            for key, g in (got.get("results") or {}).items():
                if (sid, key) in rows:
                    rows[(sid, key)].update(
                        valid_to=g.get("valid_to"), source_class=g.get("source_class")
                    )
    return rows


def _when(v: Any) -> datetime | str | None:
    """A bundle or wire timestamp as naive UTC whole seconds (the column's precision)."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return str(v)  # never equal to a parsed time, so the server sees and judges it
    if d.tzinfo:
        d = d.astimezone(UTC).replace(tzinfo=None)
    return d.replace(microsecond=0)


def _one_per_row(
    concepts: list[okf.Concept], org: str, args: argparse.Namespace, root: Path
) -> list[tuple[okf.Concept, dict[str, Any]]]:
    """The concepts with their payloads, one per target row. An export holds a key once
    per writer, but the import writes every concept under its own identity: of the
    concepts that land on one row, the most recently updated is written, so a run does
    not alternate between them."""
    newest: dict[tuple[str, str], tuple[okf.Concept, dict[str, Any]]] = {}
    for c in concepts:
        p = _concept_payload(c, org, args, root)
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
    loom = c.extra.get("loom")
    return str(loom.get("updated_at") or "") if isinstance(loom, dict) else ""


def _unchanged(payload: dict[str, Any], row: dict[str, Any]) -> bool:
    """True when the live row already holds everything memory.set would write.
    `content_sha256` covers the value only, so the metadata is compared too."""
    if payload.get("source_resource_id"):
        return False  # no read returns source_resource_id, so it cannot be compared
    return (
        row.get("content_sha256") == sha256_value(payload["value"])
        and row.get("memory_type") == payload["memory_type"]
        and row.get("source_trace") == payload["source_trace"]
        and {
            t
            for t in row.get("tags") or []
            if t in payload["tags"] or not t.startswith(_SERVER_TAGS)
        }
        == set(payload["tags"])
        and math.isclose(float(row.get("confidence") or 0), payload["confidence"], abs_tol=1e-6)
        # A bundle without entities leaves them to the target's extractor.
        and (
            "entities" not in payload
            or (row.get("entities") or []) == canonical_entities(payload["entities"])
        )
        and _when(row.get("valid_at")) == _when(payload.get("valid_at"))
        and _when(row.get("valid_to")) == _when(payload.get("valid_to"))
        # Without a declared class the server derives one from the op and source_trace.
        and ("source_class" not in payload or row.get("source_class") == payload["source_class"])
    )


async def _import_concepts(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    concepts: list[okf.Concept],
    root: Path,
    live: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, int]:
    """memory.set per changed or new concept (conflict detection, blacklist and audit
    apply); `--verify` re-attests human-verified concepts as the caller."""
    n = {"written": 0, "unchanged": 0, "merged": 0, "conflicted": 0, "verified": 0, "failed": 0}
    for c, payload in _one_per_row(concepts, org, args, root):
        bundle_actor = okf.human_verified_actor(c)
        row = live.get((payload["session_id"], payload["memory_key"]))
        if row is not None and _unchanged(payload, row):
            n["unchanged"] += 1
            # The row keeps the attestation it got when it was written.
            if args.verify and bundle_actor and not args.dry_run:
                if row.get("verification") in ("none", "stale"):
                    n["verified"] += await _verify(client, org, args, payload, c)
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
        if res.get("conflict_verdict") == "merge":
            # Folded into another key's row, so nothing lands at this key and the next run
            # sends it again.
            n["merged"] += 1
            continue
        n["written"] += 1
        if res.get("conflict") or res.get("status") == "conflicted":
            n["conflicted"] += 1
        if args.verify and bundle_actor:
            n["verified"] += await _verify(client, org, args, payload, c)
    return n


async def _verify(
    client: _Client, org: str, args: argparse.Namespace, payload: dict[str, Any], c: okf.Concept
) -> int:
    """memory.verify as the caller; 1 when the row was found."""
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
        print(f"  ! {c.path}: verify failed: {e}", file=sys.stderr)
        return 0
    return int(bool(v.get("found")))


async def _import_links(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    concepts: list[okf.Concept],
    root: Path,
    live_edges: set[tuple[str, str, str]],
) -> dict[str, int]:
    """Re-create the bundle's `relates_to` edges that are not live yet with memory.link; the
    author is whoever the token names. Entries that name no concept in the bundle (a
    `loom://` pointer, a file that did not travel) are reported, never written."""
    links, unresolved = okf.resolve_relates_to(concepts)
    for u in unresolved[:10]:
        print(
            f"  ! {u['path']}: relates_to {u['kind']} → {u['target']} is not in the bundle; "
            f"not linked",
            file=sys.stderr,
        )
    if len(unresolved) > 10:
        print(f"  ! … {len(unresolved) - 10} more unresolved relates_to entries", file=sys.stderr)
    n = {"links": 0, "links_unchanged": 0, "links_failed": 0, "links_unresolved": len(unresolved)}
    new = [e for e in links if _edge_key(e) not in live_edges]
    n["links_unchanged"] = len(links) - len(new)
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
                    "explanation": f"okf bundle {root.name}: {e['path']}",
                },
            )
            n["links"] += 1
        except RuntimeError as err:
            n["links_failed"] += 1
            print(f"  ✗ link {e['from_key']} {e['relation']} {e['to_key']}: {err}", file=sys.stderr)
    return n


def _edge_key(e: dict[str, str]) -> tuple[str, str, str]:
    """(from_key, to_key, relation) as memory.link stores the edge."""
    relation, flip = canonical_link_relation(e["relation"])
    relation = relation or "related_to"
    from_key, to_key = (e["to_key"], e["from_key"]) if flip else (e["from_key"], e["to_key"])
    return from_key, to_key, relation


def _prune_refusal(root: Path, concepts: list[okf.Concept]) -> str:
    """Why this bundle must not drive deletes, or '' for a complete export: the concept
    files still hash to the `loom.corpus_digest` the export wrote into index.md."""
    if not concepts:
        return "the bundle has no concepts"
    from server.memory.domain import okf_lint

    files = okf_lint.read_bundle(root)
    try:
        fm = yaml.safe_load(okf.split_frontmatter(files.get("index.md", ""))[0])
    except yaml.YAMLError:
        fm = None
    loom = fm.get("loom") if isinstance(fm, dict) else None
    pointer: dict[str, Any] = loom if isinstance(loom, dict) else {}
    declared = str(pointer.get("corpus_digest") or "")
    if not declared:
        return "index.md carries no loom.corpus_digest, so this is not a complete export"
    if pointer.get("limit") is not None:
        return f"the export stopped at --limit {pointer['limit']}"
    if declared != okf_lint.bundle_digest(files):
        return (
            "the concept files do not match index.md's loom.corpus_digest: a partial or "
            "edited export, or one written into a directory that held an older bundle"
        )
    return ""


async def _prune(
    client: _Client,
    org: str,
    args: argparse.Namespace,
    live: dict[tuple[str, str], dict[str, Any]],
    keep: set[tuple[str, str]],
) -> dict[str, int]:
    """memory.delete each row an earlier import wrote whose key has left the bundle. Only
    rows under the import's identity with its source_trace stamp qualify, so rows anyone
    else wrote in the namespace are never touched."""
    gone = sorted(
        k
        for k, r in live.items()
        if k not in keep and str(r.get("source_trace") or "").startswith(_TRACE_PREFIX)
    )
    claims = jwt_claims(client.token)
    # The writer the server stamped on our rows: the agent name is the client's choice.
    actor = actor_for(str(claims.get("sub") or ""), is_service_account=claims.get("sa") is True)
    n = {"pruned": 0, "prune_failed": 0}
    for session_id, key in gone:
        if args.dry_run:
            where = f" (session {session_id})" if session_id != "-" else ""
            print(f"  would delete {key}{where}")
            n["pruned"] += 1
            continue
        ident = {
            "org": org,
            "namespace": args.namespace,
            "agent": args.agent,
            "user_id": args.user_id,
            "session_id": session_id,
            "memory_key": key,
        }
        try:
            # memory.delete falls back to the same key under any agent when ours is gone;
            # delete only while the row read is still ours.
            got = await client.post("memory.get", {**ident, "fields": ["agent", "actor"]})
            if not (
                got.get("found") and got.get("agent") == args.agent and got.get("actor") == actor
            ):
                continue
            res = await client.post("memory.delete", ident)
        except RuntimeError as e:
            n["prune_failed"] += 1
            print(f"  ✗ delete {key}: {e}", file=sys.stderr)
            continue
        n["pruned"] += int(bool(res.get("tombstoned")))
    return n


def _cmd_import_okf(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 2
    refused = _lint_before_import(root, args)
    if refused is not None:
        return refused
    concepts = _walk_bundle(root)
    if args.prune:
        reason = _prune_refusal(root, concepts)
        if reason:
            print(f"refusing to prune: {reason}", file=sys.stderr)
            return 2
    token = _resolve_token(args)
    org = _resolve_org(args, token)
    ev_chunks, ev_digests = ([], []) if args.no_evidence else _walk_evidence(root)
    if not concepts and not ev_chunks and not ev_digests:
        print("no concepts found")
        return 0

    async def _go() -> int:
        client = _Client(args.server_url, token)
        n = {
            "written": 0,
            "unchanged": 0,
            "merged": 0,
            "conflicted": 0,
            "verified": 0,
            "failed": 0,
            "links": 0,
            "links_unchanged": 0,
            "links_failed": 0,
            "links_unresolved": 0,
            "pruned": 0,
            "prune_failed": 0,
        }
        try:
            try:
                live = await _live_rows(client, org, args)
                live_edges: set[tuple[str, str, str]] = set()
                if not args.no_links and okf.resolve_relates_to(concepts)[0]:
                    edges = await _fetch_edges(client, org, args.namespace)
                    live_edges = {(e["from_key"], e["to_key"], e["relation"]) for e in edges}
            except RuntimeError as e:
                print(f"cannot read {org}/{args.namespace}: {e}", file=sys.stderr)
                return 1
            if args.prune:
                # Before the writes, so each delete follows the read that chose it closely.
                keep = {
                    (p["session_id"], p["memory_key"])
                    for p in (_concept_payload(c, org, args, root) for c in concepts)
                }
                n.update(await _prune(client, org, args, live, keep))
            n.update(await _import_concepts(client, org, args, concepts, root, live))
            if not args.no_links:
                n.update(await _import_links(client, org, args, concepts, root, live_edges))
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
        would = " would be" if args.dry_run else ""
        link_note = (
            ""
            if args.no_links
            else f", {n['links']} typed edges{would} linked ({n['links_unchanged']} unchanged)"
            + (f" ({n['links_unresolved']} unresolved)" if n["links_unresolved"] else "")
            + (f" ({n['links_failed']} link failures)" if n["links_failed"] else "")
        )
        prune_note = (
            f", {n['pruned']}{would} pruned"
            + (f" ({n['prune_failed']} delete failures)" if n["prune_failed"] else "")
            if args.prune
            else ""
        )
        if args.dry_run:
            print(
                f"dry run: {n['written']} of {len(concepts)} concepts "
                f"({n['unchanged']} unchanged), "
                f"{len(ev_chunks)} raw chunks and {len(ev_digests)} session digests would be "
                f"written to {org}/{args.namespace}{link_note}{prune_note}"
            )
        else:
            print(
                f"imported {len(concepts)} concepts into {org}/{args.namespace}: "
                f"{n['written']} written, {n['unchanged']} unchanged, {n['failed']} failed"
                + (f", {n['merged']} merged into another key" if n["merged"] else "")
                + " "
                f"({n['conflicted']} flagged conflicted, {n['verified']} re-attested as caller)"
                f"{link_note}{prune_note}"
            )
        return 1 if n["failed"] or n["links_failed"] or n["prune_failed"] else 0

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
        help="Also export soft-deleted rows (status: deprecated)",
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
        help="Also export rows with sensitivity=high (they stay "
        "in the database by default — a mirror repository is a wider "
        "audience than the namespace's grants).",
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
    p_imp.add_argument(
        "--prune",
        action="store_true",
        help="Delete the rows an earlier import wrote (this --agent, an okf: "
        "source_trace) whose key is no longer in the bundle. Refuses a bundle "
        "whose concept files do not match its loom.corpus_digest; with --dry-run, "
        "lists them.",
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

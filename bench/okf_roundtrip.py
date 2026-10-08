"""Portability round-trip: export a namespace as an OKF bundle, import it into a
fresh namespace, and measure what a user would lose on the way (you can leave
with your memories and come back).

    python bench/okf_roundtrip.py --namespace <namespace> \\
        --questions bench/datasets/locomo10.json --sample-id conv-30 --top-k 10

Steps: (1) `_fetch_namespace` (every live row, full values) → `plan_bundle` →
files on disk; (2) `_walk_bundle` → `concept_to_memory` → `memory.set` into
`bench-okf-rt-<hex>` under one import identity; (3) compare source vs imported, per
memory_key: present, value equal, tags equal (minus the `okf:*` tags the bundle
adds), valid_at equal, memory_type equal, source_class of the copy; (4) with
`--questions`, retrieve top-k for every question in both namespaces and report
key-set Jaccard@k and top-1 agreement. Writes only into the fresh namespace;
the source is read. The minted target is registered as a bench namespace and
erased when the run ends, after a failure too (`--keep-namespaces` keeps it for a
later `--skip-import --target` run); a `--target` you name is never erased.
Summary JSON beside `--out`.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.bench_namespaces import (
    erase_bench_namespaces,
    register_bench_namespace,
)
from server.cli.okf import (
    _Client,
    _fetch_edges,
    _fetch_evidence,
    _fetch_namespace,
    _import_evidence,
    _walk_bundle,
    _walk_evidence,
    _write_bundle,
)
from server.memory.domain import okf_bundle as okf
from server.memory.domain import okf_evidence as okf_ev

# Tags the bundle itself adds on the way through: `okf:*` (type/status) and
# `origin:*` (provenance). Neither is a change to what the
# source row said.
_BUNDLE_TAG_PREFIXES = ("okf:", "origin:")
_PLACEHOLDER_ENTITY = "_none_"  # entity_extractor sentinel: "processed, nothing found"
# `source` values a memory.search hit carries when it is a loom.memory row (the
# only table the bundle reads). Everything else (chunk, summary, entity_summary,
# …) lives in a sibling table the exporter never sees.
_MEMORY_HIT_SOURCES = frozenset({"fact", "memory", ""})


def outside_memory_pct(hits_by_source: dict[str, int]) -> float:
    total = sum(hits_by_source.values())
    if not total:
        return 0.0
    outside = sum(v for k, v in hits_by_source.items() if k not in _MEMORY_HIT_SOURCES)
    return round(100.0 * outside / total, 1)


def _norm_entities(ents: list[str] | None) -> set[str]:
    return {str(e) for e in (ents or []) if str(e) and str(e) != _PLACEHOLDER_ENTITY}


def _norm_tags(tags: list[str] | None) -> set[str]:
    return {t for t in (tags or []) if not str(t).startswith(_BUNDLE_TAG_PREFIXES)}


def _norm_value(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip()
    return v


def _iso_day(v: Any) -> str:
    return str(v or "")[:10]


def compare_rows(source: list[dict[str, Any]], imported: list[dict[str, Any]]) -> dict[str, Any]:
    """Field-by-field drift between the source rows and their imported copies,
    keyed by memory_key (the bundle keeps the key in frontmatter)."""
    src = {str(r["memory_key"]): r for r in source}
    imp = {str(r["memory_key"]): r for r in imported}
    keys = set(src)
    out: dict[str, Any] = {
        # distinct keys; the *_total counts include every identity a key lives under
        "source_rows": len(src),
        "imported_rows": len(imp),
        "source_rows_total": len(source),
        "imported_rows_total": len(imported),
        "missing_after_import": sorted(keys - set(imp))[:50],
        "extra_after_import": sorted(set(imp) - keys)[:50],
        "value_equal": 0,
        "tags_equal": 0,
        "valid_at_equal": 0,
        "type_equal": 0,
        "imported_source_class": {},
        "entities_equal": 0,
        "imported_placeholder_entities": sum(
            1 for r in imp.values() if _PLACEHOLDER_ENTITY in (r.get("entities") or [])
        ),
        "tags_gained": {},
        "tags_lost": {},
        "source_attested": sum(1 for r in src.values() if r.get("verified_by")),
        "imported_attested": sum(1 for r in imp.values() if r.get("verified_by")),
    }
    for k in keys & set(imp):
        a, b = src[k], imp[k]
        out["value_equal"] += _norm_value(a.get("value")) == _norm_value(b.get("value"))
        out["tags_equal"] += _norm_tags(a.get("tags")) == _norm_tags(b.get("tags"))
        out["valid_at_equal"] += _iso_day(a.get("valid_at")) == _iso_day(b.get("valid_at"))
        out["type_equal"] += str(a.get("memory_type")) == str(b.get("memory_type"))
        ea, eb = _norm_entities(a.get("entities")), _norm_entities(b.get("entities"))
        out["entities_equal"] += ea == eb
        for tag in _norm_tags(b.get("tags")) - _norm_tags(a.get("tags")):
            out["tags_gained"][tag] = out["tags_gained"].get(tag, 0) + 1
        for tag in _norm_tags(a.get("tags")) - _norm_tags(b.get("tags")):
            out["tags_lost"][tag] = out["tags_lost"].get(tag, 0) + 1
        sc = str(b.get("source_class") or "")
        out["imported_source_class"][sc] = out["imported_source_class"].get(sc, 0) + 1
    n = max(1, len(keys & set(imp)))
    for f in ("value_equal", "tags_equal", "valid_at_equal", "type_equal", "entities_equal"):
        out[f + "_pct"] = round(100.0 * out[f] / n, 1)
    out["missing_after_import_n"] = len(keys - set(imp))
    return out


def evidence_drift(
    src_chunks: list[Any], imp_chunks: list[Any], src_digests: list[Any], imp_digests: list[Any]
) -> dict[str, int]:
    """Counts for the two planes the bundle carries: rows,
    byte-equal bodies (by session, index, hash), chunk→memory links, digests."""

    def _cset(rows: list[Any]) -> set[tuple[str, int, str]]:
        return {(c.session_id, int(c.chunk_index), okf_ev.sha256_text(c.content)) for c in rows}

    def _dset(rows: list[Any]) -> set[tuple[str, str]]:
        return {(d.session_id, okf_ev.sha256_text(d.summary_text)) for d in rows}

    return {
        "source_chunks": len(src_chunks),
        "imported_chunks": len(imp_chunks),
        "chunk_bodies_equal": len(_cset(src_chunks) & _cset(imp_chunks)),
        "source_links": sum(len(c.memory_keys) for c in src_chunks),
        "imported_links": sum(len(c.memory_keys) for c in imp_chunks),
        "source_digests": len(src_digests),
        "imported_digests": len(imp_digests),
        "digest_texts_equal": len(_dset(src_digests) & _dset(imp_digests)),
    }


# Edge verbs the exporter keeps in ClickHouse: the digest
# chain and telemetry; they are not expected to travel.
_STAY_HOME_RELATIONS = okf.EXCLUDED_EDGE_RELATIONS


def edge_drift(src_edges: list[dict[str, Any]], imp_edges: list[dict[str, Any]]) -> dict[str, Any]:
    """Which authored edges of the source exist on the copy,
    by verb, matched on (from_key, to_key, relation). Excluded verbs are
    counted apart so a 100% on the rest is the bar."""

    def _set(rows: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
        return {(str(r["from_key"]), str(r["to_key"]), str(r["relation"])) for r in rows}

    src, imp = _set(src_edges), _set(imp_edges)
    travel = {e for e in src if e[2] not in _STAY_HOME_RELATIONS}
    by_kind: dict[str, dict[str, int]] = {}
    for e in travel:
        row = by_kind.setdefault(e[2], {"source": 0, "imported": 0})
        row["source"] += 1
        row["imported"] += e in imp
    return {
        "source_edges": len(src),
        "source_edges_stay_home": len(src) - len(travel),
        "source_edges_expected": len(travel),
        "imported_edges": len(imp),
        "edges_travelled": len(travel & imp),
        "edges_travelled_pct": (
            round(100.0 * len(travel & imp) / len(travel), 1) if travel else 100.0
        ),
        "by_kind": dict(sorted(by_kind.items())),
    }


def retrieval_overlap(pairs: list[tuple[list[str], list[str]]]) -> dict[str, Any]:
    """Jaccard of the top-k key sets and top-1 agreement over question pairs
    (source hits, imported hits)."""
    if not pairs:
        return {"questions": 0}
    jac = []
    top1 = 0
    for s, i in pairs:
        ss, ii = set(s), set(i)
        jac.append(len(ss & ii) / len(ss | ii) if (ss | ii) else 1.0)
        top1 += bool(s and i and s[0] == i[0])
    return {
        "questions": len(pairs),
        "jaccard_at_k_mean": round(mean(jac), 3),
        "jaccard_at_k_min": round(min(jac), 3),
        "top1_agreement_pct": round(100.0 * top1 / len(pairs), 1),
        "identical_sets_pct": round(100.0 * sum(1 for j in jac if j == 1.0) / len(pairs), 1),
    }


def _concept_payload(c: Any, target_org: str, target_namespace: str) -> dict[str, Any]:
    """`concept_to_memory`, passing the import target when the exporter knows how
    to stamp origin (source_class=imported + origin:* tags); older
    builds take the concept alone and the copy arrives without provenance."""
    params = inspect.signature(okf.concept_to_memory).parameters
    if "target_org" in params:
        return okf.concept_to_memory(c, target_org=target_org, target_namespace=target_namespace)
    return okf.concept_to_memory(c)


async def _search_hits(
    client: _Client, org: str, namespace: str, agent: str, query: str, top_k: int, mode: str
) -> list[dict[str, Any]]:
    resp: dict[str, Any] = {}
    last: Exception | None = None
    for attempt in range(
        4
    ):  # ClickHouse Cloud keeper hiccups are transient; one row must not end a run
        try:
            resp = await client.post(
                "memory.search",
                {
                    "org": org,
                    "namespace": namespace,
                    "agent": agent,
                    "user_id": "-",
                    "query": query,
                    "top_k": top_k,
                    "search_mode": mode,
                    "namespace_scope": "this",
                    "auto_touch": False,
                },
            )
            break
        except RuntimeError as e:
            last = e
            await asyncio.sleep(3.0 * (attempt + 1))
    else:
        raise RuntimeError(f"memory.search failed after retries: {last}")
    return [
        {"memory_key": str(h.get("memory_key")), "source": str(h.get("source") or "memory")}
        for h in (resp.get("results") or [])
    ]


def _bench_post(client: _Client) -> Any:
    """`client` as the `post(path, body)` bench_namespaces takes."""

    async def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await client.post(path.removeprefix("/v1/"), body)

    return post


async def run(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("LOOM_API_KEY", "")
    if not token:
        print("LOOM_API_KEY required", file=sys.stderr)
        return 2
    client = _Client(args.base_url, token)
    target = args.target or f"bench-okf-rt-{secrets.token_hex(4)}"
    minted = not args.target
    t0 = datetime.now(UTC)
    try:
        if minted:
            await register_bench_namespace(
                _bench_post(client),
                org=args.org,
                namespace=target,
                description="OKF round-trip import target",
                keep=args.keep_namespaces,
            )
        source = await _fetch_namespace(
            client, args.org, args.namespace, include_tombstoned=False, limit=args.limit
        )
        # Typed edges travel as relates_to
        src_edges = [] if args.no_links else await _fetch_edges(client, args.org, args.namespace)
        files = okf.plan_bundle(
            source,
            namespace=args.namespace,
            exported_at=t0,
            title=args.namespace,
            edges=src_edges,
        )
        orphans = okf.bundle_orphans(files)
        src_chunks: list[okf_ev.ChunkRecord] = []
        src_digests: list[okf_ev.DigestRecord] = []
        if not args.no_evidence:
            # Raw chunks and session digests travel too
            src_chunks, src_digests = await _fetch_evidence(client, args.org, args.namespace)
            paths = okf.concept_paths_by_key(okf.plan_concepts(source, src_edges))
            files.update(okf_ev.plan_files(src_chunks, src_digests, paths))
        bundle_dir = Path(args.bundle_dir or tempfile.mkdtemp(prefix="okf-rt-"))
        created, updated = _write_bundle(bundle_dir, files)
        concepts = _walk_bundle(bundle_dir)
        t_export = (datetime.now(UTC) - t0).total_seconds()
        # ---- import into the fresh namespace
        t1 = datetime.now(UTC)
        written = failed = 0
        for c in [] if args.skip_import else concepts:
            payload = _concept_payload(c, args.org, target)
            payload.update(
                {
                    "org": args.org,
                    "namespace": target,
                    "agent": args.import_agent,
                    "user_id": "-",
                    "session_id": (okf.concept_session_id(c) or "-") if args.keep_sessions else "-",
                    "source_trace": payload.get("source_trace")
                    or f"okf:{bundle_dir.name}/{c.path}",
                }
            )
            try:
                await client.post("memory.set", payload)
                written += 1
            except RuntimeError as e:
                failed += 1
                print(f"  ✗ {c.path}: {e}", file=sys.stderr)
        links_written = 0
        if not args.no_links and not args.skip_import:
            links, _unresolved = okf.resolve_relates_to(concepts)
            for e in links:
                try:
                    await client.post(
                        "memory.link",
                        {
                            "org": args.org,
                            "namespace": target,
                            "from_key": e["from_key"],
                            "to_key": e["to_key"],
                            "relation": e["relation"],
                            "explanation": f"okf roundtrip {bundle_dir.name}",
                        },
                    )
                    links_written += 1
                except RuntimeError as e2:
                    print(f"  ✗ link: {e2}", file=sys.stderr)
        ev_totals: dict[str, int] = {}
        if not args.no_evidence and not args.skip_import:
            ev_chunks, ev_digests = _walk_evidence(bundle_dir)
            ev_args = argparse.Namespace(
                namespace=target, agent=args.import_agent, user_id="-", path=str(bundle_dir)
            )
            ev_totals = await _import_evidence(client, args.org, ev_args, ev_chunks, ev_digests)
        t_import = (datetime.now(UTC) - t1).total_seconds()
        imported = await _fetch_namespace(client, args.org, target, include_tombstoned=False)
        imp_chunks: list[okf_ev.ChunkRecord] = []
        imp_digests: list[okf_ev.DigestRecord] = []
        if not args.no_evidence:
            imp_chunks, imp_digests = await _fetch_evidence(client, args.org, target)
        evidence = evidence_drift(src_chunks, imp_chunks, src_digests, imp_digests)
        evidence.update(
            {k: v for k, v in ev_totals.items() if k in ("failed", "edited", "skipped")}
        )
        drift = compare_rows(source, imported)
        imp_edges = [] if args.no_links else await _fetch_edges(client, args.org, target)
        edges = edge_drift(src_edges, imp_edges)
        edges["links_written"] = links_written
        # ---- retrieval overlap
        pairs: list[tuple[list[str], list[str]]] = []
        source_hit_sources: dict[str, int] = {}
        sample_pairs: list[dict[str, Any]] = []
        if args.questions:
            data = json.loads(Path(args.questions).read_text())
            convs = [s for s in data if not args.sample_id or s.get("sample_id") == args.sample_id]
            qs = [str(q.get("question") or "") for s in convs for q in (s.get("qa") or [])][
                : args.max_questions
            ]
            src_agent = source[0].get("agent") or "*" if source else "*"
            for q in qs:
                if not q:
                    continue
                s_hits = await _search_hits(
                    client, args.org, args.namespace, src_agent, q, args.top_k, args.search_mode
                )
                i_hits = await _search_hits(
                    client, args.org, target, args.import_agent, q, args.top_k, args.search_mode
                )
                for h in s_hits:
                    source_hit_sources[h["source"]] = source_hit_sources.get(h["source"], 0) + 1
                s_keys = [h["memory_key"] for h in s_hits]
                i_keys = [h["memory_key"] for h in i_hits]
                pairs.append((s_keys, i_keys))
                if len(sample_pairs) < 10:
                    sample_pairs.append({"question": q, "source": s_keys, "imported": i_keys})
        overlap = retrieval_overlap(pairs)
        if pairs:
            # Hits whose `source` is not a memory row (raw chunks, session digests,
            # entity summaries) live in tables the bundle never reads: an upper
            # bound on what portability can recover with the current exporter.
            overlap["source_hits_by_source"] = source_hit_sources
            overlap["source_hits_outside_memory_pct"] = outside_memory_pct(source_hit_sources)
            overlap["sample_pairs"] = sample_pairs
    finally:
        if minted and not args.keep_namespaces:
            await erase_bench_namespaces(_bench_post(client), org=args.org, namespaces=[target])
        await client.close()
    summary = {
        "source_namespace": args.namespace,
        "target_namespace": target,
        "bundle_files": len(files),
        "bundle_created": created,
        "bundle_updated": updated,
        "bundle_orphans": len(orphans),
        "concepts_walked": len(concepts),
        "imported_written": written,
        "imported_failed": failed,
        "export_seconds": round(t_export, 1),
        "import_seconds": round(t_import, 1),
        "drift": drift,
        "evidence": evidence,
        "edges": edges,
        "retrieval": overlap,
        "top_k": args.top_k,
        "search_mode": args.search_mode,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=1))
    d = drift
    print(
        f"{args.namespace} → {target}: keys {d['source_rows']} → {d['imported_rows']}, "
        f"rows {d['source_rows_total']} → {d['imported_rows_total']} "
        f"(missing {d['missing_after_import_n']}, extra {len(d['extra_after_import'])}); "
        f"value {d['value_equal_pct']}% tags {d['tags_equal_pct']}% "
        f"valid_at {d['valid_at_equal_pct']}% "
        f"type {d['type_equal_pct']}% entities {d['entities_equal_pct']}% "
        f"(placeholder entities on {d['imported_placeholder_entities']}); "
        f"attested {d['source_attested']} → {d['imported_attested']}; "
        f"source_class of copies {d['imported_source_class']}; bundle orphans {len(orphans)}; "
        f"export {t_export:.1f}s import {t_import:.1f}s"
    )
    e = evidence
    print(
        f"evidence: chunks {e['source_chunks']} → {e['imported_chunks']} "
        f"(bodies equal {e['chunk_bodies_equal']}, "
        f"links {e['source_links']} → {e['imported_links']}); "
        f"digests {e['source_digests']} → {e['imported_digests']} "
        f"(texts equal {e['digest_texts_equal']})"
    )
    g = edges
    print(
        f"edges: {g['source_edges']} on the source ({g['source_edges_stay_home']} stay home), "
        f"{g['edges_travelled']}/{g['source_edges_expected']} travelled "
        f"({g['edges_travelled_pct']}%), by kind {g['by_kind']}"
    )
    if pairs:
        print(
            f"retrieval over {overlap['questions']} questions @k={args.top_k} {args.search_mode}: "
            f"Jaccard mean {overlap['jaccard_at_k_mean']} min {overlap['jaccard_at_k_min']}, "
            f"top-1 agreement {overlap['top1_agreement_pct']}%, "
            f"identical sets {overlap['identical_sets_pct']}%; "
            f"{overlap['source_hits_outside_memory_pct']}% of source hits are raw chunks or "
            f"digests rather than memory rows {overlap['source_hits_by_source']}"
        )
    print(f"wrote {out}")
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=None)
    p.add_argument("--org", default="dev")
    p.add_argument("--namespace", required=True, help="source namespace (read only)")
    p.add_argument(
        "--target", default=None, help="fresh namespace to import into (default bench-okf-rt-<hex>)"
    )
    p.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="do not erase the minted bench-okf-rt-* target when the run ends",
    )
    p.add_argument("--import-agent", default="okf-import")
    p.add_argument(
        "--keep-sessions",
        action="store_true",
        help="import each concept under its exported session",
    )
    p.add_argument(
        "--skip-import",
        action="store_true",
        help="reuse an existing --target; compare and retrieve only",
    )
    p.add_argument("--limit", type=int, default=None, help="cap source rows (smoke)")
    p.add_argument("--bundle-dir", default=None)
    p.add_argument(
        "--questions", default="", help="locomo10.json; retrieval overlap on its questions"
    )
    p.add_argument("--sample-id", default="", help="restrict --questions to one conversation")
    p.add_argument("--max-questions", type=int, default=200)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--search-mode", default="semantic")
    p.add_argument(
        "--no-evidence",
        action="store_true",
        help="concepts only (a bundle without evidence): skip raw chunks and session digests",
    )
    p.add_argument(
        "--no-links",
        action="store_true",
        help="a bundle without typed links: skip typed edges on both sides",
    )
    p.add_argument("--out", default="bench/okf_roundtrip.summary.json")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

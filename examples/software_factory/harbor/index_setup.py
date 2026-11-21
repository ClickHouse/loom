"""Audit native index units against current Git bytes without rewriting provenance."""

from __future__ import annotations

import hashlib
import json

from examples.software_factory.factory.model import digest

from .source_tree import git, source_files


def require(condition, message):
    if not condition:
        raise PermissionError(message)


def edge_key(row):
    return tuple(
        row[key] for key in ("edge_kind", "from_symbol_id", "to_symbol_id", "path", "line")
    )


def check_chunks(rows, expected_chunks, expected_context, dimension):
    for row in rows:
        expected = expected_chunks.get(row["chunk_id"])
        require(
            expected is not None
            and all(
                row[key] == getattr(expected, key)
                for key in (
                    "path",
                    "scope",
                    "kind",
                    "start_line",
                    "end_line",
                    "content",
                    "content_hash",
                )
            ),
            "Native chunk body/span differs from current source",
        )
        require(
            row["embedding_dim"] == dimension
            and not row.get("embed_last_error")
            and row.get("context_text") == expected_context[row["chunk_id"]],
            "Native chunk lacks a real current-model embedding",
        )
    require(
        {row["chunk_id"] for row in rows} == set(expected_chunks),
        "Native chunk set differs from current source",
    )


async def audit_index(request):
    from server.code.app.index.ingest_chunks import _chunks_for, chunk_context_texts
    from server.code.app.index.ingest_edges import _derive_edges
    from server.code.app.index.ingest_parse import parse_for_ingest
    from server.code.app.index.ingest_reconcile import _same_symbol_row
    from server.code.app.index.ingest_stats import FileIngestStats
    from server.code.app.index.pass_budget import git_text_source
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client, set_query_tag
    from server.settings import settings

    org, ns, head = request["org"], request["namespace"], request["commit"]
    files = source_files(request["source_git"], head, request["source_roots"])
    require(digest(files) == request["source_sha256"], "Native audit source differs from fixture")
    client = get_client()
    repo = CodeRepo()
    set_query_tag("bench.factory.native_source_parity")
    params = {"org": org, "ns": ns}
    tables = {}
    for table in ("code_files", "code_chunks", "code_symbols", "code_edges"):
        projection = "* EXCEPT (embedding)" if table == "code_chunks" else "*"
        encoded = await client.raw_bytes(
            "SELECT "
            + projection
            + " FROM loom."
            + table
            + " FINAL WHERE org={org:String} AND namespace={ns:String} "
            + "AND tombstoned_at IS NULL FORMAT JSONEachRow",
            params,
        )
        tables[table] = [json.loads(line) for line in encoded.splitlines() if line]
    not_indexed = {}
    ancestors = set()
    expected_chunks = {}
    expected_symbols = {}
    expected_context = {}
    expected_edges = set()
    with git_text_source(request["source_git"], head):
        for path, body in files.items():
            pf, error = parse_for_ingest(path, body.encode())
            require(not error, "Current source has a native parser error")
            if pf is None:
                not_indexed[path] = {
                    "reason": "native_parser_declined",
                    "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
                continue
            chunks, prefixes, _ = _chunks_for(
                pf, path, body.encode(), settings.code_chunk_max_chars, ns_key=(org, ns)
            )
            expected_chunks.update({chunk.chunk_id(ns): chunk for chunk in chunks})
            _, texts = chunk_context_texts(ns, path, pf, chunks, chunks, prefixes, {})
            expected_context.update(
                {chunk.chunk_id(ns): text for chunk, text in zip(chunks, texts, strict=True)}
            )
            expected_symbols.update(
                {(path, symbol.qualified_name, symbol.id): symbol for symbol in pf.symbols}
            )
            edges, _ = await _derive_edges(repo, org, ns, pf, path, FileIngestStats(path))
            expected_edges.update(
                (edge.edge_kind, edge.from_symbol_id, edge.to_symbol_id, edge.path, edge.line)
                for edge in edges
            )
    require(
        {row["path"] for row in tables["code_files"]} == set(files) - set(not_indexed),
        "Index has missing/extra files beyond explicitly declined source",
    )
    for rows in tables.values():
        for row in rows:
            require(row["path"] in files, "Index row leaves the source allowlist")
            old = row["indexed_commit"]
            require(isinstance(old, str) and len(old) == 40, "Index row has no source commit")
            if old not in ancestors:
                git(request["source_git"], "merge-base", "--is-ancestor", old, head)
                ancestors.add(old)
    for row in tables["code_files"]:
        require(
            row["content_hash"] == hashlib.sha256(files[row["path"]].encode()).hexdigest()
            and not row.get("index_error"),
            "Native file bytes or parser status differ",
        )
    check_chunks(tables["code_chunks"], expected_chunks, expected_context, settings.embedding_dim)
    tree_symbols = [row for row in tables["code_symbols"] if row["tier"] == "treesitter"]
    require(
        {(row["path"], row["qualified_name"], row["symbol_id"]) for row in tree_symbols}
        == set(expected_symbols),
        "Native declaration set differs from current parse",
    )
    for row in tree_symbols:
        require(
            _same_symbol_row(
                row, expected_symbols[(row["path"], row["qualified_name"], row["symbol_id"])]
            ),
            "Native declaration metadata differs from current parse",
        )
    tree_edges = [row for row in tables["code_edges"] if row["tier"] == "treesitter"]
    require(
        {edge_key(row) for row in tree_edges} == expected_edges,
        "Native edge set differs from current resolution",
    )
    compiler_symbols = [row for row in tables["code_symbols"] if row["tier"] == "scip"]
    compiler_edges = [row for row in tables["code_edges"] if row["tier"] == "scip"]
    require(
        all(row["indexed_commit"] == head for row in compiler_symbols + compiler_edges),
        "Compiler rows do not name their actual full snapshot",
    )
    producer_symbols = {
        row["symbol_id"]
        for row in compiler_symbols
        if row.get("scip_symbol", "").startswith(request["producer"] + " ")
    }
    counts = dict(
        compiler_definitions=len(compiler_symbols),
        compiler_references=len(compiler_edges),
        **{
            request["producer_count_fields"]["definitions"]: len(producer_symbols),
            request["producer_count_fields"]["references"]: sum(
                row["from_symbol_id"] in producer_symbols for row in compiler_edges
            ),
        },
    )
    summaries = {
        table: {"rows": len(rows), "paths_sha256": digest(sorted({row["path"] for row in rows}))}
        for table, rows in tables.items()
        if table != "code_files"
    }
    provenance = {
        "schema_version": 1,
        "kind": "native-current-source-unit-parity",
        "verified": True,
        "org": org,
        "namespace": ns,
        "base_commit": head,
        "source_sha256": digest(files),
        "ancestor_commits": sorted(ancestors),
        "not_indexed": not_indexed,
        "unit_sha256": {
            table: digest(
                sorted(
                    digest(row)
                    for row in (
                        [
                            {
                                key: value
                                for key, value in row.items()
                                if key not in ("created_at", "version", "tombstoned_at")
                            }
                            for row in rows
                        ]
                    )
                )
            )
            for table, rows in tables.items()
        },
        "checks": [
            "git_ancestry",
            "file_bytes",
            "current_chunk_body_span_set",
            "current_declaration_metadata_set",
            "current_native_edge_set",
            "compiler_snapshot_commit",
        ],
    }
    scope = dict(
        verified=True,
        base_commit=head,
        source_sha256=digest(files),
        paths_sha256=digest(sorted(files)),
        file_count=len(files),
        indexed_file_count=len(tables["code_files"]),
        not_indexed=not_indexed,
        tables=summaries,
        provenance_sha256=digest(provenance),
    )
    return {"source_scope": scope, "provenance": provenance, "compiler_rows": counts}

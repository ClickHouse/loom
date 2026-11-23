"""Match live compiler units to a pinned snapshot while retaining row provenance."""

from __future__ import annotations

import math

from server.code.domain.extract.treesitter import symbol_id

SYMBOL_KEY = ("path", "qualified_name", "symbol_id")
EDGE_KEY = ("path", "edge_kind", "from_symbol_id", "to_symbol_id", "line")


def require(condition, message):
    if not condition:
        raise PermissionError(message)


def symbol_rows(document):
    for symbol in document["symbols"]:
        yield {
            "path": document["path"],
            "qualified_name": symbol["qualified_name"],
            "symbol_id": symbol_id(symbol["qualified_name"], symbol["signature"]),
            "name": symbol["name"] or symbol["qualified_name"].split("::")[-1],
            "kind": symbol["kind"],
            "signature": symbol["signature"],
            "start_line": symbol["line_start"],
            "end_line": symbol["line_end"],
            "decl_paths": symbol["decl_paths"],
            "doc_comment": symbol["doc"][:2000],
        }


def edge_rows(document, is_declaration):
    for edge in document["edges"]:
        if edge["edge_kind"] == "calls" and is_declaration and is_declaration(edge["line"]):
            continue
        yield {
            "path": document["path"],
            "edge_kind": edge["edge_kind"],
            "from_symbol_id": symbol_id(edge["from_qualified_name"], edge["from_signature"]),
            "to_symbol_id": symbol_id(edge["to_qualified_name"], edge["to_signature"]),
            "line": edge["line"],
            "from_qualified_name": edge["from_qualified_name"],
            "to_qualified_name": edge["to_qualified_name"],
            "confidence": edge["confidence"],
        }


def expected_units(snapshot, declarations):
    symbols, edges = {}, {}
    for document in snapshot["files"]:
        if document["deleted"]:
            continue
        for row in symbol_rows(document):
            symbols.setdefault(tuple(row[key] for key in SYMBOL_KEY), row)
        for row in edge_rows(document, declarations[document["path"]]):
            edges.setdefault(tuple(row[key] for key in EDGE_KEY), row)
    return symbols, edges


def check_units(expected, actual, key_fields, kind):
    indexed = {tuple(row[key] for key in key_fields): row for row in actual}
    require(
        len(indexed) == len(actual) and set(indexed) == set(expected),
        "Native compiler " + kind + " set differs from the pinned snapshot",
    )
    for key, row in expected.items():
        for field, value in row.items():
            current = indexed[key].get(field)
            matches = (
                isinstance(current, (int, float))
                and math.isfinite(current)
                and math.isclose(current, value, rel_tol=1e-6, abs_tol=1e-7)
                if field == "confidence"
                else current == value
            )
            require(matches, f"Native compiler {kind} metadata differs for {key}: {field}")


def origin_symbols(snapshot):
    origins = {}
    for document in snapshot["files"]:
        for symbol in document["symbols"]:
            key = (
                document["path"],
                symbol["qualified_name"],
                symbol_id(symbol["qualified_name"], symbol["signature"]),
            )
            origins.setdefault(key, symbol["scip_symbol"])
    return origins


def check_snapshot(snapshots, head, symbols, edges, declarations):
    expected_symbols, expected_edges = expected_units(snapshots[head], declarations)
    check_units(expected_symbols, symbols, SYMBOL_KEY, "symbol")
    check_units(expected_edges, edges, EDGE_KEY, "edge")
    origins = {commit: origin_symbols(snapshot) for commit, snapshot in snapshots.items()}
    for row in symbols + edges:
        require(row["indexed_commit"] in origins, "Compiler row has no pinned origin snapshot")
    for row in symbols:
        key = tuple(row[field] for field in SYMBOL_KEY)
        require(
            origins[row["indexed_commit"]].get(key) == row["scip_symbol"],
            "Raw compiler symbol differs from its recorded origin snapshot",
        )


def audit_snapshot(snapshots, tables, files, head):
    from server.code.app.index.scip_delta import (
        ScipDelta,
        _declaration_line_test_from_spans,
        _function_symbol_spans,
    )

    symbols = [row for row in tables["code_symbols"] if row["tier"] == "scip"]
    edges = [row for row in tables["code_edges"] if row["tier"] == "scip"]
    tree_symbols = [row for row in tables["code_symbols"] if row["tier"] == "treesitter"]
    if not snapshots:
        require(not symbols and not edges, "Parser scope contains compiler rows")
        return symbols, edges
    normalized = {}
    for commit, payload in snapshots.items():
        snapshot = ScipDelta.model_validate(payload).model_dump()
        require(
            snapshot["commit"] == commit
            and not snapshot["base"]
            and not snapshot["deleted_files"]
            and len({f["path"] for f in snapshot["files"]}) == len(snapshot["files"])
            and all(not f["deleted"] for f in snapshot["files"]),
            "Compiler payload is not a full snapshot at its recorded revision",
        )
        normalized[commit] = snapshot
    require(
        head in normalized and all(f["path"] in files for f in normalized[head]["files"]),
        "Current compiler snapshot differs from pinned source",
    )
    declarations = {
        path: _declaration_line_test_from_spans(
            [
                (r["kind"], r["start_line"], r["end_line"])
                for r in tables["code_chunks"]
                if r["path"] == path
            ],
            _function_symbol_spans([r for r in tree_symbols if r["path"] == path]),
        )
        for path in files
    }
    check_snapshot(normalized, head, symbols, edges, declarations)
    return symbols, edges

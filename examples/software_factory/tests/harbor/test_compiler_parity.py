"""Compiler content must match the snapshot even when unchanged rows retain old commits."""

import copy
import unittest

from examples.software_factory.harbor.compiler_parity import check_snapshot
from server.code.domain.extract.treesitter import symbol_id


class CompilerParity(unittest.TestCase):
    def setUp(self):
        self.symbol = {
            "qualified_name": "a.run",
            "name": "run",
            "kind": "function",
            "signature": "()",
            "line_start": 2,
            "line_end": 5,
            "doc": "A function",
            "scip_symbol": "compiler a.run",
            "decl_paths": [],
        }
        self.edge = {
            "edge_kind": "calls",
            "from_qualified_name": "a.run",
            "from_signature": "()",
            "to_qualified_name": "a.other",
            "to_signature": "()",
            "line": 4,
            "confidence": 0.8,
        }
        self.snapshot = {
            "files": [
                {
                    "path": "src/a.py",
                    "deleted": False,
                    "symbols": [self.symbol],
                    "edges": [self.edge],
                }
            ]
        }
        self.symbols = [
            {
                "path": "src/a.py",
                "qualified_name": "a.run",
                "symbol_id": symbol_id("a.run", "()"),
                "name": "run",
                "kind": "function",
                "signature": "()",
                "start_line": 2,
                "end_line": 5,
                "doc_comment": "A function",
                "scip_symbol": "compiler a.run",
                "decl_paths": [],
                "indexed_commit": "a" * 40,
            }
        ]
        self.edges = [
            {
                "path": "src/a.py",
                "edge_kind": "calls",
                "from_symbol_id": symbol_id("a.run", "()"),
                "to_symbol_id": symbol_id("a.other", "()"),
                "line": 4,
                "from_qualified_name": "a.run",
                "to_qualified_name": "a.other",
                "confidence": 0.800000011920929,
                "indexed_commit": "a" * 40,
            }
        ]
        self.declarations = {"src/a.py": None}
        self.head = "b" * 40
        self.snapshots = {"a" * 40: self.snapshot, self.head: copy.deepcopy(self.snapshot)}
        self.snapshots[self.head]["files"][0]["symbols"][0]["scip_symbol"] = (
            "compiler current a.run"
        )

    def test_matching_old_rows_and_float32_confidence_are_valid(self):
        check_snapshot(self.snapshots, self.head, self.symbols, self.edges, self.declarations)

    def test_unchanged_identity_does_not_excuse_stale_metadata(self):
        for field, value in (("end_line", 9), ("doc_comment", "Old notes"), ("name", "old")):
            rows = copy.deepcopy(self.symbols)
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(PermissionError, field):
                check_snapshot(self.snapshots, self.head, rows, self.edges, self.declarations)
        rows = copy.deepcopy(self.edges)
        rows[0]["confidence"] = 0.9
        with self.assertRaisesRegex(PermissionError, "confidence"):
            check_snapshot(self.snapshots, self.head, self.symbols, rows, self.declarations)

    def test_missing_extra_and_duplicate_units_refuse(self):
        for symbols, edges in (
            ([], self.edges),
            (self.symbols * 2, self.edges),
            (self.symbols, []),
        ):
            with (
                self.subTest(symbols=len(symbols), edges=len(edges)),
                self.assertRaises(PermissionError),
            ):
                check_snapshot(self.snapshots, self.head, symbols, edges, self.declarations)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["files"][0]["edges"] = []
        with self.assertRaises(PermissionError):
            check_snapshot(
                {**self.snapshots, self.head: snapshot},
                self.head,
                self.symbols,
                self.edges,
                self.declarations,
            )

    def test_only_calls_on_native_declaration_lines_are_filtered(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["files"][0]["edges"] += [
            {**self.edge, "line": 1},
            {**self.edge, "edge_kind": "inherits", "line": 1},
        ]
        inherited = {**self.edges[0], "edge_kind": "inherits", "line": 1}
        check_snapshot(
            {**self.snapshots, self.head: snapshot},
            self.head,
            self.symbols,
            self.edges + [inherited],
            {"src/a.py": lambda n: n == 1},
        )

    def test_raw_tokens_and_commit_must_match_a_pinned_origin(self):
        for field, value in (("scip_symbol", "forged token"), ("indexed_commit", "c" * 40)):
            rows = copy.deepcopy(self.symbols)
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                check_snapshot(self.snapshots, self.head, rows, self.edges, self.declarations)

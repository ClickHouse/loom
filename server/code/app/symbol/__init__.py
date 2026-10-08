"""code.symbol / outline / callers / callees / impact / examples / history handlers.

The structural ops: exact answers from the symbol/edge tables, scip tier preferred,
embeddings never consulted. Wire models are imported lazily (operations→handlers layering).

One module per op, over shared layers that import nothing above them:

- `resolve`: a name, id or `path:line` to definition rows; bodies and file rows.
- `diff`: unified-diff parsing for the diff-shaped ops.
- `coverage`: what the index can vouch for (scip coverage, staleness, declined calls,
  constructor edges).
- `dispatch`: virtual dispatch and member folding for the walks.
- `edges`: the one-hop walk, the impact expansion and the PR overlay.
- `hierarchy`, `uses`, `neighbours`: the legs code.symbol and code.enclosing attach.
- `symbol`, `outline`, `graph`, `impact`, `gate`, `enclosing`, `verify`, `trace`,
  `examples`, `history`: one op each.

This module re-exports the names callers import, so
`from server.code.app.symbol import ...` keeps working.
"""

from __future__ import annotations

from server.code.app.symbol.diff import _name_matches, _parse_unified_diff_full
from server.code.app.symbol.enclosing import (
    ENCLOSING_KINDS,
    handle_code_enclosing,
    section_rows,
)
from server.code.app.symbol.examples import handle_code_examples
from server.code.app.symbol.gate import handle_code_test_gate
from server.code.app.symbol.graph import handle_code_callees, handle_code_callers
from server.code.app.symbol.history import handle_code_history
from server.code.app.symbol.impact import handle_code_impact
from server.code.app.symbol.neighbours import (
    _attach_enclosing_neighbours,
    documented_in_for,
    is_doc_path,
    module_of,
)
from server.code.app.symbol.outline import handle_code_outline
from server.code.app.symbol.resolve import (
    _FILE_LINE,
    _innermost,
    _resolve_symbols,
)
from server.code.app.symbol.symbol import handle_code_symbol
from server.code.app.symbol.trace import handle_code_trace
from server.code.app.symbol.verify import (
    _normalize_claim_name,
    _resolve_claim_symbol,
    bfs_callees,
    handle_code_verify,
)

__all__ = [
    "ENCLOSING_KINDS",
    "_FILE_LINE",
    "_attach_enclosing_neighbours",
    "_innermost",
    "_name_matches",
    "_normalize_claim_name",
    "_parse_unified_diff_full",
    "_resolve_claim_symbol",
    "_resolve_symbols",
    "bfs_callees",
    "documented_in_for",
    "handle_code_callees",
    "handle_code_callers",
    "handle_code_enclosing",
    "handle_code_examples",
    "handle_code_history",
    "handle_code_impact",
    "handle_code_outline",
    "handle_code_symbol",
    "handle_code_test_gate",
    "handle_code_trace",
    "handle_code_verify",
    "is_doc_path",
    "module_of",
    "section_rows",
]

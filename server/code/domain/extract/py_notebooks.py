"""A Jupyter notebook is Python the index can read.

An `.ipynb` is JSON; its code cells (`cells[].source` where `cell_type == "code"`) are
the program. As a text-fallback chunk of JSON, the functions a notebook defines would
not be symbols, its imports not edges, and `code.grep` would match the escaped JSON.
Here the code cells become one Python text, each cell headed by a marker comment (`# %%
[cell N]`, the cell's index in the notebook), IPython line magics and shell escapes
commented out so the grammar sees valid Python. The parse runs on that text;
`cell_facets` maps the marker lines back to the cells so a symbol's line resolves to
(cell, line in cell).

Pure: bytes in, bytes out. Outside every extractor stamp — the notebooks a
namespace holds gain their symbols by a targeted backfill over its
`.ipynb` paths or by the paced heal after the next deploy (both re-read the
source through `ingest_parse._parse_python`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from server.code.domain.extract.lang_sql_model import Facet

FACET_KIND = "notebook_cell"
CELL_MARK = "# %% [cell {index}]"
_MARK_RE = re.compile(r"^# %% \[cell (\d+)\]$", re.M)
_MAGIC_RE = re.compile(r"^(\s*)([%!?].*)$")


def is_notebook_path(path: str) -> bool:
    return path.lower().endswith(".ipynb")


def _cells(nb: Any) -> list[Any]:
    if not isinstance(nb, dict):
        return []
    cells = nb.get("cells")
    if isinstance(cells, list):
        return cells
    # nbformat 3: worksheets[].cells[] with `input` instead of `source`
    ws = nb.get("worksheets")
    if isinstance(ws, list):
        return [c for w in ws if isinstance(w, dict) for c in (w.get("cells") or [])]
    return []


def _cell_text(cell: dict[str, Any]) -> str:
    src = cell.get("source", cell.get("input", ""))
    if isinstance(src, list):
        src = "".join(str(s) for s in src)
    return str(src)


def notebook_source(raw: bytes) -> bytes | None:
    """The notebook's code cells as one Python text with cell markers; None
    when the bytes are not a notebook (the caller keeps the text fallback)."""
    try:
        nb = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    cells = _cells(nb)
    if not cells:
        return None
    out: list[str] = []
    for i, cell in enumerate(cells):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        out.append(CELL_MARK.format(index=i))
        for line in _cell_text(cell).splitlines():
            m = _MAGIC_RE.match(line)
            out.append(f"{m.group(1)}# {m.group(2)}" if m else line)
    if not out:
        return None
    return ("\n".join(out) + "\n").encode("utf-8")


def cell_facets(source: bytes) -> list[Facet]:
    """One `notebook_cell` facet per code cell, named `cell N` at the marker
    line: the line map from the parsed text back to the notebook."""
    text = source.decode("utf-8", "replace")
    out: list[Facet] = []
    for k, m in enumerate(_MARK_RE.finditer(text)):
        out.append(
            Facet(
                kind=FACET_KIND,
                name=f"cell {m.group(1)}",
                ordinal=k,
                line=text.count("\n", 0, m.start()) + 1,
                confidence=1.0,
            )
        )
    return out


def cell_of_line(source: bytes, line: int) -> tuple[int, int] | None:
    """(cell index, 1-based line within the cell) for a line of the parsed
    text; None before the first marker."""
    text = source.decode("utf-8", "replace")
    best: tuple[int, int] | None = None
    for m in _MARK_RE.finditer(text):
        mark_line = text.count("\n", 0, m.start()) + 1
        if mark_line > line:
            break
        best = (int(m.group(1)), line - mark_line)
    return best if best is not None and best[1] >= 1 else None


__all__ = [
    "CELL_MARK",
    "FACET_KIND",
    "cell_facets",
    "cell_of_line",
    "is_notebook_path",
    "notebook_source",
]

"""An extractor artifact in the shape the scorer reads, shared by the bench's
runner (run.py, in its child process) and the qualification gates
(tests/unit/test_extraction_gates.py), so the two read a parse the same way.

A block is `{"type", "text", "page_start", "page_end", "line_start", "line_end",
"level", "keys", "key_path", "notes"}` and a table `{"rows", "page_start",
"page_end"}`, its header row first. A table's row blocks count once, as its
cells, where its first row block sits; a DOCX, PPTX or HTML table block
(`attrs.table`, cells joined by ` | `) is read as its grid.
"""

from __future__ import annotations

from typing import Any


def _grid(t: dict[str, Any]) -> list[list[str]]:
    rows = [[c["name"] for c in t["columns"]]] if t.get("header_rows", 1) else []
    return rows + [["" if v is None else str(v) for v in r] for r in t["rows"]]


def normalize(art: dict[str, Any]) -> dict[str, Any]:
    """The parse of artifact `art` (`Artifact.to_dict()`): its blocks and tables,
    whether it is complete, its failed units, lost capabilities and warnings, and
    the pages or slides it read with OCR or names as holding unread images."""
    art_tables = {t["id"]: t for t in art.get("tables", [])}
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    seen: set[str] = set()
    for b in art["blocks"]:
        loc, attrs = b.get("locator", {}), b.get("attrs", {})
        nb = {
            "type": b["type"],
            "text": b["text"],
            "page_start": loc.get("page_start", 0),
            "page_end": loc.get("page_end", 0),
            "line_start": loc.get("line_start", 0),
            "line_end": loc.get("line_end", 0),
            "level": b.get("depth", 0),
            "keys": b.get("keys", []),
            "key_path": loc.get("key_path", ""),
            "notes": bool(attrs.get("notes")),
        }
        tid = attrs.get("table_id")
        if tid in art_tables:
            if tid in seen:
                continue
            seen.add(tid)
            t, tl = art_tables[tid], art_tables[tid].get("locator", {})
            nb.update(
                type="table",
                text="\n".join(" | ".join(r) for r in _grid(t)),
                page_start=tl.get("page_start", 0),
                page_end=tl.get("page_end", 0),
            )
            tables.append(
                {"rows": _grid(t), "page_start": nb["page_start"], "page_end": nb["page_end"]}
            )
        elif "table" in attrs:
            rows = [line.split(" | ") for line in b["text"].split("\n")]
            tables.append(
                {"rows": rows, "page_start": nb["page_start"], "page_end": nb["page_end"]}
            )
            nb["type"] = "table"
        blocks.append(nb)
    cov, facts = art["coverage"], art.get("attrs", {})
    return {
        "complete": cov["complete"],
        "blocks": blocks,
        "tables": tables,
        "failed": cov.get("failed", []),
        "lost": cov.get("lost", []),
        "warnings": list(art.get("warnings", [])),
        "images_unread": list(facts.get("images_unread", [])),
        "ocr_pages": list(facts.get("ocr_pages", facts.get("ocr_slides", []))),
        "parser_version": art["parser_version"],
    }

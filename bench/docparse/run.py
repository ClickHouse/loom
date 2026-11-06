"""Parse every corpus file with the reference extractor, one child process per
file, and write the normalized parses `score` reads, one JSON line per file:
`{"file", "complete", "seconds", "peak_mb", "blocks", "tables", ...}`, a block
`{"type", "text", "page_start", "page_end", "level", "notes"}` and a table
`{"rows", "page_start", "page_end"}` (its header row first).

A table's row blocks count once, as its cells, where its first row block sits;
a DOCX or PPTX table block (`attrs.table`, cells joined by ` | `) is read as its
grid. Another parser is compared by writing the same shape; the scorer counts a
table's words from `tables` and leaves a block of type `table` out, so a table
written both ways counts once. A file whose parse fails or passes TIMEOUT_S is
`{"error": ...}`, scored 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = 900  # one file's parse

CHILD = r"""
import json, resource, sys, time
from loom_extractor.source import extract
path = sys.argv[1]
data = open(path, "rb").read()
t0 = time.perf_counter()
try:
    a = extract(data, path.rsplit("/", 1)[-1])
except Exception as e:  # noqa: BLE001 — a file the extractor fails on is that file's score, not the run's
    print(json.dumps({"error": f"{type(e).__name__}: {e}"})); sys.exit(0)
dt = time.perf_counter() - t0
art = a.to_dict()
blocks, tables = [], []
art_tables = {t["id"]: t for t in art.get("tables", [])}
def grid(t):
    rows = [[c["name"] for c in t["columns"]]] if t.get("header_rows", 1) else []
    return rows + [["" if v is None else str(v) for v in r] for r in t["rows"]]
seen = set()
for b in art["blocks"]:
    loc = b.get("locator", {})
    attrs = b.get("attrs", {})
    nb = {"type": b["type"], "text": b["text"], "page_start": loc.get("page_start", 0),
          "page_end": loc.get("page_end", 0), "level": b.get("depth", 0), "notes": bool(attrs.get("notes"))}
    tid = attrs.get("table_id")
    if tid in art_tables:
        # a table's row blocks count once, as its cells, where its first row block sits
        if tid in seen:
            continue
        seen.add(tid)
        t = art_tables[tid]
        tl = t.get("locator", {})
        nb.update(type="table", text="\n".join(" | ".join(r) for r in grid(t)),
                  page_start=tl.get("page_start", 0), page_end=tl.get("page_end", 0))
        tables.append({"rows": grid(t), "page_start": nb["page_start"], "page_end": nb["page_end"]})
    elif "table" in attrs:
        rows = [line.split(" | ") for line in b["text"].split("\n")]
        tables.append({"rows": rows, "page_start": nb["page_start"], "page_end": nb["page_end"]})
        nb["type"] = "table"
    blocks.append(nb)
cov = art["coverage"]
print(json.dumps({"seconds": dt, "complete": cov["complete"], "blocks": blocks, "tables": tables,
                  "failed": cov.get("failed", []), "lost": cov.get("lost", []), "parser_version": art["parser_version"], "peak_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}))
"""


def run(
    corpus: Path,
    out: Path,
    extractor: Path,
    python: str = sys.executable,
    only: set[str] | None = None,
) -> None:
    """Parse each corpus file with the extractor package in `extractor` (its
    `extractor/` directory) under `python`."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(extractor)}
    gold = [json.loads(line) for line in (corpus / "gold.jsonl").read_text().splitlines()]
    with out.open("w") as f:
        for g in gold:
            if only and g["file"] not in only:
                continue
            res = _one(python, corpus / g["file"], env)
            res["file"] = g["file"]
            f.write(json.dumps(res) + "\n")
            f.flush()
            print(
                g["file"],
                f"{res.get('seconds', -1):.2f}s",
                res.get("complete"),
                res.get("error", "")[:80],
            )


def _one(python: str, path: Path, env: dict[str, str]) -> dict:
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            [python, "-c", CHILD, str(path)],
            capture_output=True,
            env=env,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {TIMEOUT_S} s", "wall_seconds": time.perf_counter() - t0}
    # the child prints one object and exits 0, a parse that failed included; a
    # child that died in native code may leave any bytes behind
    res = None
    if p.returncode == 0:
        try:
            res = json.loads(p.stdout.decode(errors="replace").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            pass
    if not isinstance(res, dict):
        tail = p.stderr.decode(errors="replace")[-400:]
        res = {"error": f"no parse (exit {p.returncode})" + (f": {tail}" if tail else "")}
    res["wall_seconds"] = time.perf_counter() - t0
    return res

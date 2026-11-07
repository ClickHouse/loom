"""Parse every corpus file with the reference extractor, one child process per
file, and write the normalized parses `score` reads, one JSON line per file:
`bench.docparse.normalize`'s shape plus `{"file", "seconds", "peak_mb"}`.

Another parser is compared by writing the same shape; the scorer counts a
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
ROOT = Path(__file__).resolve().parents[2]

CHILD = r"""
import json, resource, sys, time
from loom_extractor.source import extract
from bench.docparse.normalize import normalize
path = sys.argv[1]
data = open(path, "rb").read()
t0 = time.perf_counter()
try:
    a = extract(data, path.rsplit("/", 1)[-1])
except Exception as e:  # noqa: BLE001 — a file the extractor fails on is that file's score, not the run's
    print(json.dumps({"error": f"{type(e).__name__}: {e}"})); sys.exit(0)
dt = time.perf_counter() - t0
art = a.to_dict()
if len(sys.argv) > 2 and sys.argv[2]:  # the artifact itself, for the downstream check
    with open(sys.argv[2], "wb") as out:
        out.write(a.to_json())
parse = normalize(art)
parse.update(seconds=dt, peak_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
print(json.dumps(parse))
"""


def run(
    corpus: Path,
    out: Path,
    extractor: Path,
    python: str = sys.executable,
    only: set[str] | None = None,
    artifacts: Path | None = None,
) -> None:
    """Parse each corpus file with the extractor package in `extractor` (its
    `extractor/` directory) under `python`; with `artifacts`, each file's
    artifact is written there too, as `<file>.json`."""
    if artifacts is not None:
        empty_artifacts_dir(artifacts)
    # The extractor under test first; this checkout for the shared normalize.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join([str(extractor), str(ROOT)]),
    }
    gold = [json.loads(line) for line in (corpus / "gold.jsonl").read_text().splitlines()]
    with out.open("w") as f:
        for g in gold:
            if only and g["file"] not in only:
                continue
            art = str(artifacts / f"{g['file']}.json") if artifacts else ""
            res = _one(python, corpus / g["file"], env, art)
            res["file"] = g["file"]
            f.write(json.dumps(res) + "\n")
            f.flush()
            print(
                g["file"],
                f"{res['wall_seconds']:.2f}s",
                res.get("complete"),
                res.get("error", "")[:80],
            )


def empty_artifacts_dir(path: Path) -> None:
    """`path`, made when missing; SystemExit when it holds anything, as an
    artifact left there from another run would be served as this run's parse."""
    if path.is_dir() and any(path.iterdir()):
        raise SystemExit(f"{path} is not empty: an artifact left there would be served as a parse")
    path.mkdir(parents=True, exist_ok=True)


def _one(python: str, path: Path, env: dict[str, str], artifact: str = "") -> dict:
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            [python, "-c", CHILD, str(path), artifact],
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
        tail = p.stderr.decode(errors="replace").strip()[-400:]
        res = {"error": f"no parse (exit {p.returncode})" + (f": {tail}" if tail else "")}
    res["wall_seconds"] = time.perf_counter() - t0
    return res

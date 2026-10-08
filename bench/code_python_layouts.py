"""The Python layout harness.

What the parser tier holds for ANY Python checkout, offline (no ClickHouse):
every file the interpreter would see is parsed the way `ingest_file` parses
it, the name table is built, the imports are settled against the checkout's
own path set (and its layout roots when the tree has `py_layouts`), and the
call edges are derived. Per checkout it reports files by language, files
whose parse has an ERROR node, symbols by kind, import specifiers total /
resolved to a path / unresolved-but-repo-shaped (the first segment is a
top-level package of the checkout yet no path matched — a layout bug),
call edges by confidence band and misses.

usage: python bench/code_python_layouts.py <loom_tree_root> <checkout> [<out.json>]
"""

from __future__ import annotations

import collections
import json
import os
import posixpath
import sys
import time

root, checkout = sys.argv[1], sys.argv[2].rstrip("/")
out_path = sys.argv[3] if len(sys.argv) > 3 else ""
sys.path.insert(0, root)
os.chdir(root)
os.environ.setdefault("LOOM_INSTANCE_LABEL", "layouts")
from server.code.app.index.ingest import default_denylist  # noqa: E402
from server.code.app.index.ingest_parse import parse_file  # noqa: E402
from server.code.domain.extract.edges_treesitter import (  # noqa: E402
    callee_targets,
    extract_edges_and_misses,
)
from server.code.domain.extract.lang_python import resolve_imports  # noqa: E402
from server.code.domain.extract.name_scope import gate_name_table  # noqa: E402

try:  # the layout module; absent on the tree the "before" run measures
    from server.code.domain.extract import py_layouts
except ImportError:  # pragma: no cover - the before run
    py_layouts = None  # type: ignore[assignment]

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache"}
PY_EXTS = (".py", ".pyi", ".pyw", ".ipynb", ".pyx", ".pxd")
MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")

t0 = time.monotonic()
rules = default_denylist()
paths: list[str] = []
for dp, dn, fn in os.walk(checkout):
    dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
    for f in sorted(fn):
        rel = posixpath.relpath(posixpath.join(dp, f), checkout)
        paths.append(rel)


def read(rel: str) -> bytes:
    with open(posixpath.join(checkout, rel), "rb") as fh:
        return fh.read()


present_all: set[str] = set()
refused = collections.Counter()
by_lang = collections.Counter()
kinds = collections.Counter()  # ext → count for the Python-ish extensions
parsed: dict[str, object] = {}
errored: list[str] = []
name_table: dict[str, list[tuple[str, str, str]]] = {}
returns_by_qn: dict[str, str] = {}
symbols = collections.Counter()
manifest_texts: dict[str, str] = {}
for rel in paths:
    src = read(rel)
    reason = rules.refusal(rel, src)
    if reason:
        refused[reason.split(":")[0]] += 1
        continue
    present_all.add(rel)
    if posixpath.basename(rel) in MANIFESTS:
        manifest_texts[rel] = src.decode("utf-8", "replace")
    try:
        pf = parse_file(rel, src)
    except Exception as e:  # noqa: BLE001 - the harness reports, never stops
        errored.append(f"{rel}: raised {type(e).__name__}")
        continue
    lang = pf.lang if pf is not None else "(dropped)"
    by_lang[lang] += 1
    if rel.endswith(PY_EXTS):
        kinds[f"{rel[rel.rfind('.') :]} → {lang}"] += 1
    if pf is None or pf.lang != "python":
        continue
    parsed[rel] = pf
    tree = getattr(pf, "tree", None)
    if tree is None:
        errored.append(f"{rel}: no tree")
    elif tree.root_node.has_error:
        errored.append(f"{rel}: ERROR node")
    for s in pf.symbols:
        symbols[s.kind] += 1
        name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
        if getattr(s, "return_type", ""):
            returns_by_qn[s.qualified_name] = s.return_type

# the checkout's top-level package names: a directory with __init__.py whose
# parent has none, plus every bare module beside such a package or at a root
top_level: set[str] = set()
init_dirs = {p[: -len("/__init__.py")] for p in present_all if p.endswith("/__init__.py")}
for d in init_dirs:
    parent = d.rsplit("/", 1)[0] if "/" in d else ""
    if parent not in init_dirs:
        top_level.add(d.rsplit("/", 1)[-1])
for p in present_all:
    if p.endswith(".py") and "/" not in p:
        top_level.add(p[:-3])

layout = None
roots_extra: tuple[str, ...] = ()
entry_points = 0
if py_layouts is not None:
    layout = py_layouts.layout_from_texts(manifest_texts, present_all)
    roots_extra = tuple(layout.roots)
    entry_points = len(layout.entry_points)


def resolve(rel: str, specs: list[str]) -> list[str]:
    if layout is not None:
        return resolve_imports(rel, specs, present_all, roots=layout.roots_for(rel))
    return resolve_imports(rel, specs, present_all)


imports = collections.Counter()
unresolved_shaped = collections.Counter()
unresolved_shaped_by_dir = collections.Counter()
edges_by_band = collections.Counter()
edge_kinds = collections.Counter()
misses_total = 0
for rel, pf in parsed.items():
    specs = list(pf.includes)  # type: ignore[attr-defined]
    imports["specifiers"] += len(specs)
    resolved_paths = [p for p in resolve(rel, specs) if p != rel]
    resolved_specs = 0
    for spec in specs:
        hits = [p for p in resolve(rel, [spec]) if p != rel]
        if hits:
            resolved_specs += 1
            continue
        # `from a.b import x` names `a.b.x` too (x may be a submodule); when
        # `a.b` itself resolved, x is a name of it, not a missing file
        level = len(spec) - len(spec.lstrip("."))
        tail = spec[level:]
        parent = spec[: spec.rfind(".")] if "." in tail else ("." * level if level else "")
        if not tail and rel.endswith("__init__.py"):
            imports["self_package"] += 1  # `from . import x` inside the package's own __init__
            continue
        if parent and (
            parent.strip(".") == ""
            and rel.endswith("__init__.py")
            or [p for p in resolve(rel, [parent]) if p != rel]
        ):
            imports["member_of_resolved"] += 1
            continue
        head = spec.lstrip(".").split(".", 1)[0]
        if spec.startswith(".") or head in top_level:
            imports["unresolved_repo_shaped"] += 1
            unresolved_shaped[spec] += 1
            unresolved_shaped_by_dir[rel.split("/", 1)[0] if "/" in rel else "."] += 1
        else:
            imports["unresolved_external"] += 1
    imports["resolved"] += resolved_specs
    pf.includes = resolved_paths  # type: ignore[attr-defined]
    gated = gate_name_table(name_table, "python")
    lane_context: dict[str, object] = {"py_present": present_all}
    if layout is not None:
        lane_context["py_roots"] = layout.roots_for(rel)
    try:
        targets = callee_targets(pf, gated, lane_context)
        callee_returns = {c: returns_by_qn[q] for c, q in targets.items() if returns_by_qn.get(q)}
    except Exception:  # noqa: BLE001
        callee_returns = {}
    try:
        edges, misses = extract_edges_and_misses(
            pf, gated, None, callee_returns or None, lane_context
        )
    except Exception as e:  # noqa: BLE001
        errored.append(f"{rel}: extract raised {type(e).__name__}: {e}")
        continue
    for e in edges:
        edge_kinds[e.edge_kind] += 1
        if e.edge_kind == "calls":
            edges_by_band[str(round(e.confidence, 1))] += 1
    misses_total += len(misses)

report = {
    "checkout": checkout,
    "seconds": round(time.monotonic() - t0, 1),
    "files_walked": len(paths),
    "files_refused": dict(refused),
    "files_by_lang": dict(by_lang.most_common()),
    "python_kinds": dict(sorted(kinds.items())),
    "python_parsed": len(parsed),
    # one line per failure; a file can fail to parse or carry an ERROR node, and fail extraction
    "python_with_error_node": sum(1 for e in errored if e.endswith(": ERROR node")),
    "python_parse_failed": sum(1 for e in errored if e.endswith(": no tree") or ": raised " in e),
    "python_extract_failed": sum(1 for e in errored if ": extract raised " in e),
    "symbols": dict(sorted(symbols.items())),
    "imports": dict(imports),
    "layout_roots_extra": sorted(roots_extra)[:40],
    "entry_points": entry_points,
    "edges": dict(edge_kinds),
    "calls_by_confidence": dict(sorted(edges_by_band.items(), reverse=True)),
    "misses": misses_total,
    "top_unresolved_repo_shaped": unresolved_shaped.most_common(40),
    "unresolved_repo_shaped_by_dir": unresolved_shaped_by_dir.most_common(12),
    "errored_sample": errored[:20],
}
print(json.dumps(report, indent=1))
if out_path:
    with open(out_path, "w") as fh:
        json.dump(report, fh, indent=1)

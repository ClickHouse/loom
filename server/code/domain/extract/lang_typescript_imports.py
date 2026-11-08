"""TypeScript / JavaScript import resolution: the bindings an import makes, the
candidate paths a specifier names (relative, alias, workspace package, package
exports), barrels and their re-exports."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import OrderedDict
from typing import Any

from server.code.domain.extract.lang_typescript import (
    _INDEX_FILES,
    _MAX_ALIAS_DEPTH,
    _MAX_CANDIDATES_PER_IMPORT,
    _ROOT_ALIASES,
    _WORKSPACE_DIRS,
    IMPORT_EXTS,
)
from server.code.domain.extract.treesitter import ParsedFile, node_text


def import_bindings(pf: ParsedFile) -> dict[str, str]:
    """{local name: specifier} for every name an `import` statement binds —
    the default import, each named import (under its alias), a namespace
    import — plus `const x = require("y")`."""
    out: dict[str, str] = {}
    if pf.tree is None:
        return out
    source = pf.source
    for node in pf.tree.root_node.named_children:
        if node.type != "import_statement":
            continue
        src = node.child_by_field_name("source")
        spec = node_text(src, source).strip("\"'`") if src is not None else ""
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if not spec or clause is None:
            continue
        for c in clause.named_children:
            if c.type == "identifier":
                out[node_text(c, source)] = spec
            elif c.type == "namespace_import":
                ident = next((x for x in c.named_children if x.type == "identifier"), None)
                if ident is not None:
                    out[node_text(ident, source)] = spec
            elif c.type == "named_imports":
                for sp in c.named_children:
                    if sp.type != "import_specifier":
                        continue
                    alias = sp.child_by_field_name("alias") or sp.child_by_field_name("name")
                    if alias is not None:
                        out[node_text(alias, source)] = spec
    text = source.decode("utf-8", "replace")
    for m in re.finditer(r"""(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*["']([^"']+)["']""", text):
        out[m.group(1)] = m.group(2)
    # `const m = await import("y")` binds m as y's namespace; `const { go } = await
    # import("y")` binds go to y
    for m in re.finditer(
        r"""(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?import\(\s*["']([^"']+)["']""",
        text,
    ):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(
        r"""(?:const|let|var)\s*\{([^{}]*)\}\s*=\s*(?:await\s+)?import\(\s*["']([^"']+)["']""",
        text,
    ):
        for part in m.group(1).split(","):
            name = part.split(":")[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                out[name] = m.group(2)
    # `const { a, b: c } = require("y")` binds a and c to y
    for m in re.finditer(
        r"""(?:const|let|var)\s*\{([^{}]*)\}\s*=\s*require\(\s*["']([^"']+)["']""",
        text,
    ):
        for part in m.group(1).split(","):
            name = part.split(":")[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                out[name] = m.group(2)
    return out


def external_names(pf: ParsedFile, resolved: set[str]) -> set[str]:
    """The local names bound to specifiers no indexed path satisfies —
    library imports. `resolved` is the file's resolved include set.

    A relative or root-alias specifier always names a repository file, even when nothing
    resolved it (a missing file is a miss, not a library). A bare specifier is a
    library's when neither the extension ladder nor a workspace package directory
    (`@scope/name` → `packages/name/…`, the `exports`-table and `main` resolutions land
    there) holds any of the file's resolved includes — otherwise every workspace-package
    import (e.g. `@clickhouse/client-common`) would be marked external and the call
    ladder would refuse to bind it."""
    out: set[str] = set()
    for name, spec in import_bindings(pf).items():
        if spec.startswith((".", "/")) or spec.startswith(_ROOT_ALIASES):
            continue
        if any(c in resolved for c in import_candidates(pf.path, spec)):
            continue
        dirs = [d.rstrip("/") + "/" for d in package_dirs_for(spec)]
        if any(inc.startswith(d) for d in dirs for inc in resolved):
            continue
        out.add(name)
    return out


def drop_external_misses(pf: ParsedFile, misses: list[Any], resolved: set[str]) -> list[Any]:
    """A declined call whose callee, or whose receiver's root, is a name the
    file imported from a package is a library call, not a site anyone asks
    about (the shell lane's `drop_program_misses` for TypeScript)."""
    ext = external_names(pf, resolved)
    if not ext:
        return misses
    kept: list[Any] = []
    for m in misses:
        root = str(m.call_text).split(".", 1)[0].split("(", 1)[0]
        if m.callee_name in ext or root in ext:
            continue
        kept.append(m)
    return kept


def stem_forms(stem: str) -> list[str]:
    """Public name of `_stem_forms` (tsconfig targets)."""
    return _stem_forms(stem)


def _stem_forms(stem: str) -> list[str]:
    """The files a module specifier may name, in the compiler's probing
    order: `<stem>.ts` … then `<stem>/index.ts` …; a specifier that already
    carries a code extension (`./x.js` for `./x.ts` under ESM) names its
    stem's forms too."""
    out: list[str] = []
    bare = stem
    for ext in sorted(IMPORT_EXTS, key=len, reverse=True):  # `.d.ts` before `.ts`
        if stem.endswith(ext):
            bare = stem[: -len(ext)]
            break
    if stem.endswith((".json", ".css", ".svg", ".png", ".wav", ".md")):
        return [stem]
    out.extend(bare + e for e in IMPORT_EXTS)
    out.extend(posixpath.join(bare, i) for i in _INDEX_FILES)
    return out


def import_candidates(from_path: str, spec: str) -> list[str]:
    """Repository paths one specifier may name, most likely first. Pure."""
    spec = spec.split("?", 1)[0].strip()
    if not spec:
        return []
    here = posixpath.dirname(from_path)
    if spec.startswith("."):
        stem = posixpath.normpath(posixpath.join(here, spec)) if here else posixpath.normpath(spec)
        return [] if stem.startswith("..") else _stem_forms(stem)
    for alias in _ROOT_ALIASES:
        if spec.startswith(alias):
            return _alias_candidates(here, spec[len(alias) :])
    if spec == "$lib" or spec.startswith("$lib/"):
        # SvelteKit's default alias (`kit.files.lib`, src/lib); its generated tsconfig
        # that spells it out is not committed
        rest = spec[5:] if spec != "$lib" else ""
        return _stem_forms(posixpath.normpath("src/lib/" + rest) if rest else "src/lib")
    return _package_candidates(spec)


def _alias_candidates(here: str, rest: str) -> list[str]:
    """`@/src/x` from `web/src/a/b.ts`: `<ancestor>/src/x` for the ancestors
    nearest the REPOSITORY ROOT first (the root itself, then `web/`, then
    `web/src/` …, at most `_MAX_ALIAS_DEPTH` deep) — a root alias points at
    the package that holds the file, and packages sit one or two levels
    down in a monorepo, never beside the importing file."""
    out: list[str] = []
    parts = here.split("/") if here else []
    for depth in range(0, min(len(parts), _MAX_ALIAS_DEPTH) + 1):
        d = "/".join(parts[:depth])
        base = posixpath.normpath(posixpath.join(d, rest)) if d else posixpath.normpath(rest)
        out.extend(f for f in _stem_forms(base) if f not in out)
    return out


def _package_candidates(spec: str) -> list[str]:
    """`@scope/name/sub` or `name/sub`: the workspace package `name` under
    the conventional roots, its `sub` path (as written, then under `src/`)
    or its entry (`src/index`, `index`)."""
    parts = spec.split("/")
    if spec.startswith("@"):
        if len(parts) < 2:
            return []
        name, sub = parts[1], "/".join(parts[2:])
    else:
        name, sub = parts[0], "/".join(parts[1:])
    if not name or name in {"node", "react", "next", "zod"}:
        return []
    out: list[str] = []
    for root in _WORKSPACE_DIRS:
        pkg = root + name
        # a subpath export (`@scope/pkg/query`) maps through the package's `exports` table, which
        # this rung does not read; the forms below are the conventions the table usually encodes:
        # the path as written, under `src/`, a `src/features/` module, each as a file or a directory
        # index
        subs = (
            [sub, posixpath.join("src", sub), posixpath.join("src/features", sub)]
            if sub
            else ["src/index", "index", "src/main", "src/lib"]
        )
        for s in subs:
            out.extend(f for f in _stem_forms(posixpath.join(pkg, s)) if f not in out)
    return out


# A package's `exports` table. Node resolves a subpath import (`@langfuse/shared/query`)
# ONLY through it: the key `./query` maps, under the first matching condition, to a
# target such as `./dist/src/features/query/index.js`. The plane has no `dist/`, so a
# target is probed as written, then with its first segment (`dist`, `build`, `lib`,
# `out`) replaced by `src` and dropped, each through the extension ladder (`.js` → `.ts`
# / `.tsx`). Conditions in the order TypeScript tooling reads them; a `null` target
# means "not exported". Pure.
_EXPORT_CONDITIONS: tuple[str, ...] = ("types", "import", "module", "default", "require", "node")
_BUILD_DIRS: frozenset[str] = frozenset({"dist", "build", "lib", "out", "esm", "cjs"})
PACKAGE_ROOTS: tuple[str, ...] = ("packages/", "", "apps/", "libs/")


def package_dirs_for(spec: str) -> list[str]:
    """The directories a workspace package named by `spec` may live in
    (`@scope/name/sub` / `name/sub` → `packages/name`, `name`, …)."""
    parts = spec.split("/")
    name = parts[1] if spec.startswith("@") and len(parts) > 1 else parts[0]
    return [root + name for root in PACKAGE_ROOTS] if name else []


def package_subpath(spec: str) -> str:
    """`@scope/name/a/b` → `./a/b`; `name` → `.`."""
    parts = spec.split("/")
    rest = parts[2:] if spec.startswith("@") else parts[1:]
    return "./" + "/".join(rest) if rest else "."


# `exports` with each target's condition picked: one target string, or subpath -> target
ExportsView = str | dict[str, str] | None


def _pick_condition(target: Any) -> str:
    if isinstance(target, str):
        return target
    if isinstance(target, dict):
        for cond in _EXPORT_CONDITIONS:
            if cond in target:
                picked = _pick_condition(target[cond])
                if picked:
                    return picked
        for key, value in target.items():  # a nested condition object without a known key
            if key in _EXPORT_CONDITIONS:
                continue  # read above: a second read per level is exponential in the depth
            picked = _pick_condition(value)
            if picked:
                return picked
    if isinstance(target, list):
        for item in target:
            picked = _pick_condition(item)
            if picked:
                return picked
    return ""


def _exports_target(exports: ExportsView, subpath: str) -> str:
    """The target string a manifest view's `exports` (`_view`: each target's condition
    already picked) maps `subpath` to, '' when the subpath is not exported. Exact keys
    first, then `*` patterns (longest literal prefix wins, as Node ranks them)."""
    if isinstance(exports, str):
        return exports if subpath == "." else ""
    if not isinstance(exports, dict):
        return ""
    if subpath in exports:
        return exports[subpath]
    best_key, best = "", ""
    for key, target in exports.items():
        if "*" not in key:
            continue
        head, _, tail = key.partition("*")
        fits = subpath.startswith(head) and subpath.endswith(tail)
        if fits and len(subpath) >= len(head) + len(tail):
            if len(head) > len(best_key):
                star = subpath[len(head) : len(subpath) - len(tail)]
                best_key, best = head, target.replace("*", star)
    return best


# Parsed manifests kept, by count and by the characters of their texts: a view holds up
# to about 7 times its text's size, and a miss only parses again.
# not a setting: a memory bound on a parse cache
_MANIFESTS_KEPT = 256
# not a setting: a memory bound on a parse cache
_MANIFESTS_CHARS = 4 * 1024 * 1024
# digest -> (view, its text's length)
_MANIFESTS: OrderedDict[bytes, tuple[tuple[ExportsView, str] | None, int]] = OrderedDict()
_manifest_chars = 0


def _view(data: Any) -> tuple[ExportsView, str] | None:
    """`_manifest`'s view of a parsed package.json; None when it is no object."""
    if not isinstance(data, dict):
        return None
    raw = data.get("exports")
    exports: ExportsView = raw if isinstance(raw, str) else None
    if isinstance(raw, dict):
        if any(k.startswith(".") for k in raw):
            exports = {k: _pick_condition(v) for k, v in raw.items()}
        else:
            exports = _pick_condition(raw)  # conditions at the top level: "." only
    return exports, str(data.get("types") or data.get("module") or data.get("main") or "")


def _manifest(package_json: str) -> tuple[ExportsView, str] | None:
    """(`exports` with each target's condition picked, the `types` / `module` / `main`
    fallback) of a package.json, parsed once per text: every importer of the package
    asks again. None when it does not parse."""
    global _manifest_chars
    key = hashlib.blake2b(package_json.encode(), digest_size=16).digest()
    if key in _MANIFESTS:
        _MANIFESTS.move_to_end(key)
        return _MANIFESTS[key][0]
    try:
        view = _view(json.loads(package_json))
    except (ValueError, RecursionError):  # nesting deeper than the parser's or our stack
        view = None
    _MANIFESTS[key] = (view, len(package_json))
    _manifest_chars += len(package_json)
    while len(_MANIFESTS) > 1 and (
        len(_MANIFESTS) > _MANIFESTS_KEPT or _manifest_chars > _MANIFESTS_CHARS
    ):
        _manifest_chars -= _MANIFESTS.popitem(last=False)[1][1]
    return view


def clear_manifests() -> None:
    """Drop every parsed manifest (tests start from an empty cache)."""
    global _manifest_chars
    _MANIFESTS.clear()
    _manifest_chars = 0


def exports_candidates(spec: str, pkg_dir: str, package_json: str) -> list[str]:
    """Repository paths the package's `exports` (or `main` / `types`) maps
    the specifier to, most likely first; [] when the package.json does not
    parse or the subpath is not exported."""
    view = _manifest(package_json)
    if view is None:
        return []
    exports, fallback = view
    subpath = package_subpath(spec)
    target = _exports_target(exports, subpath)
    if not target and subpath == ".":
        target = fallback
    if not target:
        return []
    rel = target[2:] if target.startswith("./") else target.lstrip("/")
    forms = [rel]
    segs = rel.split("/")
    if segs and segs[0] in _BUILD_DIRS:
        forms.append("/".join(["src", *segs[1:]]))
        forms.append("/".join(segs[1:]))
    out: list[str] = []
    for form in forms:
        base = posixpath.normpath(posixpath.join(pkg_dir, form)) if pkg_dir else form
        out.extend(f for f in _stem_forms(base) if f not in out)
    return out


def unresolved_package_specs(from_path: str, specs: list[str], present: set[str]) -> list[str]:
    """The package-shaped specifiers (`@scope/name[/sub]`, `name/sub`) the
    probing ladder left unresolved — the `exports` table's work."""
    out: list[str] = []
    for spec in specs:
        if spec.startswith((".", "/", *_ROOT_ALIASES)) or not package_dirs_for(spec):
            continue
        if any(c in present for c in import_candidates(from_path, spec)):
            continue
        if spec not in out:
            out.append(spec)
    return out


# A barrel (`index.ts` that only re-exports) is where an import lands, not where the
# name is defined. `reexport_specs` reads the module specifiers a barrel forwards
# (`export * from "./x"`, `export { a, b } from "./x"`, `export type { T } from "./x"`);
# the ingest follows them ONE level from every resolved import target, so a file that
# imports from `features/query` also depends on `features/query/dataModel` — an
# `includes` edge at `BARREL_CONFIDENCE`, below the direct import's 1.0.
_REEXPORT_RE = re.compile(
    r"""^[^\S\n]*export\s+(?:type\s+)?(?:\*(?:\s+as\s+\w+)?|\{[^{}]*\})\s+from\s+["']([^"']+)["']""",
    re.M,
)
BARREL_CONFIDENCE = 0.9
# A directory index forwards a handful of siblings; a package FACADE forwards the whole
# package (often a hundred modules or more in a large TypeScript monorepo). Following a
# facade makes every importer depend on everything, so a barrel above this fan-out is
# not followed.
BARREL_MAX_FANOUT = 16


def reexport_specs(text: str) -> list[str]:
    """The module specifiers a file re-exports from, in order, deduplicated."""
    return list(dict.fromkeys(m.group(1) for m in _REEXPORT_RE.finditer(text)))


def barrel_targets(barrel_path: str, text: str, present: set[str]) -> list[str]:
    """The indexed files a barrel forwards to (relative specifiers only — a
    barrel re-exporting a package is the package's business). Pure."""
    specs = [s for s in reexport_specs(text) if s.startswith(".")]
    if len(specs) > BARREL_MAX_FANOUT:
        return []  # a package facade, not a directory index
    return [p for p in resolve_imports(barrel_path, specs, present) if p != barrel_path]


def resolve_imports(
    from_path: str,
    specs: list[str],
    present: set[str],
    extra: dict[str, list[str]] | None = None,
) -> list[str]:
    """The indexed paths the specifiers name, in import order, one per specifier (the
    first candidate that exists). `present` is the oracle — the namespace's live paths
    or a fixture set. `extra`: per specifier, the exact candidates a tsconfig's `paths`
    / `baseUrl` gave, tried before the guessing ladder. Pure."""
    out: list[str] = []
    for spec in specs:
        cands = [*(extra or {}).get(spec, []), *import_candidates(from_path, spec)]
        hit = next((c for c in cands if c in present), None)
        if hit and hit != from_path and hit not in out:
            out.append(hit)
    return out


def wanted_paths(from_path: str, specs: list[str]) -> list[str]:
    """Every candidate path of every specifier, for one membership read."""
    out: list[str] = []
    for spec in specs:
        out.extend(
            c
            for c in import_candidates(from_path, spec)[:_MAX_CANDIDATES_PER_IMPORT]
            if c not in out
        )
    return out

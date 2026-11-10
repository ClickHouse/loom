"""Where a Python import may start, for any repository.

The import rung resolves an absolute specifier under the importer's
ancestors, the repository root and the `src/` beneath each. The
interpreter has more: every directory a build manifest makes a package
root (`pyproject.toml` / `setup.py` / `setup.cfg` — a monorepo has several,
an example app under `examples/tutorial/` has its own), the source
directories the manifests name (`[tool.setuptools] package-dir`,
`packages.find where`, `[tool.poetry] packages from`, `[tool.hatch.build]
packages` / `sources`), pytest's `pythonpath` and the directory of a
`conftest.py` outside any package (its rootdir insertion), and the vendored
trees an importer INSIDE them sees (`pip/_vendor/requests` resolves
`import urllib3` to the vendored copy; a file outside the vendor root does
not). This module reads those from the manifests' texts and the path set —
pure, no filesystem — and the same texts also name the entry points
(`[project.scripts]`, `console_scripts`, `__main__.py`, an
`if __name__ == "__main__":` block) as `entry_point` facets.

The ingest reads the manifest texts from the live chunks, as it reads
package.json and tsconfig for TypeScript; the lane corpus and
`bench/code_python_layouts.py` read them from disk.
"""

from __future__ import annotations

import configparser
import functools
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.treesitter import ParsedFile

FACET_KIND = "entry_point"

PACKAGE_MANIFESTS: frozenset[str] = frozenset({"pyproject.toml", "setup.py", "setup.cfg"})
PYTEST_MANIFESTS: frozenset[str] = frozenset({"pytest.ini", "tox.ini"})
MANIFEST_NAMES: frozenset[str] = PACKAGE_MANIFESTS | PYTEST_MANIFESTS
# a directory segment that holds copied third-party code: a root for the
# files inside it only (pip's `_vendor`, the `vendor/` the denylist refuses)
VENDOR_SEGMENTS: frozenset[str] = frozenset({
    "_vendor", "vendor", "_vendored", "vendored", "third_party", "third-party",
})
MAX_MANIFESTS = 200
# a file can serve every entry point: each is a facet on it, so a manifest of 1 MiB of them
# must not give every module hundreds of thousands
MAX_ENTRY_POINTS = 10_000
MAX_ROOTS = 64  # a tree of fixture packages (pip's tests/data) must not make every probe a scan

_TABLE_RE = re.compile(r"^\s*+\[\s*+([^\]]*[^\]\s])\s*+\]\s*+(?:#.*)?$")
_KV_RE = re.compile(
    r"""^\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_.\-]+))\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_ENTRY_GROUPS = {"project.scripts": "console_scripts", "project.gui-scripts": "gui_scripts"}
_ENTRY_PREFIX = "project.entry-points."
# Possessive runs: three `\s*` around two optional parts split one run of spaces three ways.
_TARGET_RE = re.compile(
    r"^\s*+([A-Za-z_][\w.]*)\s*+(?::\s*+([A-Za-z_][\w.]*))?\s*+(?:\[.*\])?\s*+$"
)
_SETUP_EP_RE = re.compile(r"""["'](console_scripts|gui_scripts)["']\s*:\s*\[""")
_SETUP_STR_RE = re.compile(r"""["']([^"'\n]+)["']""")
_SETUP_PKGDIR_RE = re.compile(r"package_dir\s*=\s*\{([^{}]*)\}")
_SETUP_WHERE_RE = re.compile(
    r"""find(?:_namespace)?_packages\s*\(\s*(?:where\s*=\s*)?["']([^"']+)["']"""
)
_MAIN_GUARD_RE = re.compile(
    r"""__name__\s*==\s*["']__main__["']|["']__main__["']\s*==\s*__name__"""
)


@dataclass(frozen=True)
class EntryPoint:
    """One declared entry point: `pip = "pip._internal.cli.main:main"`."""

    name: str
    group: str  # console_scripts | gui_scripts | the `[project.entry-points.<group>]` name
    module: str
    attr: str  # '' when the target is a module
    manifest: str
    line: int  # 1-based, in the manifest


@dataclass
class Layout:
    """What the manifests and the path set say about a checkout."""

    roots: tuple[str, ...] = ()  # repo-relative directories, '' the root
    entry_points: tuple[EntryPoint, ...] = ()
    manifests: tuple[str, ...] = ()
    notes: dict[str, str] = field(default_factory=dict)  # manifest → why it was skipped

    def roots_for(self, from_path: str) -> tuple[str, ...]:
        """The roots an importer may start an absolute specifier from: every
        manifest root, minus a vendored one the importer is not under."""
        d = from_path.rsplit("/", 1)[0] if "/" in from_path else ""
        out: list[str] = []
        for r in self.roots:
            if is_vendored(r) and not (d == r or d.startswith(r + "/")):
                continue
            out.append(r)
        return tuple(out)

    @functools.cached_property
    def entry_points_by_stem(self) -> dict[str, list[tuple[int, EntryPoint]]]:
        """(index, entry point) by its module's path stem (`a.b` -> `a/b`): a file finds
        the entry points it serves by its own stems, not by every entry point's candidates."""
        out: dict[str, list[tuple[int, EntryPoint]]] = {}
        for i, ep in enumerate(self.entry_points):
            out.setdefault(ep.module.strip().replace(".", "/"), []).append((i, ep))
        return out


def is_vendored(path: str) -> bool:
    return any(seg in VENDOR_SEGMENTS for seg in path.split("/") if seg)


def _dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _join(base: str, rel: str) -> str:
    rel = rel.strip().strip("/")
    if rel in ("", "."):
        return base
    return posixpath.normpath(posixpath.join(base, rel)) if base else posixpath.normpath(rel)


def _as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        return [s for s in re.split(r"[\s,]+", v.strip()) if s]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if isinstance(x, str)]
    return []


# ---------------------------------------------------------------------------
# roots
# ---------------------------------------------------------------------------


def _toml(text: str) -> dict[str, Any] | None:
    import tomllib

    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, TypeError, RecursionError):
        return None


def _get(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _pyproject_roots(base: str, data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    # [tool.setuptools] package-dir = {"" = "src", "pkg" = "lib/pkg"}
    pkg_dir = _get(tool, "setuptools", "package-dir")
    if isinstance(pkg_dir, dict):
        for name, d in pkg_dir.items():
            if not isinstance(d, str):
                continue
            if name == "":
                out.append(_join(base, d))
            elif posixpath.basename(d.strip("/")) == name:
                out.append(_join(base, _dir(d.strip("/"))))  # `pkg → lib/pkg`: root `lib`
    for where in _as_list(_get(tool, "setuptools", "packages", "find", "where")):
        out.append(_join(base, where))
    # [tool.poetry] packages = [{include = "pkg", from = "src"}]
    for p in _get(tool, "poetry", "packages") or []:
        if isinstance(p, dict) and isinstance(p.get("from"), str):
            out.append(_join(base, p["from"]))
    # [tool.hatch.build.targets.wheel] packages = ["src/pkg"]; sources = ["src"]
    wheel = _get(tool, "hatch", "build", "targets", "wheel")
    for holder in (wheel, _get(tool, "hatch", "build")):
        for p in _as_list(_get(holder, "packages")):
            out.append(_join(base, _dir(p.strip("/"))))
        sources = _get(holder, "sources")
        for s in (list(sources) if isinstance(sources, dict) else _as_list(sources)):
            out.append(_join(base, s))
    # [tool.pytest.ini_options] pythonpath = ["src", "tests"]
    for p in _as_list(_get(tool, "pytest", "ini_options", "pythonpath")):
        out.append(_join(base, p))
    return out


def _squeezed_names(text: str) -> str:
    """Each line with the whitespace runs before its first `=` / `:` cut to one space, its
    indent kept: configparser's option pattern rescans such a run from every character
    before it, inside one regex call that holds the GIL. What is read here is unchanged: no
    section or option name read holds a space, nor a `package_dir` name."""
    out: list[str] = []
    for line in text.split("\n"):
        cut = min((i for i in (line.find("="), line.find(":")) if i >= 0), default=len(line))
        name = line[:cut].lstrip()
        indent = line[: cut - len(name)]
        out.append(indent + " ".join(name.split()) + line[cut:])
    return "\n".join(out)


def _ini(text: str) -> configparser.RawConfigParser | None:
    cp = configparser.RawConfigParser(strict=False, allow_no_value=True)
    try:
        cp.read_string(_squeezed_names(text))
    except (configparser.Error, ValueError):
        return None
    return cp


def _setup_cfg_roots(base: str, cp: configparser.RawConfigParser) -> list[str]:
    out: list[str] = []
    if cp.has_option("options", "package_dir"):
        for line in (cp.get("options", "package_dir") or "").splitlines():
            if "=" not in line:
                continue
            name, d = (s.strip() for s in line.split("=", 1))
            if name == "":
                out.append(_join(base, d))
            elif posixpath.basename(d.strip("/")) == name:
                out.append(_join(base, _dir(d.strip("/"))))
    if cp.has_option("options.packages.find", "where"):
        out.extend(_join(base, w) for w in _as_list(cp.get("options.packages.find", "where")))
    if cp.has_option("tool:pytest", "pythonpath"):
        out.extend(_join(base, p) for p in _as_list(cp.get("tool:pytest", "pythonpath")))
    return out


def _setup_py_roots(base: str, text: str) -> list[str]:
    out: list[str] = []
    m = _SETUP_PKGDIR_RE.search(text)
    if m:
        for km, vm in re.findall(r"""["']([^"']*)["']\s*:\s*["']([^"']+)["']""", m.group(1)):
            if km == "":
                out.append(_join(base, vm))
            elif posixpath.basename(vm.strip("/")) == km:
                out.append(_join(base, _dir(vm.strip("/"))))
    for w in _SETUP_WHERE_RE.findall(text):
        out.append(_join(base, w))
    return out


def roots_from_manifest(path: str, text: str) -> list[str]:
    """The absolute-specifier roots one manifest declares, repo-relative,
    the manifest's own directory first for a package manifest."""
    base = _dir(path)
    name = posixpath.basename(path)
    out: list[str] = [base] if name in PACKAGE_MANIFESTS else []
    if name == "pyproject.toml":
        data = _toml(text)
        if data is not None:
            out.extend(_pyproject_roots(base, data))
    elif name == "setup.cfg":
        cp = _ini(text)
        if cp is not None:
            out.extend(_setup_cfg_roots(base, cp))
    elif name == "setup.py":
        out.extend(_setup_py_roots(base, text))
    elif name in PYTEST_MANIFESTS:
        cp = _ini(text)
        if cp is not None and cp.has_option("pytest", "pythonpath"):
            out.extend(_join(base, p) for p in _as_list(cp.get("pytest", "pythonpath")))
    # ordered and deduplicated by a dict: a list's `in` per root is quadratic
    return list(dict.fromkeys(r for r in out if ".." not in r.split("/")))


def conftest_roots(present: Iterable[str]) -> list[str]:
    """pytest's rootdir insertion (prepend import mode): the directory of a
    `conftest.py` that is not itself inside a package is on `sys.path`, so
    `from helpers import x` in a test resolves to `tests/helpers.py`."""
    paths = set(present)
    out: list[str] = []
    for p in sorted(paths):
        if posixpath.basename(p) != "conftest.py":
            continue
        d = _dir(p)
        if (f"{d}/__init__.py" if d else "__init__.py") in paths:
            continue
        out.append(d)  # one conftest.py per directory: no duplicates
    return out


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def _target(value: str) -> tuple[str, str] | None:
    m = _TARGET_RE.match(value)
    if not m:
        return None
    return m.group(1), m.group(2) or ""


def _toml_entry_points(path: str, text: str) -> list[EntryPoint]:
    """`[project.scripts]` / `[project.gui-scripts]` / `[project.entry-points.g]`
    by a line scan: the value and its line, tolerant of a manifest tomllib
    rejects."""
    out: list[EntryPoint] = []
    group = ""
    for i, line in enumerate(text.splitlines(), 1):
        tm = _TABLE_RE.match(line)
        if tm:
            table = tm.group(1).strip().strip('"').strip("'")
            if table in _ENTRY_GROUPS:
                group = _ENTRY_GROUPS[table]
            elif table.startswith(_ENTRY_PREFIX):
                group = table[len(_ENTRY_PREFIX):].strip('"').strip("'")
            else:
                group = ""
            continue
        if not group:
            continue
        km = _KV_RE.match(line)
        if not km:
            continue
        name = km.group(1) or km.group(2) or km.group(3) or ""
        tgt = _target(km.group(4) if km.group(4) is not None else km.group(5) or "")
        if name and tgt:
            out.append(EntryPoint(name, group, tgt[0], tgt[1], path, i))
    return out


def _ini_entry_points(path: str, text: str, section: str) -> list[EntryPoint]:
    """setup.cfg `[options.entry_points]` (`console_scripts =` followed by
    indented `name = module:attr` lines) and the ini-in-a-string form
    `setup.py` may carry (`[console_scripts]` headers)."""
    out: list[EntryPoint] = []
    group = ""
    in_section = section == ""
    for i, line in enumerate(text.splitlines(), 1):
        tm = _TABLE_RE.match(line)
        if tm:
            table = tm.group(1).strip()
            if section and table == section:
                in_section, group = True, ""
            elif table in ("console_scripts", "gui_scripts"):
                in_section, group = True, table
            elif section:
                in_section, group = False, ""
            else:
                group = ""
            continue
        if not in_section:
            continue
        stripped = line.strip()
        if stripped.rstrip("=").strip() in ("console_scripts", "gui_scripts") and "=" in stripped:
            group = stripped.rstrip("=").strip()
            rest = stripped.split("=", 1)[1].strip()
            if not rest:
                continue
            stripped = rest
        if not group or "=" not in stripped or stripped.startswith(("#", ";")):
            continue
        name, value = (s.strip() for s in stripped.split("=", 1))
        tgt = _target(value)
        if name and tgt and " " not in name:
            out.append(EntryPoint(name, group, tgt[0], tgt[1], path, i))
    return out


def _setup_py_entry_points(path: str, text: str) -> list[EntryPoint]:
    out: list[EntryPoint] = []
    # Each search and line count goes on from the last list's end: from the top, or into a
    # list already scanned, an unclosed list costs the rest of the file per match.
    j, counted, line = 0, 0, 1
    while (m := _SETUP_EP_RE.search(text, j)) is not None:
        group = m.group(1)
        depth, j = 1, m.end()
        while j < len(text) and depth:
            if text[j] == "[":
                depth += 1
            elif text[j] == "]":
                depth -= 1
            j += 1
        for sm in _SETUP_STR_RE.finditer(text, m.end(), j):
            item = sm.group(1)
            if "=" not in item:
                continue
            name, value = (s.strip() for s in item.split("=", 1))
            tgt = _target(value)
            if name and tgt:
                line += text.count("\n", counted, sm.start())
                counted = sm.start()
                out.append(EntryPoint(name, group, tgt[0], tgt[1], path, line))
    if not out and "[console_scripts]" in text:
        out = _ini_entry_points(path, text, "")
    return out


def entry_points_from_manifest(path: str, text: str) -> list[EntryPoint]:
    """The entry points one manifest declares."""
    name = posixpath.basename(path)
    if name == "pyproject.toml":
        return _toml_entry_points(path, text)
    if name == "setup.cfg":
        return _ini_entry_points(path, text, "options.entry_points")
    if name == "setup.py":
        return _setup_py_entry_points(path, text)
    return []


# ---------------------------------------------------------------------------
# the layout
# ---------------------------------------------------------------------------


def is_manifest_path(path: str) -> bool:
    return posixpath.basename(path) in MANIFEST_NAMES


def layout_from_texts(texts: dict[str, str], present: Iterable[str]) -> Layout:
    """The roots and entry points of a checkout from its manifests' texts
    ({path: text}) and its path set. A manifest under a vendored tree is a
    root for that tree's own files only (`Layout.roots_for`)."""
    roots: dict[str, None] = {}  # ordered and deduplicated: a list's `in` per root is quadratic
    eps: list[EntryPoint] = []
    notes: dict[str, str] = {}
    # shallow manifests first: the repository's own before a fixture package's
    manifests = sorted((p for p in texts if is_manifest_path(p)),
                       key=lambda p: (p.count("/"), p))[:MAX_MANIFESTS]
    for p in manifests:
        text = texts.get(p) or ""
        if not text.strip():
            notes[p] = "empty"
            continue
        roots.update(dict.fromkeys(roots_from_manifest(p, text)))
        eps.extend(entry_points_from_manifest(p, text)[: MAX_ENTRY_POINTS - len(eps)])
    roots.update(dict.fromkeys(conftest_roots(present)))
    return Layout(tuple(roots)[:MAX_ROOTS], tuple(eps), tuple(manifests), notes)


# ---------------------------------------------------------------------------
# facets
# ---------------------------------------------------------------------------


def manifest_facets(path: str, text: str) -> list[Facet]:
    """`entry_point` facets on the manifest, one per declaration at its line."""
    out: list[Facet] = []
    for i, ep in enumerate(entry_points_from_manifest(path, text)):
        out.append(Facet(kind=FACET_KIND, name=ep.name, ordinal=i, line=ep.line, confidence=1.0))
    return out


def module_entry_facets(pf: ParsedFile, layout: Layout | None, *, ordinal: int = 0) -> list[Facet]:
    """`entry_point` facets on the module an entry point targets, at the
    target function's line (1.0) or the module's first line when the
    attribute is not a module-level definition (0.8)."""
    if layout is None or not layout.entry_points or not pf.path.endswith(".py"):
        return []
    from server.code.domain.extract.lang_python import _module_files, _roots_for, module_scope

    scope = module_scope(pf.path)
    by_name = {s.qualified_name: s for s in pf.symbols}
    # the entry points whose `import_candidates("", module, layout.roots)` hold this path:
    # under each root it tries, the path is one of `_module_files` of a stem
    hits: dict[int, EntryPoint] = {}
    for root in _roots_for("", layout.roots):
        prefix = f"{root}/" if root else ""
        if not pf.path.startswith(prefix):
            continue
        rest = pf.path[len(prefix):]
        for suffix in _module_files(""):
            if rest.endswith(suffix):
                hits.update(layout.entry_points_by_stem.get(rest[: -len(suffix)], ()))
    out: list[Facet] = []
    seen: set[str] = set()
    for _i, ep in sorted(hits.items()):
        if ep.name in seen:
            continue
        seen.add(ep.name)
        sym = by_name.get(f"{scope}::{ep.attr}") if ep.attr else None
        line = sym.start_line if sym is not None else 1
        out.append(Facet(kind=FACET_KIND, name=ep.name, ordinal=ordinal + len(out), line=line,
                         confidence=1.0 if sym is not None or not ep.attr else 0.8))
    return out


def script_facets(pf: ParsedFile, *, ordinal: int = 0) -> list[Facet]:
    """`python -m <package>` for a `__main__.py`; `python <path>` for a
    module guarded by `if __name__ == "__main__":`, at the guard's line."""
    out: list[Facet] = []
    if pf.lang != "python":
        return out
    base = posixpath.basename(pf.path)
    if base == "__main__.py":
        pkg = posixpath.basename(_dir(pf.path)) or pf.path[:-3]
        out.append(Facet(kind=FACET_KIND, name=f"python -m {pkg}", ordinal=ordinal, line=1,
                         confidence=1.0))
    tree = pf.tree
    root = getattr(tree, "root_node", None) if tree is not None else None
    if root is None:
        return out
    for child in root.named_children:
        if child.type != "if_statement":
            continue
        cond = child.child_by_field_name("condition")
        if cond is None:
            continue
        text = pf.source[cond.start_byte:cond.end_byte].decode("utf-8", "replace")
        if _MAIN_GUARD_RE.search(text):
            out.append(Facet(kind=FACET_KIND, name=f"python {pf.path}", ordinal=ordinal + len(out),
                             line=child.start_point[0] + 1, confidence=1.0))
            break
    return out


__all__ = [
    "FACET_KIND", "MANIFEST_NAMES", "MAX_MANIFESTS", "PACKAGE_MANIFESTS", "VENDOR_SEGMENTS",
    "EntryPoint", "Layout", "conftest_roots", "entry_points_from_manifest", "is_manifest_path",
    "is_vendored", "layout_from_texts", "manifest_facets", "module_entry_facets",
    "roots_from_manifest", "script_facets",
]

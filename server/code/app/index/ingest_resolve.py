"""What edge resolution reads from the namespace beyond the file: its live paths, the
mirror's text (package manifests, tsconfig, workspaces, Go modules, barrels, Python
roots), each cached and read once per path while in flight."""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

from server.code.app.index.ingest_headers import include_candidates
from server.code.app.index.ingest_parse import (
    _LANE_INCLUDE_LANGS,
    CODE_ISLAND_LANGS,
    LANE_UNIT_LANGS,
    TS_LANGS,
    _lane_module,
)
from server.code.app.ttl_cache import TtlCache
from server.code.domain.extract.treesitter import ParsedFile
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger("loom.code_index.ingest")

_PY_LAYOUT_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}


async def _py_layout(repo: CodeRepo, org: str, ns: str, present: set[str]) -> Any:
    """The namespace's Python layout — the roots its manifests declare (`pyproject.toml`
    / `setup.py` / `setup.cfg` / `pytest.ini` / `tox.ini`, texts from the live chunks
    the way package.json is read) and its entry points — cached ten minutes; None when
    the set is not live. A manifest read before it is indexed (a first backfill) is not
    cached."""
    import time

    from server.code.domain.extract import py_layouts as pl

    if _LIVE_PATHS_CACHE.get((org, ns)) is None:
        return None
    hit = _PY_LAYOUT_CACHE.get((org, ns))
    now = time.monotonic()
    if hit is not None and now - hit[0] < settings.code_ingest_sibling_text_ttl_s:
        return hit[1]
    manifests = sorted(
        (p for p in present if pl.is_manifest_path(p)), key=lambda p: (p.count("/"), p)
    )[: pl.MAX_MANIFESTS]
    texts = {p: await _package_json_text(repo, org, ns, p) for p in manifests}
    layout = pl.layout_from_texts({p: t for p, t in texts.items() if t}, present)
    if all(texts.values()):
        _PY_LAYOUT_CACHE[(org, ns)] = (now, layout)
    return layout


async def _py_roots_for(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    present: set[str],
) -> tuple[str, ...]:
    """The manifest roots this importer may start an absolute specifier
    from (a vendored root only for the files under it)."""
    layout = await _py_layout(repo, org, ns, present)
    return layout.roots_for(pf.path) if layout is not None else ()


_MAX_INCLUDE_PROBES = 3000  # one bloom-filtered membership read per file

# (org, ns) -> (read at, live path set). The probing form — every candidate a file's
# specifiers might name, one `IN` read of at most 500 of them — drops the imports of
# any file with more than about ten. The set is the whole oracle.
_LIVE_PATHS_CACHE: dict[tuple[str, str], tuple[float, set[str]]] = {}


async def _live_paths(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> set[str] | None:
    """The namespace's live paths, cached per process for
    `code_ingest_live_paths_ttl_s`; the file being ingested joins the set
    at once so the files after it resolve to it. None when the repository
    has no set read (a fake in tests): the caller probes instead."""
    import time

    from server.settings import settings as _settings

    reader = getattr(repo, "live_path_set", None)
    if reader is None:
        return None
    key = (org, ns)
    now = time.monotonic()
    hit = _LIVE_PATHS_CACHE.get(key)
    if hit is None or now - hit[0] >= float(_settings.code_ingest_live_paths_ttl_s):
        # One reload for every caller that saw it expire — otherwise concurrent
        # dependents each reload it, and each reload replaces the set the others have
        # just added paths to.
        async def _reload() -> tuple[float, set[str]]:
            fresh = (time.monotonic(), set(await reader(org, ns)))
            _LIVE_PATHS_CACHE[key] = fresh
            return fresh

        hit = cast(tuple[float, set[str]], await single_flight(("live_paths", *key), _reload))
    hit[1].add(pf.path)
    return hit[1]


async def _present_for(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    wanted: list[str],
) -> set[str]:
    """The membership oracle for `wanted`: the live set or, without one, the probing
    membership read."""
    live = await _live_paths(repo, org, ns, pf)
    if live is not None:
        return live
    if not wanted:
        return set()
    return set(await repo.live_paths_present(org, ns, wanted[:_MAX_INCLUDE_PROBES]))


async def _resolve_lane_includes(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> None:
    """A TypeScript import specifier or a Rust `use` path names a repository file only
    through a probing ladder; the namespace's live paths are the oracle, read once per
    file. A specifier nothing indexed satisfies (a package, `std`) is not an edge."""
    if pf.lang in CODE_ISLAND_LANGS:
        await _resolve_code_lane_imports(repo, org, ns, pf)
        return
    if pf.lang == "config" and not pf.includes:
        from server.code.domain.extract.lang_gotemplate import (
            chart_dependency_candidates,
            is_chart_manifest,
        )

        if is_chart_manifest(pf.path):
            # A chart's dependencies are include edges to the dependency charts the
            # repository holds
            wanted = chart_dependency_candidates(pf.path, pf.source.decode("utf-8", "replace"))
            present = await repo.live_paths_present(org, ns, wanted) if wanted else set()
            pf.includes = [c for c in wanted if c in present]
        return
    if pf.lang in TS_LANGS or pf.lang in ("rust", "python") or pf.lang in LANE_UNIT_LANGS:
        await _live_paths(repo, org, ns, pf)  # This file joins the set now
    if not pf.includes:
        return
    if pf.lang == "python":
        await _resolve_py_includes(repo, org, ns, pf)
        return
    if pf.lang in LANE_UNIT_LANGS:
        # A proto import's root is protoc's `-I`, unknown here — the file's directory
        # and every ancestor + the specifier are probed
        unit_lane = _lane_module(pf.lang)
        specs = list(pf.includes)
        present = await _present_for(repo, org, ns, pf, unit_lane.wanted_paths(pf.path, specs))
        pf.includes = unit_lane.resolve_imports(pf.path, specs, present)
        return
    if pf.lang == "shell":
        # A `?cwd:` alternate (root-relative reading of a relative `source`) is an edge
        # only when the namespace holds it
        from server.code.domain.extract.lang_shell_islands import CWD_MARK, settle_cwd_includes

        marked = [i[len(CWD_MARK) :] for i in pf.includes if i.startswith(CWD_MARK)]
        present = await repo.live_paths_present(org, ns, marked) if marked else set()
        pf.includes = settle_cwd_includes(pf.includes, set(present))
        return
    if pf.lang in _LANE_INCLUDE_LANGS:
        await _resolve_simple_lane_includes(repo, org, ns, pf)
        return
    if pf.lang in TS_LANGS:
        from server.code.domain.extract import lang_typescript_imports as lane
    elif pf.lang == "rust":
        from server.code.domain.extract import lang_rust as lane  # type: ignore[no-redef]
    else:
        return
    present = await _present_for(repo, org, ns, pf, lane.wanted_paths(pf.path, pf.includes))
    if pf.lang in TS_LANGS:
        # The governing tsconfig's paths / baseUrl, exact, first; A workspace member's
        # real directory before the guesses
        extra = await _tsconfig_candidates(repo, org, ns, pf, present)
        for spec, cands in (await _workspace_candidates(repo, org, ns, pf, present)).items():
            extra[spec] = [*extra.get(spec, []), *cands]
        resolved = lane.resolve_imports(pf.path, pf.includes, present, extra)
    else:
        resolved = lane.resolve_uses(pf.path, pf.includes, present)  # type: ignore[attr-defined]
    if pf.lang in TS_LANGS:
        # A workspace package's subpath export resolves only through its package.json
        # `exports` table
        resolved.extend(
            p
            for p in await _resolve_package_exports(repo, org, ns, pf.path, pf.includes, present)
            if p not in resolved
        )
    pf.includes = resolved


async def _resolve_py_includes(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> None:
    """An import names a repository module through the interpreter's own ladder (`a.b` → `a/b.py`,
    `a/b/__init__.py` under the importer's ancestors, the root and their `src/`; a relative
    specifier under its package), plus the roots the checkout's manifests declare. A specifier
    nothing indexed satisfies (the standard library, a dependency) is not an edge."""
    from server.code.domain.extract import lang_python as py_lane

    specs = list(pf.includes)
    live = await _live_paths(repo, org, ns, pf)
    roots = await _py_roots_for(repo, org, ns, pf, live) if live is not None else ()
    present = await _present_for(repo, org, ns, pf, py_lane.wanted_paths(pf.path, specs, roots))
    pf.includes = [
        p for p in py_lane.resolve_imports(pf.path, specs, present, roots) if p != pf.path
    ]


async def _resolve_code_lane_imports(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> None:
    """Go import paths inside the repository's modules and Java imports under the file's own source
    root resolve to indexed files through one membership read; everything else names no edge. The Go
    side reads every `go.mod` the namespace holds once (the module index) so a multi-module
    repository's cross-module imports resolve like `go build` resolves them."""
    if pf.lang == "go":
        await _resolve_go_imports(repo, org, ns, pf)
        return
    from server.code.domain.extract.lang_java import (
        java_import_candidates,
        java_imports,
        java_package,
        java_resolve_imports,
        java_source_root,
    )

    specs = java_imports(pf)
    root = java_source_root(pf.path, java_package(pf))
    if not specs or root is None:
        return
    roots = [root]
    wanted = [c for s in specs for c in java_import_candidates(s, roots)][:_MAX_INCLUDE_PROBES]
    present = await repo.live_paths_present(org, ns, wanted) if wanted else set()
    pf.includes = [p for p in java_resolve_imports(specs, roots, set(present)) if p != pf.path]


async def _resolve_go_imports(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> None:
    """Go import resolution for one file: the conventional file names of each in-module
    import probed in one membership read, then one directory listing per import the
    names missed."""
    from server.code.domain.extract.lang_go import (
        go_imports,
        go_package_file,
        go_resolve_imports_indexed,
        go_wanted_paths,
    )

    specs = go_imports(pf)
    if not specs:
        return
    index = await _go_module_index(repo, org, ns)
    if not index:
        pf.includes = []
        return
    wanted = go_wanted_paths(specs, index)[:_MAX_INCLUDE_PROBES]
    present = await repo.live_paths_present(org, ns, wanted) if wanted else set()
    resolved, unresolved = go_resolve_imports_indexed(specs, index, set(present))
    hit_dirs = {p.rsplit("/", 1)[0] for p in resolved}
    for _spec, pkg_dir in unresolved:
        if pkg_dir in hit_dirs:
            continue
        file = go_package_file(await _go_package_listing(repo, org, ns, pkg_dir), pkg_dir)
        if file and file not in resolved:
            resolved.append(file)
            hit_dirs.add(pkg_dir)
    pf.includes = [p for p in resolved if p != pf.path]


_GO_LISTING_CACHE: TtlCache[tuple[str, str, str], list[str]] = TtlCache(
    lambda: settings.code_ingest_sibling_text_ttl_s, maxsize=50_000
)


async def _go_package_listing(repo: CodeRepo, org: str, ns: str, pkg_dir: str) -> list[str]:
    """The listing rung's directory read, cached like the module index — a package is
    imported by many files of one backfill, and the listing rung can fire thousands of
    times on a large monorepo (one read each, the backfill's critical path). A listing taken
    before the package was indexed is empty for the cache's lifetime; the edges-only
    second pass re-derives what the first pass missed."""
    key = (org, ns, pkg_dir)
    hit = _GO_LISTING_CACHE.get(key)
    if hit is not None:
        return hit
    listing = await repo.live_paths_under(org, ns, pkg_dir + "/")
    _GO_LISTING_CACHE.put(key, listing)
    return listing


_GO_MODULE_INDEX_CACHE: TtlCache[tuple[str, str], list[tuple[str, str]]] = TtlCache(
    lambda: settings.code_ingest_sibling_text_ttl_s
)


async def _go_module_index(repo: CodeRepo, org: str, ns: str) -> list[tuple[str, str]]:
    """The namespace's Go module index — every live `go.mod` read once (the same cached live-text
    read used for package.json), cached ten minutes like it. A repository with one `go.mod` at the
    root is the index's one-entry case."""
    from server.code.domain.extract.lang_go import go_module_index

    key = (org, ns)
    hit = _GO_MODULE_INDEX_CACHE.get(key)
    if hit is not None:
        return hit
    paths = await repo.live_paths_named(org, ns, "go.mod")
    texts = {p: await _package_json_text(repo, org, ns, p) for p in paths}
    index = go_module_index(texts)
    if index:
        # An EMPTY index is not cached — a first backfill reaching .go files before any
        # go.mod is indexed would otherwise treat every cross-package import as a
        # library for ten minutes
        _GO_MODULE_INDEX_CACHE.put(key, index)
    return index


_PACKAGE_JSON_CACHE: TtlCache[tuple[str, str, str], str] = TtlCache(
    lambda: settings.code_ingest_sibling_text_ttl_s, maxsize=50_000
)

# The (git_dir, sha) a worker pass is ingesting at. Set, a sibling file's text (a
# package.json, a barrel's re-exports) comes from the mirror at that commit, a few
# milliseconds each, instead of reassembling the file from code_chunks FINAL (~0.33 s
# and ~6 MiB a read, enough to starve the fast tier of the other repositories).
_GIT_TEXT_SOURCE: ContextVar[tuple[str, str] | None] = ContextVar("git_text_source", default=None)


@contextmanager
def git_text_source(git_dir: str, sha: str) -> Iterator[None]:
    """Read sibling texts from `git_dir` at `sha` inside the block; tasks
    started inside inherit it."""
    token = _GIT_TEXT_SOURCE.set((git_dir, sha) if git_dir and sha else None)
    try:
        yield
    finally:
        _GIT_TEXT_SOURCE.reset(token)


async def _live_text(repo: CodeRepo, org: str, ns: str, path: str) -> str:
    """One file's text: the mirror's blob at the pass's commit when a
    `git_text_source` is set and holds the path, else the indexed chunks."""
    src = _GIT_TEXT_SOURCE.get()
    if src is not None:
        from server.code.app.index import gitrepo

        try:
            blob = await gitrepo.show_file(src[0], src[1], path)
        except Exception:  # noqa: BLE001 — a slow or broken mirror falls back
            blob = None
        if blob is not None:
            return blob.decode("utf-8", "replace")
    return await repo.live_text_for_path(org, ns, path)


_TSCONFIG_CACHE: TtlCache[tuple[str, str, str], Any] = TtlCache(
    lambda: settings.code_ingest_sibling_text_ttl_s, maxsize=50_000
)
_WORKSPACE_CACHE: TtlCache[tuple[str, str], dict[str, str]] = TtlCache(
    lambda: settings.code_ingest_sibling_text_ttl_s
)


async def _workspace_registry(
    repo: CodeRepo,
    org: str,
    ns: str,
    present: set[str],
) -> dict[str, str]:
    """{package name: directory} for the namespace's workspace members, from
    `pnpm-workspace.yaml` / the root `package.json` `workspaces` expanded against the
    live paths; cached ten minutes; {} when the repository declares no workspace or the
    set is not live."""
    from server.code.domain.extract import ts_workspace as tw

    if _LIVE_PATHS_CACHE.get((org, ns)) is None:
        return {}
    hit = _WORKSPACE_CACHE.get((org, ns))
    if hit is not None:
        return hit
    yaml_text = (
        await _package_json_text(repo, org, ns, "pnpm-workspace.yaml")
        if "pnpm-workspace.yaml" in present
        else ""
    )
    root_text = (
        await _package_json_text(repo, org, ns, "package.json") if "package.json" in present else ""
    )

    async def _opt(path: str) -> str:
        return await _package_json_text(repo, org, ns, path) if path in present else ""

    globs = tw.workspace_globs(yaml_text, root_text) + tw.extra_manifest_globs(
        await _opt("lerna.json"),
        await _opt("rush.json"),
        await _opt("nx.json"),
    )
    dirs = tw.member_dirs(globs, present)
    texts = {d: await _package_json_text(repo, org, ns, f"{d}/package.json") for d in dirs[:200]}
    reg = tw.registry(texts)
    if reg:  # an empty registry in a first backfill is the manifests not yet indexed, not a fact
        _WORKSPACE_CACHE.put((org, ns), reg)
    return reg


def _registry_dirs(registry: dict[str, str], spec: str) -> list[str]:
    """The registry's directory for a specifier's package, first, then the
    conventional guesses."""
    from server.code.domain.extract.lang_typescript_imports import package_dirs_for
    from server.code.domain.extract.ts_workspace import package_name_of

    exact = registry.get(package_name_of(spec))
    if not exact:
        return package_dirs_for(spec)
    return [exact, *[d for d in package_dirs_for(spec) if d != exact]]


async def _tsconfig_candidates(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    present: set[str],
) -> dict[str, list[str]]:
    """Per import specifier, the repository files the governing tsconfig / jsconfig maps
    it to — `paths` (longest prefix, `*` substituted, fallbacks in order, relative to
    `baseUrl` or the config that wrote the pattern) then `baseUrl` for a bare specifier
    — through the `extends` chain (relative, or a workspace config package). Only under
    the live set (the probing fallback has no config list); {} when no config governs
    the file."""
    from server.code.domain.extract import ts_config as tc
    from server.code.domain.extract.lang_typescript_imports import stem_forms

    if _LIVE_PATHS_CACHE.get((org, ns)) is None:
        return {}
    configs = {p for p in present if p.endswith(tc.CONFIG_NAMES)}
    d = tc.config_dir_for(pf.path, configs)
    cp = tc.config_path_in(d, configs) if d is not None else None
    if cp is None:
        return {}
    key = (org, ns, cp)
    cfg = _TSCONFIG_CACHE.get(key)
    if cfg is None:
        texts: dict[str, str] = {}
        cur: str | None = cp
        reg = await _workspace_registry(repo, org, ns, present)

        def dirs_for(spec: str) -> list[str]:
            return _registry_dirs(reg, spec)

        while cur is not None and cur not in texts and len(texts) < 8:
            texts[cur] = await _package_json_text(repo, org, ns, cur)
            cur = tc.next_extends(cur, texts[cur], dirs_for, present)
        cfg = tc.resolve_config(cp, texts, dirs_for, present)
        if texts.get(cp):  # a config read before it is indexed is not cached
            _TSCONFIG_CACHE.put(key, cfg)
    out: dict[str, list[str]] = {}
    for spec in pf.includes:
        stems = tc.paths_candidates(spec, cfg)
        if stems:
            out[spec] = [f for stem in stems for f in stem_forms(stem)]
    return out


async def _workspace_candidates(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    present: set[str],
) -> dict[str, list[str]]:
    """Per specifier naming a workspace member, the files it may mean under the member's
    REAL directory — the subpath as written and under `src/`, the entry (`src/index`,
    `index`), and what the member's `exports` table maps it to — tried before the
    guesses."""
    from server.code.domain.extract.lang_typescript_imports import exports_candidates, stem_forms
    from server.code.domain.extract.ts_workspace import package_name_of

    reg = await _workspace_registry(repo, org, ns, present)
    if not reg:
        return {}
    out: dict[str, list[str]] = {}
    for spec in pf.includes:
        name = package_name_of(spec)
        d = reg.get(name)
        if d is None:
            continue
        sub = spec[len(name) :].lstrip("/")
        stems = (
            [posixpath.join(d, sub), posixpath.join(d, "src", sub)]
            if sub
            else [
                posixpath.join(d, "src/index"),
                posixpath.join(d, "index"),
            ]
        )
        cands = [f for stem in stems for f in stem_forms(stem)]
        text = await _package_json_text(repo, org, ns, f"{d}/package.json")
        cands.extend(c for c in exports_candidates(spec, d, text) if c not in cands)
        out[spec] = cands
    return out


_IN_FLIGHT: dict[tuple[str, ...], asyncio.Future[Any]] = {}


async def single_flight(key: tuple[str, ...], read: Any) -> Any:
    """One read per key at a time: concurrent dependents of one commit ask for the
    same package.json / barrel text at once, each missing the cache the first has not
    filled yet. The callers share the first one's
    answer; nothing is kept past it (the TTL caches above decide what is kept).

    Every caller, the first included, waits through `shield`: a cancelled caller (a
    `code.refresh` whose wait timed out) must not cancel the read the others share, or
    they raise CancelledError and a worker pass among them ends its supervised loop."""
    running = _IN_FLIGHT.get(key)
    if running is None or running.done() or running.get_loop() is not asyncio.get_running_loop():
        running = asyncio.ensure_future(read())
        _IN_FLIGHT[key] = running
        running.add_done_callback(lambda t: _read_finished(key, t))
    return await asyncio.shield(running)


def _read_finished(key: tuple[str, ...], task: asyncio.Future[Any]) -> None:
    if _IN_FLIGHT.get(key) is task:
        del _IN_FLIGHT[key]
    if not task.cancelled():
        task.exception()  # retrieved: a read every caller left must not log "never retrieved"


async def _package_json_text(repo: CodeRepo, org: str, ns: str, path: str) -> str:
    """The live text of one package.json, cached ten minutes per namespace:
    a backfill reads it once per package, a worker pass a few times."""
    key = (org, ns, path)
    hit = _PACKAGE_JSON_CACHE.get(key)
    if hit is not None:
        return hit
    text = cast(
        str,
        await single_flight(("package", *key), lambda: _live_text(repo, org, ns, path)),
    )
    if text:
        # An empty read (the file not yet indexed in a first backfill) is not a fact to
        # keep — it would poison every dependent of a manifest or component file for
        # ten minutes, the second pass included
        _PACKAGE_JSON_CACHE.put(key, text)
    return text


async def _resolve_package_exports(
    repo: CodeRepo,
    org: str,
    ns: str,
    from_path: str,
    specs: list[str],
    present: set[str],
) -> list[str]:
    """For every package-shaped specifier the ladder left unresolved, find the workspace
    package's package.json among the live paths, read its `exports` table and probe the
    mapped target."""
    from server.code.domain.extract.lang_typescript_imports import (
        exports_candidates,
        unresolved_package_specs,
    )

    pending = unresolved_package_specs(from_path, specs, present)
    if not pending:
        return []
    reg = await _workspace_registry(repo, org, ns, present)
    manifests = [f"{d}/package.json" for spec in pending for d in _registry_dirs(reg, spec)]
    # Under the live set, `present` answers membership itself
    complete = _LIVE_PATHS_CACHE.get((org, ns)) is not None
    live_manifests = (
        {m for m in manifests if m in present}
        if complete
        else await repo.live_paths_present(org, ns, list(dict.fromkeys(manifests)))
    )
    out: list[str] = []
    for spec in pending:
        pkg_dir = next(
            (d for d in _registry_dirs(reg, spec) if f"{d}/package.json" in live_manifests),
            None,
        )
        if pkg_dir is None:
            continue
        text = await _package_json_text(repo, org, ns, f"{pkg_dir}/package.json")
        cands = exports_candidates(spec, pkg_dir, text)
        if not cands:
            continue
        found = (
            {c for c in cands if c in present}
            if complete
            else set(await repo.live_paths_present(org, ns, cands[:64]))
        )
        hit = next((c for c in cands if c in found), None)
        if hit and hit not in out:
            out.append(hit)
    return out


_BARREL_CACHE: TtlCache[tuple[str, str, str], tuple[str, ...]] = TtlCache(
    lambda: settings.code_ingest_sibling_text_ttl_s, maxsize=50_000
)


async def _barrel_targets_of(repo: CodeRepo, org: str, ns: str, path: str) -> tuple[str, ...]:
    """The files one indexed TypeScript module re-exports from, one level,
    cached ten minutes per path (a barrel is read once per backfill)."""
    from server.code.domain.extract.lang_typescript_imports import (
        barrel_targets,
        import_candidates,
        reexport_specs,
    )

    key = (org, ns, path)
    hit = _BARREL_CACHE.get(key)
    if hit is not None:
        return hit
    text = cast(
        str,
        await single_flight(("barrel", *key), lambda: _live_text(repo, org, ns, path)),
    )
    specs = [s for s in reexport_specs(text) if s.startswith(".")]
    targets: tuple[str, ...] = ()
    if specs:
        wanted = [c for s in specs for c in import_candidates(path, s)][:_MAX_INCLUDE_PROBES]
        present = await repo.live_paths_present(org, ns, wanted) if wanted else set()
        targets = tuple(barrel_targets(path, text, set(present)))
    _BARREL_CACHE.put(key, targets)
    return targets


async def _resolve_simple_lane_includes(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> None:
    """An EXTENDS'd module, an included schema, an add_subdirectory, a doc's relative
    link — the includer's directory first, then each ancestor (the C++ ladder), kept
    when the namespace holds the file."""
    wanted: list[str] = []
    for inc in pf.includes:
        wanted.extend(c for c in include_candidates(pf.path, inc) if c not in wanted)
    present: set[str] = set()
    if wanted:
        present = await repo.live_paths_present(org, ns, wanted[:_MAX_INCLUDE_PROBES])
    resolved: list[str] = []
    for inc in pf.includes:
        hit = next((c for c in include_candidates(pf.path, inc) if c in present), None)
        if hit and hit not in resolved and hit != pf.path:
            resolved.append(hit)
    pf.includes = resolved

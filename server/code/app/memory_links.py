"""The code facts a memory's code links are made of and checked against: which trunk
namespace a repository's links live in, the indexed head, the paths a commit range kept,
moved or deleted, and the definitions a file holds at a commit. Those are parsed from the
file in the bare mirror, so they are the commit's own; the index only proposes
candidates."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from server.code.app.index import gitrepo
from server.code.domain.anchor import Definition, fingerprint
from server.code.domain.declaration import NO_SHAPE, Declarations, edit_pattern
from server.code.domain.index.remote import repo_tag
from server.code.domain.index.repos import configured_repos, docs_namespaces, repo_for_namespace
from server.code.store.index import CodeRepo


def trunk_namespace(org: str, namespace: str) -> str:
    """The repository's trunk code namespace for any of its branch namespaces; '' when
    `namespace` indexes no repository."""
    cfg = repo_for_namespace(org, namespace)
    if cfg is None:
        return ""
    return docs_namespaces().get((org, cfg.repo), namespace)


def namespace_for_tags(org: str, tags: list[str]) -> str:
    """The trunk namespace of the repository a `repo:<id>` tag names; '' when none does."""
    wanted = {t for t in tags if t.startswith("repo:")}
    if not wanted:
        return ""
    for cfg in configured_repos():
        if cfg.org == org and repo_tag(cfg.git_remote or cfg.repo) in wanted:
            return trunk_namespace(org, cfg.namespace)
    return ""


def code_namespaces() -> list[str]:
    """Every configured repository branch's code namespace."""
    return sorted({cfg.namespace for cfg in configured_repos()})


def configured_code_namespaces() -> list[list[str]]:
    """Every configured repository branch as [org, code namespace]: two orgs may share
    a namespace name."""
    return [list(r) for r in sorted({(cfg.org, cfg.namespace) for cfg in configured_repos()})]


async def indexed_head(org: str, namespace: str) -> str:
    """The commit the namespace's fast tier has ingested; '' before its first pass."""
    return await CodeRepo().get_state(org, namespace, "git_head")


async def kept_paths(org: str, namespace: str, base: str, head: str) -> dict[str, str] | None:
    """path -> its path at `head` ('' = deleted) for the paths `base..head` changed;
    None when `base` is not an ancestor of `head` (a force-push) or git cannot tell."""
    cfg = repo_for_namespace(org, namespace)
    if cfg is None or not base or not head:
        return None
    try:
        if not await gitrepo.is_ancestor(cfg.git_dir, base, head):
            return None
        return await gitrepo.diff_paths_renamed(cfg.git_dir, base, head)
    except gitrepo.GitError:
        return None


async def descends(org: str, namespace: str, ancestor: str, descendant: str) -> bool:
    """Whether `descendant` has `ancestor` in its history; False for a commit git does
    not know, a GitError when git fails."""
    cfg = repo_for_namespace(org, namespace)
    if cfg is None or not ancestor or not descendant:
        return False
    return bool(await gitrepo.is_ancestor(cfg.git_dir, ancestor, descendant))


async def commits_after(org: str, namespace: str, base: str, head: str) -> list[str]:
    """The commits `base..head`, `head` among them."""
    cfg = repo_for_namespace(org, namespace)
    if cfg is None or not base or not head:
        return []
    out = await gitrepo.run(cfg.git_dir, "rev-list", "--end-of-options", f"{base}..{head}")
    return out.split()


async def edits_of(
    org: str, namespace: str, base: str, head: str, path: str, name: str
) -> list[tuple[str, str]] | None:
    """(commit, its first parent) of each commit of `base..head` that can change whether
    `path` defines the dotted `name` (`declaration.edit_pattern`), oldest first; None
    when `base` is not an ancestor of `head` or git cannot tell."""
    cfg = repo_for_namespace(org, namespace)
    if cfg is None or not base or not head:
        return None
    try:
        if not await gitrepo.is_ancestor(cfg.git_dir, base, head):
            return None
        return await gitrepo.edits_of(cfg.git_dir, base, head, path, edit_pattern(name, path))
    except gitrepo.GitError:
        return None


async def touched_paths(org: str, namespace: str, base: str, head: str) -> set[str] | None:
    """The paths at `head` that `base..head` added or changed (no rename detection: what
    a rename brought in counts as added); None when `base` is not an ancestor of `head`
    or git cannot tell."""
    cfg = repo_for_namespace(org, namespace)
    if cfg is None or not base or not head:
        return None
    try:
        if not await gitrepo.is_ancestor(cfg.git_dir, base, head):
            return None
        entries = await gitrepo.diff_files(cfg.git_dir, base, head)
    except gitrepo.GitError:
        return None
    return {e.path for e in entries if e.status != "D"}


async def renamed_from(
    org: str, namespace: str, base: str, head: str, paths: list[str]
) -> dict[str, str] | None:
    """path at `head` -> its path at `base` for those of `paths` renamed since; None when
    `base` is not an ancestor of `head` or git cannot tell."""
    cfg = repo_for_namespace(org, namespace)
    if cfg is None or not base or not head:
        return None
    try:
        if not await gitrepo.is_ancestor(cfg.git_dir, base, head):
            return None
        return await gitrepo.renamed_from(cfg.git_dir, base, head, paths)
    except gitrepo.GitError:
        return None


async def candidates(
    org: str,
    namespace: str,
    *,
    names: list[str] | None = None,
    qualified_names: list[str] | None = None,
) -> list[Definition]:
    """Indexed tree-sitter definitions by bare name or qualified name: where to look,
    to be confirmed with `parsed`."""
    rows = await CodeRepo().parser_definitions(
        org, namespace, names=names, qualified_names=qualified_names
    )
    unique = {(str(r["path"]), str(r["qualified_name"])): r for r in rows}
    return [
        Definition(
            path=str(r["path"]),
            qualified_name=str(r["qualified_name"]),
            name=str(r.get("name") or ""),
            symbol_id=str(r["symbol_id"]),
        )
        for r in unique.values()
    ]


class ParseFailed(Exception):
    """A file the parser failed on: its missing symbols are no evidence of removal."""


def _parse(path: str, source: bytes) -> tuple[list[Any], Any]:
    """The parser's symbols of `path` and its tree-sitter tree (None without one); a
    parse that failed raises `ParseFailed`."""
    from server.code.app.index.ingest_parse import _parse_for_ingest

    pf, error = _parse_for_ingest(path, source)
    if error:
        raise ParseFailed(f"{path}: {error}")
    return (pf.symbols, pf.tree) if pf is not None else ([], None)


@dataclass(frozen=True)
class FileAt:
    lines: list[str]
    defs: list[Definition]

    def span(self, start: int, end: int) -> str:
        return "\n".join(self.lines[max(0, start - 1) : end])


async def has_commit(org: str, namespace: str, sha: str) -> bool:
    """Whether the repository's mirror holds commit `sha`; one it cannot read raises."""
    cfg = repo_for_namespace(org, namespace)
    if cfg is None:
        raise gitrepo.GitError(f"no repository indexes {namespace}")
    return await gitrepo.has_commit(cfg.git_dir, sha)


async def parsed(
    org: str, namespace: str, commit: str, path: str, *, named: str = ""
) -> FileAt | None:
    """`path` at `commit` from the bare mirror, with the tree-sitter definitions in it
    (with `named`, only those of that bare name, fingerprinted but without rename shapes:
    what a read needs); None when the commit has no such file. A mirror that cannot be
    read, or a file that does not parse, raises: code that cannot be read is not code
    that is gone."""
    data = await _file_at(org, namespace, commit, path)
    if data is None:
        return None
    # parsing and fingerprinting is CPU work: off the event loop
    defs = await asyncio.to_thread(_definitions, path, data, named)
    return FileAt(data.decode("utf-8", "replace").split("\n"), defs)


async def _file_at(org: str, namespace: str, commit: str, path: str) -> bytes | None:
    """`path` at `commit` from the bare mirror; None when the commit has no such file. A
    mirror that cannot be read raises."""
    if not commit or not path:
        return None
    cfg = repo_for_namespace(org, namespace)
    if cfg is None:
        raise gitrepo.GitError(f"no repository indexes {namespace}")
    return await gitrepo.file_at(cfg.git_dir, commit, path)


async def names_at(org: str, namespace: str, commit: str, path: str) -> set[str] | None:
    """The qualified names `path` defines at `commit`, read without fingerprints; None
    when the commit has no such file. A mirror that cannot be read, or a file that does
    not parse, raises."""
    data = await _file_at(org, namespace, commit, path)
    if data is None:
        return None
    symbols, _ = await asyncio.to_thread(_parse, path, data)
    return {s.qualified_name for s in symbols}


def _definitions(path: str, source: bytes, named: str = "") -> list[Definition]:
    """Each symbol of the file, fingerprinted with every overload of its name; with
    `named`, only those of that bare name, and without rename shapes. A parse that
    failed raises `ParseFailed`."""
    symbols, tree = _parse(path, source)
    lines = source.decode("utf-8", "replace").split("\n")
    groups: dict[str, list[Any]] = defaultdict(list)
    for sym in sorted(symbols, key=lambda x: x.start_line):
        if not named or sym.name == named:
            groups[sym.qualified_name].append(sym)
    decls = Declarations.parse(path, source, tree)
    out: list[Definition] = []
    for qn, syms in groups.items():
        starts = [decls.start(s.start_line) if decls else s.start_line for s in syms]
        # hash every declaration of the name in its scopes (overload signatures, repeats,
        # one per `#if` branch), which the index folds or numbers; texts and shapes come
        # from the same families
        at = [s.start_line for s in syms]
        prints = decls.fingerprints(at, syms[0].name, shapes=not named) if decls else None
        if prints:
            body, exact, calls, reads, shown = prints
        else:
            # each declaration's own bytes; its lines when the parser cannot single it out
            texts = [
                (decls.text(s.start_line, s.name) if decls else None)
                or "\n".join(lines[b - 1 : s.end_line])
                for b, s in zip(starts, syms, strict=True)
            ]
            shapes = [
                decls.shape(s.start_line, s.name) if decls and not named else NO_SHAPE for s in syms
            ]
            # each shape over the whole group, '' when any declaration has none
            exact, calls, reads = (
                fingerprint(part) if all(part) else "" for part in zip(*shapes, strict=True)
            )
            body = fingerprint(texts)
            shown = "\n\n".join(
                (decls.shown(s.start_line, s.name) if decls else None) or text
                for s, text in zip(syms, texts, strict=True)
            )
        out.extend(
            Definition(
                path=path,
                qualified_name=qn,
                name=s.name,
                symbol_id=s.id,
                start_line=b,
                end_line=s.end_line,
                body_hash=body,
                shape=exact,
                loose_shapes=(calls, reads),
                kind=s.kind,
                shown=shown,
            )
            for b, s in zip(starts, syms, strict=True)
        )
    return out


async def live_paths(org: str, namespace: str, refs: list[str]) -> dict[str, str]:
    """ref -> the live path it names: itself when indexed, else the one live path whose
    basename it is. Unresolved or ambiguous refs are absent."""
    repo = CodeRepo()
    present = await repo.live_paths_present(org, namespace, refs)
    out = {r: r for r in refs if r in present}
    for ref in refs:
        if ref not in out and "/" not in ref:
            named = await repo.live_paths_named(org, namespace, ref, limit=2)
            if len(named) == 1:
                out[ref] = named[0]
    return out


async def refresh_repos() -> None:
    """Re-read the self-service repositories when their snapshot is stale."""
    from server.code.app.index.repo_registry import refresh_db_repos

    await refresh_db_repos()


_MARK = "memory_links_checked"


async def checked_head(org: str, namespace: str) -> str:
    """The head the namespace's memory links were last checked at."""
    return await CodeRepo().get_state(org, namespace, _MARK)


async def set_checked_head(org: str, namespace: str, head: str) -> None:
    await CodeRepo().set_state(org, namespace, _MARK, head)


_CURSOR_NS = "memory_code_links"


async def link_cursor(name: str) -> str:
    """The memory link worker's cursor `name` ('' before its first pass)."""
    return await CodeRepo().get_state("", _CURSOR_NS, name)


async def set_link_cursor(name: str, value: str) -> None:
    await CodeRepo().set_state("", _CURSOR_NS, name, value)

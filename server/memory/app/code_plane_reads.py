"""Memory reads the code plane serves from: the memories linked to a symbol card and
the directories a user's memories name. The statements are
`server.memory.store.code_plane_reads` and `server.memory.store.code_links`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.code.domain.anchor import Definition, bare_name, local_name
from server.code.domain.declaration import fingerprint_version
from server.identity.domain.read_scope import ReadFilters
from server.memory.store import code_links as links_store
from server.memory.store import code_plane_reads as reads


async def lesson_namespaces(org: str, code_namespace: str, extra: Sequence[str]) -> list[str]:
    """Whose linked memories a code read shows: the namespaces the call names (over MCP
    the caller's memory home by default) and the repository's trunk code namespace (its
    team rules), each only when the caller may read it; with no caller, the deployment's
    default namespace. Readable alone is not shown."""
    from server.auth.authz import row_in_scope
    from server.auth.context import current_auth
    from server.code.app.memory_links import trunk_namespace
    from server.operations.base import Role
    from server.settings import settings

    own = [settings.default_namespace] if current_auth() is None else []
    candidates = [*own, *extra, trunk_namespace(org, code_namespace) or code_namespace]
    return [
        ns
        for ns in dict.fromkeys(n for n in candidates if n)
        if await row_in_scope(org, ns, Role.VIEWER)
    ]


def _named(qualified_name: str, defs: list[Definition]) -> list[Definition]:
    """The definitions among `defs` (one file's) that `qualified_name`, in either tier's
    form, names: the same name, else the longest name inside its module it ends with
    (the tiers may name the module differently). Namespace and enclosing type count."""
    q = qualified_name.replace("::", ".")
    same = [d for d in defs if d.qualified_name.replace("::", ".") == q]
    if same:
        return same
    ends = [
        (len(local), d)
        for d in defs
        if q == (local := local_name(d.qualified_name, d.path)) or q.endswith("." + local)
    ]
    best = max((n for n, _ in ends), default=0)
    return [d for n, d in ends if n == best]


async def lessons_for_symbol(
    org: str,
    code_namespace: str,
    *,
    qualified_name: str,
    paths: Sequence[str],
    namespaces: Sequence[str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Served memories of `namespaces` linked to this symbol, current ones first. The
    links live on the repository's trunk; each is matched to the definition the file at
    `paths` holds at the read namespace's indexed head (by its identity, or one it had
    before a move: a branch may not have the moves), and `code_state`
    compares that definition's fingerprint with the one the memory was written
    against. A link trunk lost still counts here: this branch may have the code."""
    from server.code.app import memory_links as code
    from server.memory.app.code_link_reads import current_links

    trunk = code.trunk_namespace(org, code_namespace) or code_namespace
    stored = await links_store.for_code(
        org, [trunk], paths=list(paths), namespaces=list(namespaces), or_former_paths=True
    )
    links = [
        a
        for a in await current_links(org, stored)
        if a["state"] != "pending" and a["qualified_name"]
    ]
    if not links:
        return []
    head = await code.indexed_head(org, code_namespace)
    here: dict[tuple[str, str], Definition] = {}
    for p in dict.fromkeys(paths):
        f = await code.parsed(org, code_namespace, head, p, named=bare_name(qualified_name))
        for d in _named(qualified_name, f.defs if f else []):
            here.setdefault((d.path, d.qualified_name), d)
    matched: list[tuple[dict[str, Any], str, str]] = []
    for a in links:
        # where trunk has it now, where it was before a move (latest first), or where the
        # memory was anchored: a branch may have any of trunk's renames, or none
        current = here.get((str(a["path"]), str(a["qualified_name"])))
        anchored = here.get((str(a.get("anchored_path") or ""), str(a.get("anchored_name") or "")))
        was = None if current else _former_here(a, here)
        now = current or (was[0] if was else None) or anchored
        if now is None:
            continue  # another symbol of the file, or code this branch does not have
        state = _lesson_state(
            a,
            now,
            at_current=now is current,
            at_anchor=now is anchored,
            on_trunk=code_namespace == trunk,
            baseline=was[1] if was else "",
        )
        # a correction only for the definition it was judged against
        judged = state == "changed" and a.get("judged_hash") == now.body_hash
        matched.append((a, state, str(a.get("suggestion") or "") if judged else ""))
    served = await reads.served_excerpts(org, [str(a["memory_id"]) for a, _, _ in matched])
    rows = {str(r["mid"]): r for r in served}
    out: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    for a, state, suggestion in matched:
        row = rows.get(str(a["memory_id"]))
        if row is None or row["mid"] in out:
            continue
        rank = (state == "changed", not row.get("pinned"), -float(row.get("confidence") or 0))
        out[row["mid"]] = (
            rank,
            {
                "memory_key": str(row["memory_key"]),
                "namespace": str(row["namespace"]),
                "excerpt": str(row["excerpt"]),
                "code_state": state,
                "changed_since": str(a.get("changed_since") or "") if state == "changed" else "",
                "suggestion": suggestion,
            },
        )
    return [lesson for _, lesson in sorted(out.values(), key=lambda x: x[0])][:limit]


def _former_here(
    a: dict[str, Any], here: dict[tuple[str, str], Definition]
) -> tuple[Definition, str] | None:
    """The definition at the latest former identity of `a` the read found, with the
    baseline the link had there."""
    former = zip(
        a.get("former_paths") or [],
        a.get("former_names") or [],
        a.get("former_hashes") or [],
        strict=True,
    )
    for path, name, baseline in reversed(list(former)):
        if (d := here.get((path, name))) is not None:
            return d, baseline
    return None


def _lesson_state(
    a: dict[str, Any],
    now: Definition,
    *,
    at_current: bool,
    at_anchor: bool,
    on_trunk: bool,
    baseline: str = "",
) -> str:
    """The code state of link `a` for the definition `now` a read found it at: where
    trunk has it now (`at_current`), where it was before a move (with the `baseline` it
    had there), or where the memory was written against (`at_anchor`)."""
    stored = str(a["state"]) if a["state"] in ("changed", "moved") else "fresh"
    if a.get("carried") and not at_anchor:
        return "changed"  # a renamed class carried it: what surrounds it is not the original
    if a.get("fingerprint_version") != fingerprint_version():
        return stored  # hashed under other rules: compared once the worker re-hashed it
    if on_trunk and at_current and now.body_hash == a.get("checked_hash"):
        return stored  # trunk's definition as the worker last checked it
    # compare with the baseline; a branch that still has the code under its anchored
    # name may also match the anchored text
    linked = {str(a.get("body_hash") or ""), baseline}
    if at_anchor:
        linked.add(str(a.get("anchored_hash") or ""))
    linked.discard("")
    if linked and now.body_hash and now.body_hash not in linked:
        return "changed"
    return "moved" if on_trunk and a["state"] == "moved" else "fresh"


async def user_file_dirs(
    org: str, user_id: str, namespaces: Sequence[str] | None, *, scope: ReadFilters
) -> list[str]:
    """The parent directories of the files the user's live memories in
    `namespaces` (None: every namespace) and inside `scope` name, in path order,
    each once."""
    dirs: dict[str, None] = {}
    for f in await reads.user_file_paths(org, user_id, namespaces, scope=scope):
        dirs.setdefault(f.rsplit("/", 1)[0] if "/" in f else f, None)
    return list(dirs)

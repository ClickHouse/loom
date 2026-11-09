"""Memory reads the code plane serves from: the memories linked to a symbol card, the
decisions that govern a set of files, and the directories a user's memories name. The
statements are `server.memory.store.code_plane_reads`, `server.memory.store.code_links`
and those of `memory.for_file`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from server.code.domain.anchor import Definition, bare_name, local_name
from server.code.domain.declaration import fingerprint_version
from server.code.domain.extract import bounded_parse
from server.identity.domain.read_scope import ReadFilters
from server.memory.domain.code_change import worst
from server.memory.domain.decision_schema import CONSTRAINT_TAG
from server.memory.store import code_links as links_store
from server.memory.store import code_plane_reads as reads
from server.memory.store.code_links import LinkRow
from server.settings import settings


async def lesson_namespaces(org: str, code_namespace: str, extra: Sequence[str]) -> list[str]:
    """Whose linked memories a code read shows: the namespaces the call names (over MCP
    the caller's memory home by default) and the repository's trunk code namespace (its
    team rules), each only when the caller may read it; with no caller, the deployment's
    default namespace. Readable alone is not shown."""
    from server.auth.context import current_auth
    from server.code.app.memory_links import trunk_namespace
    from server.memory.app.code_link_reads import readable_namespaces
    from server.settings import settings

    own = [settings.default_namespace] if current_auth() is None else []
    candidates = [*own, *extra, trunk_namespace(org, code_namespace) or code_namespace]
    names = list(dict.fromkeys(n for n in candidates if n))
    seen = await readable_namespaces(org, set(names))
    return [ns for ns in names if ns in seen]


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
    """The served memories of `namespaces` linked to this symbol, current ones first,
    each with the state of the symbol's code on the branch read (trunk's state where
    the branch's files cannot be read here)."""
    from server.code.app import memory_links as code
    from server.memory.app.code_link_reads import current_links

    if not namespaces:
        return []
    trunk = code.trunk_namespace(org, code_namespace) or code_namespace
    stored, head = await asyncio.gather(
        links_store.for_paths(
            org, [trunk], list(paths), include_former=True, namespaces=list(namespaces)
        ),
        code.indexed_head(org, code_namespace),
    )
    links = [
        a
        for a in await current_links(org, stored)
        if a["state"] != "pending" and a["qualified_name"]
    ]
    if not links:
        return []
    here = await _definitions_at(org, code_namespace, head, paths, qualified_name)
    if here is None:
        matched = _as_trunk_has_it(links, qualified_name)
    else:
        matched = _matched(links, here, on_trunk=code_namespace == trunk)
    served = await reads.served_excerpts(org, namespaces, [a["memory_id"] for a, _, _ in matched])
    rows = {str(r["mid"]): r for r in served}
    out: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    for a, state, suggestion in matched:
        row = rows.get(a["memory_id"])
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
                "changed_since": a["changed_since"] if state == "changed" else "",
                "suggestion": suggestion,
            },
        )
    return [lesson for _, lesson in sorted(out.values(), key=lambda x: x[0])][:limit]


# Each path's read keeps this many rows.
# not a setting: memory.for_file's own `top_k` ceiling (`le` on its input; a test pins it)
PER_PATH_ROWS = 50


class DecisionRows(NamedTuple):
    """What `decisions_for_paths` read: the rows it serves, the rows the cap left out,
    how many paths' reads reached PER_PATH_ROWS (more rows may match there), how many
    paths' code-links reads failed (rows reached only by a link are missing there), and
    whether the caller was refused the trunk namespace (nothing was read)."""

    served: list[dict[str, Any]]
    left_out: list[dict[str, Any]]
    paths_full: int = 0
    links_skipped: int = 0
    refused: bool = False


async def decisions_for_paths(
    org: str,
    trunk_ns: str,
    paths: Sequence[str],
    *,
    limit: int,
    first: Callable[[dict[str, Any]], bool] | None = None,
) -> DecisionRows:
    """The live (active or conflicted) decision and constraint rows of the trunk code
    namespace `trunk_ns` whose `files[]` or code links touch `paths`, at most `limit`
    served and the rest left out. The rows `first` admits rank ahead, then constraints,
    then pinned rows, then by confidence. Each is a `memory.for_file` hit with its
    `memory_id`, `code_state` the worst over its links on every path, `line` (the
    pre-edit block's line, the value cut) and `text` (the value as that read holds it:
    cut at its ceiling when `value_truncated`). Each path's read keeps its top
    PER_PATH_ROWS rows. `refused`, and nothing read, when the caller may not read
    `trunk_ns`."""
    from server.memory.api.models_for_file import MemoryForFileInput
    from server.memory.app.code_link_reads import readable_namespaces
    from server.memory.app.for_file import listing_line, listing_rows, value_text

    if not trunk_ns or not paths:
        return DecisionRows([], [])
    if trunk_ns not in await readable_namespaces(org, {trunk_ns}):
        return DecisionRows([], [], refused=True)
    # One pre-edit read per path. The read follows code links only into the trunk an
    # extra namespace names, so the trunk is named there too (it is not read twice).
    per_path = await asyncio.gather(
        *(
            listing_rows(
                MemoryForFileInput(
                    org=org,
                    namespace=trunk_ns,
                    path=p,
                    top_k=PER_PATH_ROWS,
                    extra_namespaces=[trunk_ns],
                ),
                whole_only=True,
            )
            for p in dict.fromkeys(paths)
        )
    )
    rows: dict[str, dict[str, Any]] = {}  # by memory_id: one row found on several paths
    for hits, _skipped in per_path:
        for hit in hits:
            row = rows.setdefault(hit["memory_id"], hit)
            row["code_state"] = worst([row["code_state"], hit["code_state"]])
    found = [r | {"line": listing_line(r), "text": value_text(r)} for r in rows.values()]
    found.sort(
        key=lambda r: (
            first is not None and not first(r),
            CONSTRAINT_TAG not in r["tags"],
            not r["pinned"],
            -r["confidence"],
        )
    )
    return DecisionRows(
        found[:limit],
        found[limit:],
        paths_full=sum(1 for hits, _ in per_path if len(hits) >= PER_PATH_ROWS),
        links_skipped=sum(1 for _, skipped in per_path if skipped),
    )


async def _definitions_at(
    org: str, code_namespace: str, head: str, paths: Sequence[str], qualified_name: str
) -> dict[tuple[str, str], Definition] | None:
    """The definitions `qualified_name` names in `paths` at `head`, by identity; None
    where the files cannot be read here: no git mirror holding `head` (LOOM_GIT_HISTORY=none,
    a host still cloning or fetching it, a PR head it lacks), one that fails, or a read and
    parse slower than `memory_code_plane_parse_timeout_s`."""
    from server.code.app import memory_links as code
    from server.code.app.index import gitrepo

    if not await code.mirror_here(org, code_namespace, head):
        return None
    named = bare_name(qualified_name)
    # a parse waiting behind another one gives up with this read (its threads copy this)
    lock_wait = bounded_parse.LockWait(settings.memory_code_plane_parse_timeout_s)
    token = bounded_parse.LOCK_WAIT.set(lock_wait)
    try:
        parse = asyncio.gather(
            *(code.parsed(org, code_namespace, head, p, named=named) for p in dict.fromkeys(paths))
        )
    finally:
        bounded_parse.LOCK_WAIT.reset(token)
    try:
        files = await asyncio.wait_for(parse, settings.memory_code_plane_parse_timeout_s)
    except (gitrepo.GitError, code.ParseFailed, TimeoutError):
        return None
    if lock_wait.gave_up:  # a parser may have taken the refusal for the file's own answer
        return None
    here: dict[tuple[str, str], Definition] = {}
    for f in files:
        for d in _named(qualified_name, f.defs if f else []):
            here.setdefault((d.path, d.qualified_name), d)
    return here


def _matched(
    links: list[LinkRow], here: dict[tuple[str, str], Definition], *, on_trunk: bool
) -> list[tuple[LinkRow, str, str]]:
    """(link, code state, suggestion) of each link whose definition the branch has."""
    out = []
    for a in links:
        # where trunk has it now, where it was before a move (latest first), or where the
        # memory was anchored: a branch may have any of trunk's renames, or none
        current = here.get((a["path"], a["qualified_name"]))
        anchored = here.get((a["anchored_path"], a["anchored_name"]))
        was = None if current else _former_here(a, here)
        now = current or (was[0] if was else None) or anchored
        if now is None:
            continue  # another symbol of the file, or code this branch does not have
        state = _lesson_state(
            a,
            now,
            at_current=now is current,
            at_anchor=now is anchored,
            on_trunk=on_trunk,
            baseline=was[1] if was else "",
        )
        # `current` serves a correction only for trunk's checked code: the branch must have it
        judged = state == "changed" and now.body_hash == a["checked_hash"]
        out.append((a, state, a["suggestion"] if judged else ""))
    return out


def _as_trunk_has_it(links: list[LinkRow], qualified_name: str) -> list[tuple[LinkRow, str, str]]:
    """(link, code state, suggestion) of each link to the symbol, as trunk's last check
    left it: what is served where the branch's own files cannot be read. A link trunk
    lost is left out, since nothing here shows the code exists."""
    live = [a for a in links if a["state"] in ("fresh", "moved", "changed")]
    defs = [
        Definition(a["path"], a["qualified_name"], bare_name(a["qualified_name"])) for a in live
    ]
    named = {(d.path, d.qualified_name) for d in _named(qualified_name, defs)}
    out = []
    for a in live:
        if (a["path"], a["qualified_name"]) in named:
            out.append((a, a["state"], a["suggestion"] if a["state"] == "changed" else ""))
    return out


def _former_here(
    a: LinkRow, here: dict[tuple[str, str], Definition]
) -> tuple[Definition, str] | None:
    """The definition at the latest former identity of `a` the read found, with the
    baseline the link had there."""
    for f in reversed(a["former"]):
        if (d := here.get((f["path"], f["name"]))) is not None:
            return d, f["hash"]
    return None


def _lesson_state(
    a: LinkRow,
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
    stored = a["state"] if a["state"] in ("changed", "moved") else "fresh"
    if a["carried"] and not at_anchor:
        return "changed"  # a renamed class carried it: what surrounds it is not the original
    if a["fingerprint_version"] != fingerprint_version():
        return stored  # hashed under other rules: compared once the worker re-hashed it
    if on_trunk and at_current and now.body_hash == a["checked_hash"]:
        return stored  # trunk's definition as the worker last checked it
    # compare with the baseline; a branch that still has the code under its anchored
    # name may also match the anchored text
    linked = {a["body_hash"], baseline}
    if at_anchor:
        linked.add(a["anchored_hash"])
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

"""A code/tracker tool's `namespace` that names no namespace.

A model may pass a repository-ish name (`namespace: "clickhouse"`) to a code
or tracker tool. Answering that with the access refusal ("No access: … claim
the namespace in the dashboard") reads as a grant problem and sends the model looking
elsewhere, so:

- A name that is a repository ("ClickHouse/ClickHouse"), its name
  ("clickhouse") or a code namespace without its prefix resolves to that
  repository's code namespace. The call runs there, and the result says so
  (`namespace_resolved`).
- Any other unknown name gets "no namespace 'X'" and the nearest namespaces
  the caller can read, never the claim text.
- The claim text stays for a real refusal on a namespace that exists.

Pure except `resolve`, which reads the process's configured repositories and
tracker namespaces (no ClickHouse read).
"""

from __future__ import annotations

import difflib
from contextvars import ContextVar
from typing import Any

UNKNOWN_NAMESPACE = "UNKNOWN_NAMESPACE"

# the {from, to} of this request's resolution, attached to its result
RESOLVED: ContextVar[dict[str, str] | None] = ContextVar("loom_namespace_resolved", default=None)


def _norm(text: str) -> str:
    return str(text or "").strip().strip("/").lower()


def known_namespaces(org: str) -> dict[str, str]:
    """namespace → its repository (owner/name), for every configured code
    repository and tracker namespace of `org`."""
    from server.code.domain.index.repos import config_entries
    from server.tracker.domain.namespaces import configured_namespaces

    out: dict[str, str] = {}
    for r in config_entries():
        if not org or r.org == org:
            out[r.namespace] = r.repo
    for ns, t in configured_namespaces().items():
        if not org or t.org == org:
            out.setdefault(ns, (t.repos or [""])[0])
    return out


def match(name: str, known: dict[str, str], *, default_branch: set[str] | None = None) -> str:
    """The namespace `name` means, or "": an exact namespace, then a
    repository (`owner/name`), then a repository's name or a code
    namespace without its prefix. Several namespaces for one repository
    (release branches): the default branch's, else the shortest name."""
    n = _norm(name)
    if not n:
        return ""
    for ns in known:
        if _norm(ns) == n:
            return ns
    cands: list[str] = []
    for ns, repo in known.items():
        r = _norm(repo)
        short = r.rsplit("/", 1)[-1]
        if n in (r, short) or _norm(ns) in (f"code-{n}", f"qa-{n}", f"tracker-{n}"):
            cands.append(ns)
    if not cands:
        return ""
    code = [c for c in cands if c.startswith("code-")] or cands
    main = [c for c in code if c in (default_branch or set())]
    return sorted(main or code, key=lambda c: (len(c), c))[0]


def suggest(name: str, readable: list[str], *, limit: int = 3) -> list[str]:
    """The readable namespaces nearest `name`: a substring match first, then
    difflib's close matches, then the first readable ones (the checkout's
    code scope leads `readable`)."""
    n = _norm(name)
    sub = [ns for ns in readable if n and n in _norm(ns)]
    close = difflib.get_close_matches(n, [_norm(r) for r in readable], n=limit, cutoff=0.5)
    by_norm = {_norm(r): r for r in readable}
    out = list(dict.fromkeys([*sub, *[by_norm[c] for c in close if c in by_norm], *readable]))
    return out[:limit]


def suggestable(org: str, can_access: Any) -> list[str]:
    """The namespaces a refusal may name to this caller: the checkout's own
    code scope first, then every namespace the token names, then the
    standard (not restricted) code and tracker namespaces. A restricted
    namespace is never named to a caller outside it."""
    from server.code.domain.index.repos import config_entries
    from server.code.domain.scope import current_code_scope
    from server.tracker.domain.namespaces import configured_namespaces

    out: list[str] = []
    scope = current_code_scope()
    if scope and (not org or scope[0] == org):
        out.append(scope[1])
    for r in config_entries():
        if org and r.org != org:
            continue
        if can_access(org, r.namespace) or str(r.classification or "standard") != "restricted":
            out.append(r.namespace)
    for ns, t in configured_namespaces().items():
        if org and t.org != org:
            continue
        if can_access(org, ns) or not t.restricted:
            out.append(ns)
    return list(dict.fromkeys(out))


def default_branches(org: str) -> set[str]:
    """The code namespaces that index their repository's default branch."""
    from server.code.domain.index.repos import config_entries

    return {
        r.namespace
        for r in config_entries()
        if (not org or r.org == org) and str(r.git_branch or "") in ("", "main", "master")
    }


def unknown_result(name: str, suggestions: list[str]) -> dict[str, Any]:
    """The tool result for a namespace no repository or tracker has."""
    hint = f"did you mean {' / '.join(repr(s) for s in suggestions)}? " if suggestions else ""
    msg = (
        f"no namespace {name!r}: no indexed repository or tracker record has that name. "
        f"{hint}Omit `namespace` to use this checkout's repository, or pass one of those."
    )
    body = {"error": msg, "code": UNKNOWN_NAMESPACE, "namespace": name, "did_you_mean": suggestions}
    return {"content": [{"type": "text", "text": msg}], "isError": True, "structuredContent": body}

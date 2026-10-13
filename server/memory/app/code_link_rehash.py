"""Re-hashing code-memory links after the fingerprint rules change."""

from __future__ import annotations

from typing import Any

from server.code.app import memory_links as code
from server.code.domain.declaration import fingerprint_version
from server.memory.app import code_link_policy as policy
from server.memory.app import code_link_repo as repo
from server.memory.store import code_links as store
from server.memory.store.code_links import Former, LinkRow

# links re-fingerprinted per write after the fingerprint rules or grammars changed
_REFINGERPRINT_PAGE = 500


async def refingerprint(org: str, cns: str, files: repo.Files, result: repo.PassResult) -> None:
    """Re-hash the links hashed under other rules or grammars than these, a page at a
    time, each page written as it is done; a link whose code does not parse stays due
    for the next pass."""
    version = fingerprint_version()
    unparsed = 0  # links whose code did not parse: they stay due, first in key order
    while rows := await store.stale_fingerprints(
        org, cns, version, limit=_REFINGERPRINT_PAGE, offset=unparsed
    ):
        rehashed = []
        for a in rows:
            try:
                fields = await _rehashed(a, files) if a["qualified_name"] else {}
            except code.ParseFailed:
                unparsed += 1
                continue
            rehashed.append((a, fields, _owes(a, fields)))
        revised = await policy.revised_namespaces([a for a, _, owed in rehashed if owed])
        out = []
        for a, fields, owed in rehashed:
            if owed or "state" in fields:
                fields["policy_due"] = int(owed and (a["org"], a["namespace"]) in revised)
            out.append(
                store.link_row(a, follows=a["version"], fingerprint_version=version, **fields)
            )
        await store.insert(out)
        result.rechecked += len(out)
        if len(rows) < _REFINGERPRINT_PAGE:
            break


async def _rehashed(a: LinkRow, files: repo.Files) -> dict[str, Any]:
    """A symbol link's hashes, former baselines included, re-hashed under the current
    rules from the commits they were taken at, its judgement dropped, and its state set
    by what these rules see between the checked text and the baseline. No hash is ever
    compared with one made under other rules (`docs/concepts/memory.md`)."""

    async def read_at(commit: str, path: str, qualified_name: str) -> tuple[str, str] | None:
        """The definition's (hash, exact shape) at `commit`, ('', '') when it is not there;
        None when the mirror lost the commit."""
        if not commit or not await files.has(commit):
            return None
        found = await files.defs(commit, path, qualified_name)
        return (found[0].body_hash, found[0].shape.exact) if found else ("", "")

    link = repo.link_of(a)
    anchored = await read_at(a["anchored_commit"], link.anchored_path, link.anchored_name)
    checked = await read_at(a["checked_commit"], link.read_path, link.qualified_name)
    out: dict[str, Any] = {
        "anchored_hash": anchored[0] if anchored else "",
        "checked_hash": checked[0] if checked else "",
        "judged_hash": "",
        "former": [
            f | {"hash": found[0] if (found := await read_at(*_baseline_of(f))) else ""}
            for f in a["former"]
        ],
    }
    # a check that matched the baseline read its text; else the text the baseline was
    # taken from, while these rules still see the rename that moved it as one
    rebased = await read_at(*baseline_source(a)) if a["baseline_commit"] else None
    if link.matched_baseline() and checked is not None and _agree(anchored, checked):
        out["body_hash"] = checked[0] or out["anchored_hash"]
    elif rebased is not None and rebased[0] and _agree(anchored, rebased):
        out["body_hash"] = rebased[0]
    else:
        out |= {"body_hash": out["anchored_hash"] or a["body_hash"], "baseline_commit": ""}
    baseline = out["body_hash"]
    if a["state"] in ("fresh", "moved") and out["checked_hash"] not in ("", baseline):
        # these rules see a change the old ones did not
        out |= {"state": "changed", "changed_since": a["checked_commit"]}
    elif a["state"] == "changed" and not link.carried and out["checked_hash"] == baseline != "":
        # nor one they did: the checked text is the anchored one under these rules
        state = "moved" if link.moved_from_anchor(link.read_path, link.qualified_name) else "fresh"
        out |= {"state": state, "changed_since": "", "suggestion": ""}
    return out


def baseline_source(a: LinkRow) -> tuple[str, str, str]:
    """(commit, path, qualified name) of the text `a`'s baseline hashes: the anchor,
    unless a rename moved the baseline onto the renamed text."""
    if a["baseline_commit"]:
        return a["baseline_commit"], a["baseline_path"], a["baseline_name"]
    return a["anchored_commit"], a["anchored_path"], a["anchored_name"]


def _baseline_of(f: Former) -> tuple[str, str, str]:
    """(commit, path, qualified name) of the text former identity `f`'s baseline hashes."""
    return f["baseline_commit"], f["baseline_path"], f["baseline_name"]


def _owes(a: LinkRow, fields: dict[str, Any]) -> bool:
    """A re-hash leaves link `a` due for its policy, as a recheck would: it newly reads
    changed, or stays changed and was judged (a re-hash drops every judgement)."""
    if fields.get("state", a["state"]) != "changed":
        return False
    return "state" in fields or (bool(fields) and bool(a["judged_hash"]))


def _agree(anchored: tuple[str, str] | None, checked: tuple[str, str]) -> bool:
    """Whether the checked text is still the anchored one under the current rules: the
    same hash or the same rename shape. True when either side cannot be read."""
    if not checked[0] or anchored is None:
        return True
    return anchored[0] == checked[0] or anchored[1] == checked[1] != ""

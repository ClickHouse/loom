"""A namespace's code-change policy, run on the links a change left due: `revise`
asks the judge whether a changed claim still holds."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from server.memory.app import code_link_judge
from server.memory.app import code_link_repo as repo
from server.memory.app.code_link_reads import (
    SERVED,
    inputs_now,
)
from server.memory.domain import code_change
from server.memory.domain.code_change import REVISE
from server.memory.store import code_links as store
from server.memory.store.code_links import LinkRow

# The links a policy updated and those it left due, by (path, qualified_name).
_Revised = tuple[dict[tuple[str, str], LinkRow], set[tuple[str, str]]]


async def revised_namespaces(links: list[LinkRow]) -> set[tuple[str, str]]:
    """The (org, namespace) pairs among `links`' memories whose policy is `revise`."""
    by_org: dict[str, set[str]] = defaultdict(set)
    for a in links:
        by_org[a["org"]].add(a["namespace"])
    out: set[tuple[str, str]] = set()
    for org, nss in by_org.items():
        policies = await store.code_change_policies(org, sorted(nss))
        out |= {(org, ns) for ns in nss if code_change.policy_of(policies.get(ns, "")) == REVISE}
    return out


async def apply_policies(
    org: str, cns: str, head: str, files: repo.Files, budget: repo.Budget, result: repo.PassResult
) -> None:
    """Run the policy of each memory a change in `cns` left due and clear the marks it
    settled; what fails or is past the judgement budget stays due."""
    # a page a few times the budget: what is left over is read by the next pass
    due = await store.policy_due(org, cns, limit=4 * budget.judgements + 100)
    if not due:
        return
    links: dict[str, list[LinkRow]] = defaultdict(list)
    for a in await store.for_memories(org, due):
        if a["state"] != "pending":
            links[a["memory_id"]].append(a)
    heads = {str(h["mid"]): h for h in await store.memory_heads(org, due)}
    policies = await store.code_change_policies(
        org, sorted({h["namespace"] for h in heads.values()})
    )
    rows: list[LinkRow] = []
    for mid in due:
        try:
            row = heads.get(mid)
            if row is None or row.get("tombstoned_at") is not None:
                rows += store.tombstones(links[mid])  # the memory is gone: so are its links
                continue
            want = inputs_now(org, row)
            rows += store.tombstones([a for a in links[mid] if a["inputs_hash"] != want])
            mine = [
                a for a in links[mid] if a["inputs_hash"] == want and a["code_namespace"] == cns
            ]
            out: _Revised | None = {}, set()
            if mine:
                policy = code_change.policy_of(policies.get(str(row["namespace"]), ""))
                out = await _revise(org, row, mine, policy, head, files, budget, result)
            if out is None:
                continue  # deferred: stays due
            updated, left = out
            rows += [
                store.link_row(updated.get(k, a), follows=a["version"], policy_due=int(k in left))
                for a in mine
                for k in [(a["path"], a["qualified_name"])]
            ]
        except Exception as e:  # noqa: BLE001 — one memory's failure never stops the pass
            result.errors.append(f"policy {org}/{mid}: {e}")
    await store.insert(rows)


async def _revise(
    org: str,
    row: dict[str, Any],
    links: list[LinkRow],
    policy: str,
    head: str,
    files: repo.Files,
    budget: repo.Budget,
    result: repo.PassResult,
) -> _Revised | None:
    """The `revise` policy for one memory: the links it updated and those it left due
    (past the budget), by (path, qualified_name); None when the whole memory is deferred
    to the next pass. Each changed definition is judged on its own."""
    changed = [
        a for a in links if a["state"] == "changed" and a["qualified_name"] and a["policy_due"]
    ]
    if policy != code_change.REVISE or not changed or not _judgeable(row):
        return {}, set()
    claim = repo.claim_of(row["value"])
    if claim is None:
        return {}, set()
    if budget.judgements <= 0:
        return None
    # the batch read may be old by now, and every judgement awaits the model: the memory is
    # read again before the first and after each, and one rewritten, held or labelled
    # `high` meanwhile is judged no further, what was judged of it dropped
    if not await _unchanged(org, row):
        return {}, set()
    updated: dict[tuple[str, str], LinkRow] = {}
    due: set[tuple[str, str]] = set()
    for a in changed:
        key = (a["path"], a["qualified_name"])
        if budget.judgements <= 0:
            due.add(key)
            continue
        link = await _judge(files, claim, a, head, budget)
        if not await _unchanged(org, row):
            return {}, set()
        if link is not None:
            updated[key] = link
            result.suggested += bool(link["suggestion"])
    return None if len(due) == len(changed) else (updated, due)


async def _unchanged(org: str, row: dict[str, Any]) -> bool:
    """Whether memory `row` is still as read, and the judge may still read it."""
    now = await store.memory_heads(org, [row["mid"]])
    return bool(now) and inputs_now(org, now[0]) == inputs_now(org, row) and _judgeable(now[0])


def _judgeable(row: dict[str, Any]) -> bool:
    """Whether the judge may read memory `row`: served, not held by a person, and not
    labelled `high`, whose value never goes to the judge's model."""
    return (
        row.get("tombstoned_at") is None
        and row["status"] in SERVED
        and not code_change.held(row)
        and row.get("sensitivity") != "high"
    )


async def _judge(
    files: repo.Files, claim: str, link: LinkRow, head: str, budget: repo.Budget
) -> LinkRow | None:
    """`link` judged against the code it was written for and the code now, spending one
    of `budget`'s judgements; None when it is not judged: a method its renamed class
    carried (its text cannot show what changed in the class), either side gone, the same
    text on both, text past the judge's reach, or an unsure verdict."""
    if link["carried"]:
        return None
    if not await files.has(link["anchored_commit"]):
        return None  # the code it was written against went with a rewrite
    was = await files.defs(link["anchored_commit"], link["anchored_path"], link["anchored_name"])
    group = await files.defs(head, link["path"], link["qualified_name"])
    if not was or not group:
        return None
    # what each side's fingerprint covers, overloads and conditions with it
    old, new = was[0].shown, group[0].shown
    if old == new:
        return None  # the change is in nothing the judge would read
    if not code_link_judge.readable(claim, old, new):
        return None
    budget.judgements -= 1  # only a claim the judge reads costs one
    return await code_link_judge.judge(claim, link, old, new, group[0], head)

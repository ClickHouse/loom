"""A namespace's code-change policy, run on the links a change left due: `revise`
asks the judge whether a changed claim still holds."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.anchor import Definition
from server.code.domain.declaration import fingerprint_version
from server.core.datetimes import naive_utc_now
from server.memory.app import code_link_judge
from server.memory.app import code_link_pass as link_pass
from server.memory.app.code_link_reads import (
    SERVED,
    inputs_now,
)
from server.memory.domain import code_change
from server.memory.domain.code_change import REVISE
from server.memory.store import code_links as store
from server.memory.store.code_links import LinkRow


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


@dataclass
class _Revised:
    """What a policy did with one memory's links, by (path, qualified_name): those it
    updated, those past the budget and those whose judgement failed (both left due)."""

    updated: dict[tuple[str, str], LinkRow] = field(default_factory=dict)
    due: set[tuple[str, str]] = field(default_factory=set)
    failed: set[tuple[str, str]] = field(default_factory=set)


async def apply_policies(
    org: str,
    cns: str,
    head: str,
    files: link_pass.RepoReads,
    budget: link_pass.Budget,
    result: link_pass.PassResult,
) -> None:
    """Run the policy of each memory a change in `cns` left due and clear the marks it
    settled; what fails or is past the judgement budget stays due. An org whose model
    calls are switched off is left as it is: its links stay due, nothing written."""
    from server.llm import customer

    if await customer.switched_off(org):
        return
    # a page a few times the budget: what is left over is read by the next pass
    due = await store.policy_due(org, cns, fingerprint_version(), limit=4 * budget.judgements + 100)
    if not due:
        return
    links: dict[str, list[LinkRow]] = defaultdict(list)
    for a in await store.for_memories(org, due):
        if a["state"] != "pending":
            links[a["memory_id"]].append(a)
    heads = {h["mid"]: h for h in await store.memory_heads(org, due)}
    policies = await store.code_change_policies(
        org, sorted({h["namespace"] for h in heads.values()})
    )
    rows: list[LinkRow] = []  # settled without a judgement: written once, at the end
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
            out: _Revised | None = _Revised()
            if mine:
                policy = code_change.policy_of(policies.get(str(row["namespace"]), ""))
                spent = budget.judgements
                out = await _revise(org, row, mine, policy, head, files, budget, result)
                if out is not None and budget.judgements < spent:
                    # paid for: written now, right after the memory was last read
                    await store.insert(_policy_rows(mine, out))
                    continue
            if out is not None:
                rows += _policy_rows(mine, out)
        except Exception as e:  # noqa: BLE001 — one memory's failure never stops the pass
            result.errors.append(f"policy {org}/{mid}: {e}")
    await store.insert(rows)


def _policy_rows(links: list[LinkRow], out: _Revised) -> list[LinkRow]:
    """The new version of each of `links`. A failed judgement's is stamped as checked
    now, so the next passes read the memories still due before it."""
    rows = []
    for a in links:
        k = (a["path"], a["qualified_name"])
        stamp = {"checked_at": naive_utc_now()} if k in out.failed else {}
        due = int(k in out.due or k in out.failed)
        rows.append(
            store.link_row(out.updated.get(k, a), follows=a["version"], policy_due=due, **stamp)
        )
    return rows


async def _revise(
    org: str,
    row: dict[str, Any],
    links: list[LinkRow],
    policy: str,
    head: str,
    files: link_pass.RepoReads,
    budget: link_pass.Budget,
    result: link_pass.PassResult,
) -> _Revised | None:
    """The `revise` policy for one memory; None when the whole memory is deferred to the
    next pass. Each changed definition is judged on its own."""
    changed = [
        a for a in links if a["state"] == "changed" and a["qualified_name"] and a["policy_due"]
    ]
    if policy != code_change.REVISE or not changed or not _judgeable(row):
        return _Revised()
    claim = link_pass.claim_of(row["value"])
    if claim is None:
        return _Revised()
    if budget.judgements <= 0:
        return None
    out = _Revised()
    version = fingerprint_version()
    for a in changed:
        key = (a["path"], a["qualified_name"])
        if budget.judgements <= 0 or a["fingerprint_version"] != version:
            # past the budget, or hashed under other rules: due until it is re-hashed
            out.due.add(key)
            continue
        texts = await _texts(files, claim, a, head)
        if texts is None:
            continue
        # read again right before the judge: one rewritten, held or labelled `high`
        # since is judged no further, what was judged of it dropped
        if not await _unchanged(org, row):
            return _Revised()
        try:
            link = await _judge(claim, a, texts, head, budget)
        except Exception as e:  # noqa: BLE001 — a failed judgement leaves its link due
            result.errors.append(f"judge {org}/{row['mid']}: {e}")
            out.failed.add(key)
            continue
        if link is not None:
            out.updated[key] = link
            result.suggested += bool(link["suggestion"])
    if out.updated and not await _unchanged(org, row):
        return _Revised()
    return None if len(out.due) == len(changed) else out


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


@dataclass(frozen=True)
class _Texts:
    """What the judge reads of a link: its code where the claim was written (`old`) and
    at the head (`new`, of definition `now`)."""

    old: str
    new: str
    now: Definition


async def _texts(files: link_pass.RepoReads, claim: str, link: LinkRow, head: str) -> _Texts | None:
    """The texts the judge would read for `link`; None when it is not judged: a method
    its renamed class carried (its text cannot show what changed in the class), either
    side gone, the same text on both, or text past the judge's reach."""
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
    if old == new or not code_link_judge.readable(claim, old, new):
        return None  # the change is in nothing the judge would read, or past its reach
    return _Texts(old, new, group[0])


async def _judge(
    claim: str, link: LinkRow, texts: _Texts, head: str, budget: link_pass.Budget
) -> LinkRow | None:
    """`link` judged on `texts`, spending one of `budget`'s judgements; None on an unsure
    verdict. Raises when the model fails or its reply is malformed."""
    budget.judgements -= 1
    return await code_link_judge.judge(claim, link, texts.old, texts.new, texts.now, head)

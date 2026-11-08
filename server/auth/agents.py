"""Agents a workload trust tells apart: one identity and one namespace each.

A trust registered with `--agent-claim` (`loom bot create`) accepts the OIDC
token a control plane issues per agent, and the claim it names says which
agent this is. Each agent gets its own subject, `sa:<trust>:<agent>`, and its
own namespace, `<trust>-<agent>` in the trust's org, made on its first
exchange, so no one mints or names a token per agent and nothing long-lived
sits in the agent's machine. A claim never picks the org, and never reaches a
namespace the trust did not make.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from server.auth import machines
from server.auth.grant_claims import account_change
from server.core.ch import get_client
from server.observability import metrics

# The agent's name as its claim must give it: it becomes part of a subject and
# a namespace, so it is refused, never rewritten into another agent's name.
AGENT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")

# Bytes a token spends on the namespaces it names: room for a 4096-bit signature
# under a proxy's 8 KB header limit (docs/architecture/security.md).
TOKEN_NAMESPACE_BYTES = 4096

log = logging.getLogger(__name__)


def subject(trust: str, agent: str) -> str:
    # ':' is never in a bot's name or a person's token name (`token_name`).
    return f"{machines.subject(trust)}:{agent}"


def namespace(trust: str, agent: str) -> str:
    return f"{trust}-{agent}"


def parse(subject_: str) -> tuple[str, str] | None:
    """(trust, agent) of an agent's subject; None for any other subject."""
    trust, _, agent = subject_.removeprefix("sa:").partition(":")
    if not subject_.startswith("sa:") or not agent:
        return None
    return trust, agent


def home_of(subject_: str) -> str:
    """The namespace of the agent whose subject is `subject_`; "" for any
    other subject."""
    found = parse(subject_)
    return namespace(*found) if found else ""


async def granted(org: str, subject_: str) -> dict[str, str]:
    """The active namespaces of `org` where `subject_` holds a live grant, with
    the role it counts as in a token: writer at most, so an agent cannot pass
    on admin over a namespace it was lent."""
    from server.core.grant_cache import grant_map

    ranks = (await grant_map(org, subject_)).ranks
    held = {ns: "viewer" if rank == 1 else "writer" for ns, rank in ranks.items() if rank > 0}
    return {ns: held[ns] for ns in sorted(await machines.active_namespaces(org, sorted(held)))}


def lent(roles: dict[str, str], held: dict[str, str]) -> list[str]:
    """The namespaces of `held` beyond `roles` (the agent's own and its
    trust's), by name."""
    return [ns for ns in held if ns not in roles]


def names_bytes(names: Iterable[str]) -> int:
    """What `names` cost in a token, against TOKEN_NAMESPACE_BYTES."""
    return sum(map(token_bytes, names))


def token_bytes(ns: str) -> int:
    """What one name costs in a token: `"ns",` in `namespaces` and
    `"ns":"viewer",` in `ns_roles`."""
    return 2 * len(ns.encode()) + 15


async def with_grants(
    m: machines.Machine, agent: str, ns_roles: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """`ns_roles` and the namespaces of the trust's org the agent holds a live
    grant on (another agent's share, an admin's grant), as `granted` counts
    them. A grant raises a role `ns_roles` gives, and a lower or revoked one
    caps that role at the gate (`authz.claim_cap`). Namespaces are added by
    name until the next would take the token's namespaces, `ns_roles` included,
    past TOKEN_NAMESPACE_BYTES, so the token stays within a proxy's header
    limit; `grants.share_agent` checks the same budget, but two shares at once
    can both pass it."""
    from server.operations.base import grant_rank

    held = await granted(m["org"], subject(m["name"], agent))
    roles = dict(ns_roles)
    extra = lent(roles, held)
    for ns, role in held.items():
        if ns in roles and grant_rank(role) > grant_rank(roles[ns]):
            roles[ns] = role
    used = names_bytes(roles)
    kept = 0
    for ns in extra:
        used += token_bytes(ns)
        if used > TOKEN_NAMESPACE_BYTES:
            break
        roles[ns] = held[ns]
        kept += 1
    left_out = len(extra) - kept
    if left_out:
        log.warning(
            "agent %s of %s: %d granted namespaces left out of its token",
            agent,
            m["name"],
            left_out,
        )
    return list(roles.items())


TAG_PREFIX = "machine:"


def tag(trust: str) -> str:
    """Marks a namespace this trust made: the only kind its agents reach. Only
    the trust sets it; `namespace.register` keeps a caller from giving it."""
    return TAG_PREFIX + trust


def agent_of(m: machines.Machine, claims: dict[str, Any]) -> str:
    """The agent the workload token names; Refused when it names none Loom takes."""
    agent = str(claims.get(m["agent_claim"], ""))
    if not AGENT.fullmatch(agent):
        raise machines.Refused(
            "invalid_grant",
            f"claim {m['agent_claim']!r} must name the agent as lower-case letters, digits and '-'",
        )
    return agent


async def ensure_namespace(m: machines.Machine, agent: str, *, by: str = "") -> str:
    """The agent's namespace, made on its first exchange, as an account is:
    the row names its one owner, the agent, and then its owner grant follows.
    A later exchange writes that grant only if it was never written (the first
    one cut off after the row); one revoked since leaves a row, so it stays
    revoked. Held as a release holds it (its reads are sequential under the
    claim), so two first exchanges make one, and one released is not made again.
    `by` is who made it when not the trust's exchange (an admin's, ahead of it)."""
    from server.auth.directory import directory
    from server.auth.grant_claims import bot_change
    from server.auth.self_signup import provision_account

    org, ns, who = m["org"], namespace(m["name"], agent), subject(m["name"], agent)
    made = False
    async with account_change(org, ns):
        row = await get_client().query_one(
            "SELECT active, tags, owner_email FROM loom.namespaces FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": org, "ns": ns},
        )
        if row is not None:
            # Only the trust provisions its tag (a caller's is dropped at
            # register), and only for the agent it names.
            if tag(m["name"]) not in row["tags"]:
                raise machines.Refused("invalid_grant", f"{org}/{ns} is not this trust's")
            if not row["active"]:  # a release clears its owner too
                raise machines.Refused("invalid_grant", f"{org}/{ns} was released")
            if row["owner_email"] != who:
                raise machines.Refused("invalid_grant", f"{org}/{ns} is not this trust's")
            if not await _granted(org, ns, who):
                # The first exchange stopped after the row, before its grant,
                # and so before it counted and audited the agent: this one does.
                made = await _finish_grant(org, ns, who)
        else:
            # Data or grants under the name with no row are someone else's.
            if await directory().in_use(org, ns):
                raise machines.Refused("invalid_grant", f"{org}/{ns} is not this trust's")
            # The trust's own claim: two new agents at once must not both see room.
            async with bot_change(m["name"]):
                if await _made(org, m["name"]) >= m["max_agents"]:
                    # A broken or compromised issuer could name agents without end.
                    metrics.record_agent_provisioned(org, m["name"], outcome="at_cap")
                    raise machines.Refused(
                        "invalid_grant", f"trust {m['name']} has made its agents"
                    )
                # No contact email: a sign-in by email must never resolve to an agent.
                await provision_account(
                    org=org,
                    namespace=ns,
                    subject=who,
                    contact_email="",
                    description=f"Agent {agent} of {m['name']}.",
                    tags=[tag(m["name"])],
                )
            made = True
    if made:
        from server.observability import audit

        metrics.record_agent_provisioned(org, m["name"], outcome="made")
        await audit.emit(
            "auth.agent_provisioned",
            org=org,
            namespace=ns,
            actor=by or machines.subject(m["name"]),
            subject={"agent": agent},
        )
    return ns


async def _granted(org: str, ns: str, who: str) -> bool:
    """Whether the agent's owner grant was ever written; revoked, it leaves a row."""
    row = await get_client().query_one(
        "SELECT 1 AS x FROM loom.namespace_grants WHERE org = {org:String} "
        "AND namespace = {ns:String} AND subject = {who:String} LIMIT 1",
        params={"org": org, "ns": ns, "who": who},
    )
    return row is not None


async def _finish_grant(org: str, ns: str, who: str) -> bool:
    """The agent's owner grant, held as a grant change is held from its read
    to its write, so a revocation is never written over. True when this call
    wrote it: a grant written since the check (a grant change for the agent)
    is left alone and not counted."""
    from server.auth.grant_claims import grant_change
    from server.auth.self_signup import insert_owner_grant

    async with grant_change(org, ns, who):
        if await _granted(org, ns, who):
            return False
        await insert_owner_grant(org, ns, who)
        return True


async def _made(org: str, trust: str) -> int:
    """How many live agent namespaces the trust has: a released one gives its
    place back (its name is never made again), and only an operator releases,
    so an issuer naming agents without end still stops at the cap."""
    row = await get_client().query_one(
        "SELECT count() AS n FROM loom.namespaces FINAL "
        "WHERE org = {org:String} AND has(tags, {tag:String}) AND active = 1",
        params={"org": org, "tag": tag(trust)},
    )
    return int((row or {}).get("n") or 0)

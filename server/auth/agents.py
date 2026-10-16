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

import re
from typing import Any

from server.auth import machines
from server.auth.grant_claims import account_change
from server.core.ch import get_client

# The agent's name as its claim must give it: it becomes part of a subject and
# a namespace, so it is refused, never rewritten into another agent's name.
AGENT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def subject(trust: str, agent: str) -> str:
    # ':' is never in a bot's or a person's service-account name.
    return f"{machines.subject(trust)}:{agent}"


def namespace(trust: str, agent: str) -> str:
    return f"{trust}-{agent}"


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


async def ensure_namespace(m: machines.Machine, agent: str) -> str:
    """The agent's namespace, made on its first exchange, as an account is:
    the row names its one owner, the agent, and then its owner grant follows.
    A later exchange writes that grant only if it was never written (the first
    one cut off after the row); one revoked since leaves a row, so it stays
    revoked. Held as a release holds it (its reads are sequential under the
    claim), so two first exchanges make one, and one released is not made again."""
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
                await _finish_grant(org, ns, who)
        else:
            # The trust's own claim: two new agents at once must not both see room.
            async with bot_change(m["name"]):
                if await _made(org, m["name"]) >= m["max_agents"]:
                    # A broken or compromised issuer could name agents without end.
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

        await audit.emit(
            "auth.agent_provisioned",
            org=org,
            namespace=ns,
            actor=machines.subject(m["name"]),
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


async def _finish_grant(org: str, ns: str, who: str) -> None:
    """The agent's owner grant, held as a grant change is held from its read
    to its write, so a revocation is never written over."""
    from server.auth.grant_claims import grant_change
    from server.auth.self_signup import insert_owner_grant

    async with grant_change(org, ns, who):
        if not await _granted(org, ns, who):
            await insert_owner_grant(org, ns, who)


async def _made(org: str, trust: str) -> int:
    """How many agent namespaces the trust has made, released ones too."""
    row = await get_client().query_one(
        "SELECT count() AS n FROM loom.namespaces FINAL "
        "WHERE org = {org:String} AND has(tags, {tag:String})",
        params={"org": org, "tag": tag(trust)},
    )
    return int((row or {}).get("n") or 0)

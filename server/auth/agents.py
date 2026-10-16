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


def tag(trust: str) -> str:
    """Marks a namespace this trust made: the only kind its agents reach."""
    return f"machine:{trust}"


def agent_of(m: machines.Machine, claims: dict[str, Any]) -> str:
    """The agent the workload token names; Refused when it names none Loom takes."""
    agent = str(claims.get(m["agent_claim"], ""))
    if not AGENT.fullmatch(agent):
        raise machines.Refused(
            "invalid_grant",
            f"claim {m['agent_claim']!r} must name the agent as lower-case letters, digits and '-'",
        )
    return agent


async def ensure_namespace(m: machines.Machine, agent: str, owner_email: str) -> str:
    """The agent's namespace, made on its first exchange. Held as a release
    holds it, so two first exchanges make one, and one released since is not
    made again under the agent."""
    from server.auth.self_signup import provision_account

    org, ns = m["org"], namespace(m["name"], agent)
    async with account_change(org, ns):
        row = await get_client().query_one(
            "SELECT active, tags FROM loom.namespaces FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": org, "ns": ns},
        )
        if row is not None:
            if tag(m["name"]) not in row["tags"]:
                raise machines.Refused("invalid_grant", f"{org}/{ns} is not this trust's")
            if not row["active"]:
                raise machines.Refused("invalid_grant", f"{org}/{ns} was released")
            return ns
        await provision_account(
            org=org,
            namespace=ns,
            subject=subject(m["name"], agent),
            contact_email=owner_email,
            description=f"Agent {agent} of {m['name']}.",
            tags=[tag(m["name"])],
        )
        await _grant_owner(org, ns, owner_email)
    from server.observability import audit

    await audit.emit(
        "auth.agent_provisioned",
        org=org,
        namespace=ns,
        actor=machines.subject(m["name"]),
        subject={"agent": agent, "owner": owner_email},
    )
    return ns


async def _grant_owner(org: str, ns: str, owner_email: str) -> None:
    """The owner the trust vouches for reads and runs their agent's memory, when
    they have an account in the org."""
    from server.auth.login import resolve_identity_for_email
    from server.auth.self_signup import insert_owner_grant

    if not owner_email:
        return
    person = await resolve_identity_for_email(owner_email)
    if person is not None and person[1] == org:
        await insert_owner_grant(org, ns, person[0])

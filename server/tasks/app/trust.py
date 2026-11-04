"""Who may post to an inbox, and whether a post lands in ``triage`` or
``todo``: the owner's policy rows, read as the owner set them."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from server.tasks.app.common import (
    _caller_is_human,
    _err,
    _in_scope,
    _invalid,
    _triage_next,
    owner_subject,
)
from server.tasks.store import repo as repo
from server.tasks.store.rows import InboxRow, PolicyRow


def _missing_evidence(brief: dict[str, Any], required: Iterable[str]) -> list[str]:
    missing = []
    for path in required:
        node: Any = brief
        ok = True
        for part in str(path).split("."):
            if isinstance(node, dict) and part in node and node[part] not in ("", None, [], {}):
                node = node[part]
            else:
                ok = False
                break
        if not ok:
            missing.append(str(path))
    return missing


async def _poster_may_post(inbox: InboxRow, subject: str, agent: str) -> bool:
    # The posters list needs no read, so the owner check, last, decides alone.
    posters = set(inbox.get("posters") or [])
    if bool(subject) and subject in posters:
        return True
    # A listed agent LABEL admits only a caller with no token to check; with one, the entry
    # must name its subject.
    if not _token_subject() and agent != "-" and agent in posters:
        return True
    return await _in_scope(inbox["org"], inbox["owner_namespace"], write=True)


async def _require_poster(inbox: InboxRow, subject: str, agent: str) -> None:
    """Refuse a caller that may not post to `inbox`. One that may not read its namespace
    either gets the unregistered-name answer: the name is guessable, and a 403 would
    confirm the inbox exists."""
    if await _poster_may_post(inbox, subject, agent):
        return
    if not await _in_scope(inbox["org"], inbox["owner_namespace"], write=False):
        raise _err(404, "RESOURCE_NOT_FOUND", f"inbox {inbox['inbox']!r} is not registered")
    raise _err(403, "AUTH_INSUFFICIENT", "this principal may not post to that inbox")


async def _is_owner_principal(inbox: InboxRow, subject: str) -> bool:
    """The caller IS the inbox's owner: the subject that registered it, or a live service
    account that subject minted (one hop). Two different logins of one person are not
    equated."""
    owner = owner_subject(inbox)
    if not subject or not owner:
        return False
    if subject == owner:
        return True
    if subject.startswith("sa:"):
        return await repo.service_account_creator(inbox["org"], subject) == owner
    return False


def _token_subject() -> str:
    """The verified subject of the caller's token; "" without one. Unlike
    `_caller_subject` it never falls back to a body field."""
    from server.auth.context import current_auth

    auth = current_auth()
    return str(auth.subject) if auth is not None and auth.subject else ""


async def _owner_side(inbox: InboxRow, *, subject: str, agent: str, verified: bool) -> bool:
    """Who lands ``auto`` when the owner set no row for them. With a token,
    trust follows the principal: the inbox's owner (above), or a principal
    the owner granted write on the owner namespace: a person, or a bot posting as the
    owner's agent (a triage bot filing its own follow-ups). The ``agent`` label is the
    caller's to choose, so on its own it grants nothing; otherwise a poster outside the
    owner namespace naming the owner's agent would be trusted as the owner. Without a token
    there is no principal to bind to and the label rule stays."""
    if not verified:
        return bool(agent == inbox["owner_agent"])
    if await _is_owner_principal(inbox, subject):
        return True
    if not await _in_scope(inbox["org"], inbox["owner_namespace"], write=True):
        return False
    return _caller_is_human() or agent == inbox["owner_agent"]


async def _label_cannot_grant(
    inbox: InboxRow, policy: PolicyRow, *, subject: str, agent: str, verified: bool
) -> PolicyRow:
    """A row the owner keyed on an agent LABEL can deny or send to review;
    its ``auto`` holds only for a writer on the owner namespace (the owner's
    own bots). A caller outside it earns ``auto`` only from a row naming its
    subject, or ``*``: the label is the caller's to choose."""
    if (
        verified
        and str(policy.get("mode")) == "auto"
        and policy.get("poster") not in (subject, "*")
        and policy.get("poster") == agent
        and not await _in_scope(inbox["org"], inbox["owner_namespace"], write=True)
    ):
        return {**policy, "mode": "review"}
    return policy


async def _decide_mode(
    inbox: InboxRow,
    *,
    subject: str,
    agent: str,
    kind: str,
    brief: dict[str, Any],
    verified: bool | None = None,
) -> tuple[str, PolicyRow | None]:
    """deny / review / auto for this poster and kind. The inbox's owner and a
    human writer on the owner namespace are ``auto`` unless a row says
    otherwise; any other poster is ``review`` unless a row promotes it.
    ``verified``: `subject` came from a token (default: it is this call's)."""
    if verified is None:
        verified = bool(subject) and subject == _token_subject()
    policies = await repo.fetch_policies(inbox["org"], inbox["inbox"])
    policy = repo.resolve_policy(policies, poster_subject=subject, poster_agent=agent, kind=kind)
    if policy is None:
        owner_side = await _owner_side(inbox, subject=subject, agent=agent, verified=verified)
        return ("auto" if owner_side else "review"), None
    policy = await _label_cannot_grant(
        inbox, policy, subject=subject, agent=agent, verified=verified
    )
    mode = str(policy["mode"])
    if mode == "deny":
        return "deny", policy
    missing = _missing_evidence(brief, policy.get("require_evidence") or [])
    if missing:
        raise _invalid(
            "the inbox requires evidence fields this brief lacks", missing_evidence=missing
        )
    return mode, policy


def _triage_note(inbox: InboxRow, agent: str, policy: PolicyRow | None) -> str:
    """Why a post landed in triage and who moves it: the inbox's own trusted posters, or the
    policy row, named."""
    why = (
        f"the inbox's policy for this poster/kind is {policy['mode']!r}"
        if policy
        else (
            f"the inbox trusts only its owner agent {inbox['owner_agent']!r} and people with "
            f"write on {inbox['owner_namespace']!r}; this post came from agent {agent!r}"
        )
    )
    return (
        f"status=triage: {why}. Until it is accepted task.pull does not hand it out and "
        f"heartbeat/complete/defer refuse it. Next: {_triage_next(inbox)}"
    )
